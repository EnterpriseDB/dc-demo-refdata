"""Use case 1: high-throughput OLTP checkout traffic hammering a tiny,
shared `order_status` lookup table via foreign keys.

Loads ~50MB of synthetic `orders` data, then runs the same concurrent
insert workload twice -- once with `order_status` as plain heap, once
after `ALTER TABLE order_status SET ACCESS METHOD refdata` -- and prints
a before/after comparison.
"""
import argparse
import json
import random
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import advisor, bench, db

DIM = "public.demo1_order_status"
FACT = "public.demo1_orders"
APP_NAME = "refdata_demo_orders_bench"

STATUSES = [
    (1, "PENDING", "Order placed, awaiting payment"),
    (2, "PAID", "Payment received"),
    (3, "PACKED", "Items packed for shipment"),
    (4, "SHIPPED", "Handed to carrier"),
    (5, "DELIVERED", "Delivered to customer"),
    (6, "CANCELLED", "Order cancelled by customer"),
    (7, "REFUNDED", "Payment refunded"),
    (8, "ON_HOLD", "Held for manual review"),
]
STATUS_IDS = [s[0] for s in STATUSES]

STREETS = ["Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Elm St", "Park Rd",
           "Washington Ave", "Lake St", "Hill Rd", "River Rd"]
CITIES = ["Springfield", "Riverside", "Franklin", "Greenville", "Fairview",
          "Clinton", "Salem", "Madison", "Georgetown", "Arlington"]
STATES = ["CA", "TX", "NY", "FL", "IL", "PA", "OH", "GA", "NC", "WA"]


def random_address(rng: random.Random) -> str:
    pad = "".join(rng.choices(string.ascii_letters + string.digits, k=140))
    return (f"{rng.randint(1, 9999)} {rng.choice(STREETS)}, {rng.choice(CITIES)}, "
            f"{rng.choice(STATES)} {rng.randint(10000, 99999)} #{pad}")


def setup_schema():
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("create extension if not exists refdata")
        bench.ensure_multixact_helper(cur)
        cur.execute(f"drop table if exists {FACT} cascade")
        cur.execute(f"drop table if exists {DIM} cascade")
        cur.execute(f"""
            create table {DIM} (
                id smallint primary key,
                code text not null,
                description text not null
            )
        """)
        cur.executemany(
            f"insert into {DIM} (id, code, description) values (%s, %s, %s)",
            STATUSES,
        )
        cur.execute(f"""
            create table {FACT} (
                id bigserial primary key,
                customer_id integer not null,
                status_id smallint not null references {DIM}(id),
                amount numeric(10,2) not null,
                placed_at timestamptz not null default now(),
                shipping_address text not null
            )
        """)
        cur.execute(f"create index on {FACT} (status_id)")


def bulk_load(target_bytes: int):
    rng = random.Random(42)

    def copy_batch(cur, n):
        with cur.copy(
            f"copy {FACT} (customer_id, status_id, amount, shipping_address) from stdin"
        ) as cp:
            for _ in range(n):
                cp.write_row((
                    rng.randint(1, 2_000_000),
                    rng.choice(STATUS_IDS),
                    round(rng.uniform(5, 900), 2),
                    random_address(rng),
                ))

    progress = bench.ProgressPrinter()
    result = bench.load_until_size(FACT, copy_batch, target_bytes, progress_cb=progress)
    progress.finish()
    return result


def make_worker(batch_size: int, batches_per_worker: int, seed_offset: int):
    def worker(worker_id: int) -> int:
        rng = random.Random(seed_offset + worker_id)
        rows_done = 0
        with db.connect(application_name=APP_NAME) as conn:
            with conn.cursor() as cur:
                for _ in range(batches_per_worker):
                    placeholders = []
                    params = []
                    for _ in range(batch_size):
                        placeholders.append("(%s,%s,%s,%s)")
                        params.extend([
                            rng.randint(1, 2_000_000),
                            rng.choice(STATUS_IDS),
                            round(rng.uniform(5, 900), 2),
                            random_address(rng),
                        ])
                    sql = (
                        f"insert into {FACT} (customer_id, status_id, amount, shipping_address) "
                        f"values {','.join(placeholders)}"
                    )
                    cur.execute(sql, params)
                    conn.commit()
                    rows_done += batch_size
        return rows_done
    return worker


def current_max_id() -> int:
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"select coalesce(max(id), 0) from {FACT}")
        return cur.fetchone()[0]


def delete_above(watermark: int):
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"delete from {FACT} where id > %s", (watermark,))


def set_access_method(am: str):
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"alter table {DIM} set access method {am}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bulk-mb", type=int, default=50)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--batches-per-worker", type=int, default=10)
    args = p.parse_args()

    print("== order_status lookup table under checkout load ==\n")

    print("Setting up schema and seeding order_status ...")
    setup_schema()

    target_bytes = args.bulk_mb * 1024 * 1024
    print(f"Bulk loading synthetic orders to ~{args.bulk_mb}MB ...")
    rows, size = bulk_load(target_bytes)
    print(f"  loaded {rows:,} rows, orders table+indexes now {bench.human_size(size)}\n")

    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        am_before = bench.access_method(cur, DIM)
    print(f"order_status access method: {am_before}")

    watermark = current_max_id()
    print(f"\nPhase 1 (heap): {args.workers} workers x {args.batches_per_worker} batches "
          f"x {args.batch_size} rows/batch, all referencing the same {len(STATUS_IDS)} status rows ...")
    heap_worker = make_worker(args.batch_size, args.batches_per_worker, seed_offset=1000)
    heap_result = bench.run_phase("heap", DIM, heap_worker, args.workers)
    print(f"  {heap_result.rows_inserted:,} rows in {heap_result.elapsed_seconds:.2f}s "
          f"({heap_result.rows_per_second:,.0f} rows/s)")

    mxid_pct = (100.0 * heap_result.rows_with_multixact / heap_result.dim_row_count
                if heap_result.dim_row_count else 0.0)
    advisor_rec = advisor.recommend(
        DIM,
        self_dml=len(STATUSES),
        child_dml=rows + heap_result.rows_inserted,
        rows=heap_result.dim_row_count,
        mxid_pct=mxid_pct,
        am="heap",
    )
    print(f"\n{advisor.header_line(advisor_rec)}")
    print("(based on the heap-phase access pattern observed above)")
    advisor.print_report(advisor_rec)
    print("ADVISOR_JSON:" + json.dumps(advisor_rec))

    delete_above(watermark)

    print(f"\nConverting order_status to refdata ...")
    set_access_method("refdata")
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        am_after = bench.access_method(cur, DIM)
    print(f"order_status access method: {am_after}")

    print(f"\nPhase 2 (refdata): same workload ...")
    refdata_worker = make_worker(args.batch_size, args.batches_per_worker, seed_offset=2000)
    refdata_result = bench.run_phase("refdata", DIM, refdata_worker, args.workers)
    print(f"  {refdata_result.rows_inserted:,} rows in {refdata_result.elapsed_seconds:.2f}s "
          f"({refdata_result.rows_per_second:,.0f} rows/s)")

    print("\n== Before / after ==")
    bench.print_comparison(heap_result, refdata_result)
    bench.print_json_summary(heap_result, refdata_result)


if __name__ == "__main__":
    main()
