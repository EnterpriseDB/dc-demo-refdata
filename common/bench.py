import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from . import db


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}TB"


class ProgressPrinter:
    """Renders bulk-load progress as an in-place terminal bar when stdout is
    a real terminal. When it's not (e.g. piped into the web UI's subprocess),
    printing a redrawn '\\r' line would just sit invisible until a newline
    ever showed up, so it emits a compact PROGRESS_JSON line per batch
    instead, for a UI to render its own bar from."""

    def __init__(self, width: int = 30):
        self._width = width
        self._tty = sys.stdout.isatty()
        self._printed = False

    def __call__(self, rows: int, size: int, target: int):
        pct = min(1.0, size / target) if target else 1.0
        if self._tty:
            filled = int(pct * self._width)
            bar = "#" * filled + "-" * (self._width - filled)
            print(f"\r  [{bar}] {pct * 100:5.1f}%  {human_size(size)} / {human_size(target)}  "
                  f"({rows:,} rows)", end="", flush=True)
            self._printed = True
        else:
            print("PROGRESS_JSON:" + json.dumps({
                "rows": rows, "size": size, "target": target, "pct": round(pct * 100, 1),
            }), flush=True)

    def finish(self):
        if self._tty and self._printed:
            print()  # leave the final bar state on-screen, move to a fresh line


def table_size(cur, qualified_name: str) -> int:
    cur.execute("select pg_total_relation_size(to_regclass(%s))", (qualified_name,))
    return cur.fetchone()[0]


def access_method(cur, qualified_name: str) -> str:
    cur.execute(
        """
        select am.amname
        from pg_class c join pg_am am on am.oid = c.relam
        where c.oid = to_regclass(%s)
        """,
        (qualified_name,),
    )
    return cur.fetchone()[0]


def load_until_size(qualified_name: str, copy_batch_fn, target_bytes: int,
                     batch_rows: int = 5000, max_rows: int = 5_000_000, progress_cb=None):
    """Repeatedly calls copy_batch_fn(cur, batch_rows) until the table (with
    its indexes/toast) reaches target_bytes on disk. Returns (rows, bytes).

    If given, progress_cb(total_rows, size_bytes, target_bytes) is called
    before each batch (and once more at the end) so long loads can report
    where they're at -- including the final call that actually reaches
    target_bytes, so a progress bar driven by it lands on 100%."""

    total_rows = 0
    with db.connect(autocommit=True) as conn:
        while True:
            with conn.cursor() as cur:
                size = table_size(cur, qualified_name)
            if progress_cb:
                progress_cb(total_rows, size, target_bytes)
            if size >= target_bytes or total_rows >= max_rows:
                return total_rows, size
            with conn.cursor() as cur:
                copy_batch_fn(cur, batch_rows)
            total_rows += batch_rows


def ensure_multixact_helper(cur):
    """A tiny helper function that reports how many transactions co-own a
    given xmax as a MultiXact. FOR KEY SHARE locks (what an FK check takes
    on a heap parent row) are recorded directly in the row's xmax rather
    than as a separate pg_locks entry, and there's no plain boolean in SQL
    for "is this xmax a MultiXact" without superuser-only pageinspect --
    but pg_get_multixact_members() errors out on anything that isn't a
    live MultiXact, which this catches and turns into 0."""

    cur.execute(
        """
        create or replace function public.refdata_demo_xmax_members(x xid)
        returns integer
        language plpgsql
        as $$
        begin
            return (select count(*)::int from pg_get_multixact_members(x));
        exception when others then
            return 0;
        end;
        $$
        """
    )


def _multixact_snapshot_cur(cur, dim_qualified_name: str):
    """Returns (rows_with_live_multixact, max_concurrent_holders_on_one_row)
    for the dimension table's on-disk state right now."""

    cur.execute(f"select public.refdata_demo_xmax_members(xmax) from {dim_qualified_name}")
    counts = [r[0] for r in cur.fetchall()]
    rows_with_multixact = sum(1 for c in counts if c > 1)
    max_members = max(counts) if counts else 0
    return rows_with_multixact, max_members


def multixact_snapshot(dim_qualified_name: str):
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        return _multixact_snapshot_cur(cur, dim_qualified_name)


