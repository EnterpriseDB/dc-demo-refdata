"""Use case 3: high-volume IoT telemetry ingestion hammering a tiny,
shared `device_type` lookup table via foreign keys.

Loads ~50MB of synthetic `telemetry_events` data, then runs the same
concurrent insert workload twice -- once with `device_type` as plain
heap, once after `ALTER TABLE device_type SET ACCESS METHOD refdata`
-- and prints a before/after comparison.
"""
import argparse
import json
import random
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import advisor, bench, db

DIM = "public.demo3_device_type"
FACT = "public.demo3_telemetry_events"
APP_NAME = "refdata_demo_telemetry_bench"

DEVICE_TYPES = [
    (1, "thermostat", "climate"),
    (2, "smoke_detector", "safety"),
    (3, "door_sensor", "security"),
    (4, "motion_sensor", "security"),
    (5, "water_leak_sensor", "safety"),
    (6, "smart_plug", "energy"),
    (7, "camera", "security"),
    (8, "air_quality_sensor", "climate"),
    (9, "humidity_sensor", "climate"),
    (10, "light_sensor", "energy"),
    (11, "vibration_sensor", "industrial"),
    (12, "gps_tracker", "asset"),
    (13, "pressure_sensor", "industrial"),
    (14, "gas_sensor", "safety"),
    (15, "smart_lock", "security"),
]
DEVICE_TYPE_IDS = [d[0] for d in DEVICE_TYPES]


def random_payload(rng: random.Random) -> str:
    pad = "".join(rng.choices(string.hexdigits.lower(), k=160))
    return (f'{{"battery_pct":{rng.randint(1,100)},"rssi":{-rng.randint(30,95)},'
            f'"fw":"1.{rng.randint(0,9)}.{rng.randint(0,20)}","raw":"{pad}"}}')


def setup_schema():
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("create extension if not exists refdata")
        bench.ensure_multixact_helper(cur)
        cur.execute(f"drop table if exists {FACT} cascade")
        cur.execute(f"drop table if exists {DIM} cascade")
        cur.execute(f"""
            create table {DIM} (
                id smallint primary key,
                name text not null,
                category text not null
            )
        """)
        cur.executemany(
            f"insert into {DIM} (id, name, category) values (%s, %s, %s)",
            DEVICE_TYPES,
        )
        cur.execute(f"""
            create table {FACT} (
                id bigserial primary key,
                device_type_id smallint not null references {DIM}(id),
                sensor_id integer not null,
                reading numeric(10,4) not null,
                recorded_at timestamptz not null default now(),
                payload text not null
            )
        """)
        cur.execute(f"create index on {FACT} (device_type_id)")


def bulk_load(target_bytes: int):
    rng = random.Random(7)

    def copy_batch(cur, n):
        with cur.copy(
            f"copy {FACT} (device_type_id, sensor_id, reading, payload) from stdin"
        ) as cp:
            for _ in range(n):
                cp.write_row((
                    rng.choice(DEVICE_TYPE_IDS),
                    rng.randint(1, 500_000),
                    round(rng.uniform(-40, 140), 4),
                    random_payload(rng),
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
                            rng.choice(DEVICE_TYPE_IDS),
                            rng.randint(1, 500_000),
                            round(rng.uniform(-40, 140), 4),
                            random_payload(rng),
                        ])
                    sql = (
                        f"insert into {FACT} (device_type_id, sensor_id, reading, payload) "
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

    print("== device_type lookup table under IoT ingest load ==\n")

    print("Setting up schema and seeding device_type ...")
    setup_schema()

    target_bytes = args.bulk_mb * 1024 * 1024
    print(f"Bulk loading synthetic telemetry_events to ~{args.bulk_mb}MB ...")
    rows, size = bulk_load(target_bytes)
    print(f"  loaded {rows:,} rows, telemetry_events table+indexes now {bench.human_size(size)}\n")

    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        am_before = bench.access_method(cur, DIM)
    print(f"device_type access method: {am_before}")

    watermark = current_max_id()
    print(f"\nPhase 1 (heap): {args.workers} workers x {args.batches_per_worker} batches "
          f"x {args.batch_size} rows/batch, all referencing the same {len(DEVICE_TYPE_IDS)} device types ...")
    heap_worker = make_worker(args.batch_size, args.batches_per_worker, seed_offset=1000)
    heap_result = bench.run_phase("heap", DIM, heap_worker, args.workers)
    print(f"  {heap_result.rows_inserted:,} rows in {heap_result.elapsed_seconds:.2f}s "
          f"({heap_result.rows_per_second:,.0f} rows/s)")

    mxid_pct = (100.0 * heap_result.rows_with_multixact / heap_result.dim_row_count
                if heap_result.dim_row_count else 0.0)
    advisor_rec = advisor.recommend(
        DIM,
        self_dml=len(DEVICE_TYPES),
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

    print(f"\nConverting device_type to refdata ...")
    set_access_method("refdata")
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        am_after = bench.access_method(cur, DIM)
    print(f"device_type access method: {am_after}")

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
