"""
export_to_duckdb.py
-------------------
Exports the `sensor` table from the remote PostgreSQL database into a local
DuckDB file.  Once the file exists, the backend can use it directly — no
network connection required.

Usage
-----
    pip install duckdb psycopg2-binary python-dotenv
    python export_to_duckdb.py

    # Optional: export a specific date range to keep the file small
    python export_to_duckdb.py --start 2025-03-01 --end 2025-03-31

    # Export only the study area bounding box
    python export_to_duckdb.py --lat-min 13.83 --lat-max 13.86 --lon-min 100.54 --lon-max 100.58

Output
------
Creates  sensor_local.duckdb  in the same folder as this script.
The backend (app.py) automatically uses it if it exists.

Why DuckDB?
-----------
- Column-oriented: lat/lon/timestamp range scans are very fast
- No server process needed — just open the .duckdb file
- 5–10× faster query times than PostgreSQL on analytical filters
- Compressed on disk (~3–5 GB for 91M rows of mostly numeric data)
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

try:
    import duckdb
except ImportError:
    print("DuckDB not installed. Run:  pip install duckdb")
    sys.exit(1)

try:
    import psycopg2
except ImportError:
    print("psycopg2 not installed. Run:  pip install psycopg2-binary")
    sys.exit(1)


# ── CLI args ──────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Export sensor table → DuckDB")
parser.add_argument("--start",   default=None, help="Start date YYYY-MM-DD (default: all)")
parser.add_argument("--end",     default=None, help="End date YYYY-MM-DD (default: all)")
parser.add_argument("--lat-min", type=float, default=None)
parser.add_argument("--lat-max", type=float, default=None)
parser.add_argument("--lon-min", type=float, default=None)
parser.add_argument("--lon-max", type=float, default=None)
parser.add_argument("--batch",   type=int, default=100_000, help="Rows per batch (default 100k)")
parser.add_argument("--out",     default="sensor_local.duckdb", help="Output DuckDB file path")
args = parser.parse_args()

OUT_PATH = os.path.join(os.path.dirname(__file__), args.out)

# ── Build WHERE clause ────────────────────────────────────────────────────────
filters, params = [], []
if args.start:
    filters.append("timestamp >= %s"); params.append(args.start)
if args.end:
    filters.append("timestamp < %s"); params.append(args.end)
if args.lat_min is not None:
    filters.append("lat >= %s"); params.append(args.lat_min)
if args.lat_max is not None:
    filters.append("lat <= %s"); params.append(args.lat_max)
if args.lon_min is not None:
    filters.append("lon >= %s"); params.append(args.lon_min)
if args.lon_max is not None:
    filters.append("lon <= %s"); params.append(args.lon_max)

where = ("WHERE " + " AND ".join(filters)) if filters else ""
count_sql  = f"SELECT COUNT(*) FROM sensor {where}"
select_sql = f"""
    SELECT id, vin, timestamp, lat::float8, lon::float8,
           direction, gy_phyd, gx_phyd, gy_acci, gx_acci,
           event_type, collision_type, vehicle_speed
    FROM sensor {where}
    ORDER BY timestamp
"""

# ── Connect to PostgreSQL ─────────────────────────────────────────────────────
print("Connecting to PostgreSQL…")
pg = psycopg2.connect(
    host=os.getenv("DB_HOST"), port=int(os.getenv("DB_PORT", 5432)),
    dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"), connect_timeout=15,
)
pg.autocommit = True
cur = pg.cursor()
cur.execute("SET statement_timeout = '0'")   # no timeout for bulk export

cur.execute(count_sql, params)
total = cur.fetchone()[0]
print(f"Rows to export: {total:,}")
if total == 0:
    print("Nothing to export with the given filters. Exiting.")
    sys.exit(0)

# ── Create DuckDB ─────────────────────────────────────────────────────────────
print(f"Creating DuckDB file: {OUT_PATH}")
duck = duckdb.connect(OUT_PATH)
duck.execute("""
    CREATE TABLE IF NOT EXISTS sensor (
        id              INTEGER,
        vin             VARCHAR,
        timestamp       TIMESTAMP,
        lat             DOUBLE,
        lon             DOUBLE,
        direction       INTEGER,
        gy_phyd         INTEGER,
        gx_phyd         INTEGER,
        gy_acci         INTEGER,
        gx_acci         INTEGER,
        event_type      INTEGER,
        collision_type  INTEGER,
        vehicle_speed   INTEGER
    )
""")
duck.execute("CREATE INDEX IF NOT EXISTS idx_ts  ON sensor (timestamp)")
duck.execute("CREATE INDEX IF NOT EXISTS idx_lat ON sensor (lat)")
duck.execute("CREATE INDEX IF NOT EXISTS idx_lon ON sensor (lon)")
duck.execute("CREATE INDEX IF NOT EXISTS idx_vin ON sensor (vin)")

# ── Stream rows in batches ────────────────────────────────────────────────────
print(f"Streaming in batches of {args.batch:,}…")
cur.execute(select_sql, params)

COLS = ["id","vin","timestamp","lat","lon","direction",
        "gy_phyd","gx_phyd","gy_acci","gx_acci",
        "event_type","collision_type","vehicle_speed"]

exported = 0
t0 = time.time()
while True:
    batch = cur.fetchmany(args.batch)
    if not batch:
        break
    duck.executemany("INSERT INTO sensor VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    exported += len(batch)
    elapsed = time.time() - t0
    rate = exported / elapsed if elapsed > 0 else 0
    pct  = exported / total * 100
    print(f"  {exported:>10,} / {total:,}  ({pct:.1f}%)  {rate:,.0f} rows/s", end="\r")

print(f"\nDone. Exported {exported:,} rows in {time.time()-t0:.1f}s")
print(f"DuckDB file: {OUT_PATH}")

duck.close(); cur.close(); pg.close()
print("\nTo use locally, set  LOCAL_DB_PATH=sensor_local.duckdb  in your .env")