class MultixactSampler:
    """A row's xmax only reflects whoever locked it *last* -- a MultiXact
    formed mid-run can be invisible by the time the run ends if the final
    lock on that row happened not to overlap with another one. That's
    especially likely to hide real contention on a table with more distinct
    rows (e.g. 15 device types) spreading concurrent workers thinner than a
    table with fewer rows (e.g. 8 order statuses). So instead of checking
    once at the end, this polls throughout the run and keeps the peak."""

    def __init__(self, dim_qualified_name: str, interval: float = 0.05):
        self._dim_qualified_name = dim_qualified_name
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._start = None
        self.peak_rows_with_multixact = 0
        self.peak_max_members = 0
        self.timeline = []

    def _record(self, rows_with_mx, max_members):
        self.peak_rows_with_multixact = max(self.peak_rows_with_multixact, rows_with_mx)
        self.peak_max_members = max(self.peak_max_members, max_members)
        t = time.perf_counter() - self._start
        self.timeline.append({
            "t": round(t, 3),
            "rows_with_multixact": rows_with_mx,
            "max_members": max_members,
        })

    def _run(self):
        conn = db.connect(application_name="refdata_demo_mx_sampler", autocommit=True)
        cur = conn.cursor()
        try:
            while not self._stop.is_set():
                rows_with_mx, max_members = _multixact_snapshot_cur(cur, self._dim_qualified_name)
                self._record(rows_with_mx, max_members)
                time.sleep(self._interval)
        finally:
            cur.close()
            conn.close()

    def sample_once_more(self):
        rows_with_mx, max_members = multixact_snapshot(self._dim_qualified_name)
        self._record(rows_with_mx, max_members)

    def __enter__(self):
        self._start = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self.sample_once_more()


@dataclass
class PhaseResult:
    label: str
    rows_inserted: int
    elapsed_seconds: float
    dim_row_count: int
    rows_with_multixact: int
    max_concurrent_holders_on_one_row: int
    timeline: list

    @property
    def rows_per_second(self) -> float:
        return self.rows_inserted / self.elapsed_seconds if self.elapsed_seconds else 0.0


def run_phase(label: str, dim_qualified_name: str, worker_fn, n_workers: int) -> PhaseResult:
    """Runs n_workers concurrent worker_fn(worker_id) callables, each of which
    should open its own connection, perform its share of INSERTs against the
    fact table, and return the number of rows it inserted."""

    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"select count(*) from {dim_qualified_name}")
        dim_row_count = cur.fetchone()[0]

    sampler = MultixactSampler(dim_qualified_name)
    start = time.perf_counter()
    with sampler:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            counts = list(pool.map(worker_fn, range(n_workers)))
    elapsed = time.perf_counter() - start

    return PhaseResult(
        label=label,
        rows_inserted=sum(counts),
        elapsed_seconds=elapsed,
        dim_row_count=dim_row_count,
        rows_with_multixact=sampler.peak_rows_with_multixact,
        max_concurrent_holders_on_one_row=sampler.peak_max_members,
        timeline=sampler.timeline,
    )


def print_comparison(heap: PhaseResult, refdata: PhaseResult):
    def pct_change(before, after):
        if before == 0:
            return "n/a"
        change = 100.0 * (after - before) / before
        return f"{change:+.1f}%"

    rows = [
        ("Rows inserted", f"{heap.rows_inserted:,}", f"{refdata.rows_inserted:,}", ""),
        ("Wall time (s)", f"{heap.elapsed_seconds:.2f}", f"{refdata.elapsed_seconds:.2f}",
         pct_change(heap.elapsed_seconds, refdata.elapsed_seconds)),
        ("Throughput (rows/s)", f"{heap.rows_per_second:,.0f}", f"{refdata.rows_per_second:,.0f}",
         pct_change(heap.rows_per_second, refdata.rows_per_second)),
        ("Lookup rows seen carrying a live MultiXact (peak)",
         f"{heap.rows_with_multixact}/{heap.dim_row_count}",
         f"{refdata.rows_with_multixact}/{refdata.dim_row_count}", ""),
        ("Max concurrent lock-holders seen on one row",
         str(heap.max_concurrent_holders_on_one_row),
         str(refdata.max_concurrent_holders_on_one_row), ""),
    ]

    name_w = max(len(r[0]) for r in rows) + 2
    col_w = max(max(len(r[1]) for r in rows), max(len(r[2]) for r in rows), len("heap")) + 2

    header = f"{'metric':<{name_w}}{'heap':<{col_w}}{'refdata':<{col_w}}{'change'}"
    print(header)
    print("-" * len(header))
    for name, h, r, chg in rows:
        print(f"{name:<{name_w}}{h:<{col_w}}{r:<{col_w}}{chg}")


def print_json_summary(heap: PhaseResult, refdata: PhaseResult):
    """Emits a single machine-readable line so a UI can chart the results
    without having to parse the pretty-printed table above."""

    def phase_dict(phase: PhaseResult) -> dict:
        d = asdict(phase)
        d["rows_per_second"] = phase.rows_per_second
        return d

    summary = {"heap": phase_dict(heap), "refdata": phase_dict(refdata)}
    print("RESULT_JSON:" + json.dumps(summary))
