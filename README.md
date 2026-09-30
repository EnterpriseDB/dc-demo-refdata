# refdata before/after demo

Two runnable demos showing the effect of PostgreSQL's `refdata` table
access method (EDB Advanced Storage Pack) on foreign-key-heavy
workloads, plus a small Flask UI to trigger them and visualize the
results.

`refdata` is designed for small, rarely-modified lookup/reference
tables. A write to a `refdata` table takes a table-level
`ExclusiveLock`, which guarantees a referenced row can't disappear
mid-transaction -- so child-table inserts skip the per-row
`FOR KEY SHARE` lock they'd normally take on the parent row. That
skips the row-lock write, the WAL for it, and avoids forming
[MultiXact IDs](https://www.postgresql.org/docs/current/routine-vacuuming.html#VACUUM-FOR-MULTIXACT-WRAPAROUND)
under concurrent access.

## What's here

| Use case | Lookup table | Fact table | Scenario |
|---|---|---|---|
| `usecase1_order_status` | `order_status` (8 rows) | `orders` | Checkout traffic hammering a shared order-status lookup table |
| `usecase3_device_telemetry` | `device_type` (15 rows) | `telemetry_events` | High-volume IoT ingest referencing a device-type dimension table |

Each app:

1. Bulk-loads ~50MB of synthetic fact-table data (via `COPY`)
2. Runs a concurrent insert workload (many workers, multi-row batched
   inserts) against the lookup table while it's plain `heap`
3. Scores that access pattern with a ported version of the
   `refdata_advisor` recommendation model from the product demo
   (`ALTER TABLE ... SET ACCESS METHOD refdata;` if warranted)
4. Converts the lookup table to `refdata`
5. Runs the identical workload again
6. Reports throughput, and direct evidence of FK lock contention
   (MultiXacts) before vs. after

## Requirements

- Python 3.12+
- A reachable PostgreSQL instance with the `refdata` extension
  available (this was built and tested against EDB Postgres Extended
  Server 18 with EDB's Advanced Storage Pack). The connecting role
  needs `CREATE` on the `public` schema and permission to
  `CREATE EXTENSION refdata`.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Set `A1P_DATABASE_URL` to a `postgres://` connection URI, e.g.:

```bash
export A1P_DATABASE_URL="postgres://user:password@host:port/dbname?sslmode=require"
```

## Running a demo from the CLI

```bash
./.venv/bin/python usecase1_order_status/app.py
./.venv/bin/python usecase3_device_telemetry/app.py
```

Both scripts drop and recreate their own tables on every run, so
they're safe to re-run repeatedly. Optional flags (same defaults on
both):

| Flag | Default | Meaning |
|---|---|---|
| `--bulk-mb` | 50 | Size of the synthetic fact-table dataset to load |
| `--workers` | 16 | Concurrent workers in the benchmark phase |
| `--batch-size` | 500 | Rows per multi-row `INSERT` (one transaction) |
| `--batches-per-worker` | 10 | Batches each worker runs |

Run in a real terminal to get a live-updating progress bar during the
bulk load; when piped (e.g. by the web UI below) it emits structured
`PROGRESS_JSON` lines instead.

Tables created live in `public` as `demo1_order_status` /
`demo1_orders` and `demo3_device_type` / `demo3_telemetry_events`
(the connecting role in this setup doesn't have `CREATE SCHEMA`
privilege, only `CREATE` on `public`).

## Running the web UI

```bash
./.venv/bin/python webapp/app.py
```

Open `http://127.0.0.1:5050`. Pick a use case, optionally adjust the
same parameters as the CLI flags above, and hit **Run demo**. The job
page shows:

- A live status banner and progress bar while data loads
- A `refdata_advisor`-style recommendation card, scored from the
  heap-phase access pattern actually observed
- Bar charts comparing heap vs. refdata throughput and lock
  contention
- A time-series chart of MultiXact formation on the lookup table
  over the course of each phase
- The full console output of the underlying script

## Reading the results

- **Throughput (rows/sec)** -- should typically be a few percent
  higher under `refdata`, consistent with EDB's documented 5-10% gain
  for this workload shape.
- **MultiXact pressure / "rows carrying a live MultiXact"** -- this is
  the real, mechanistic evidence of what `refdata` avoids. A row's
  `xmax` becomes a MultiXact ID when multiple transactions
  concurrently hold a `FOR KEY SHARE` lock on it (what an FK check
  takes on a heap parent row). The harness samples this continuously
  during each phase (via `pg_get_multixact_members`, since there's no
  plain-SQL way to check this without superuser-only `pageinspect`)
  and keeps the peak, because a single end-of-run check can miss
  contention that already got overwritten by a later, uncontended
  lock. Under `heap` this is reliably non-zero; under `refdata` it's
  always zero, because the per-row lock is never taken at all.
