"""
app.py — L-DCM Crash Risk Analysis Dashboard (V2)

Endpoints
    GET  /                → serve map_report.html
    GET  /api/meta        → time range, data bounds, label maps
    GET  /api/trajectory  → sampled waypoint arrays per vehicle
    GET  /api/accidents   → collision events with severity
    GET  /api/analytics   → event breakdown, crash freq, before/after, risk
    POST /api/predict     → prediction model stub

Run:  .venv\\Scripts\\uvicorn app:app --reload --port 8000
"""

import math, os, json, re
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db_connection import get_connection

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="L-DCM Crash Risk Analysis API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_DIR = os.path.dirname(__file__)

# ── Precise Spatial Boundary Geometry (Kamphaeng Phet 6 Rd) ──
def _load_road_polygons():
    path = os.path.join(_DIR, "road.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
                return [s["polygon"] for s in data.get("sections", [])]
        except Exception as e:
            print(f"[app] Error loading road.json polygons: {e}")
    return []

_ROAD_POLYGONS = _load_road_polygons()

def _point_in_polygon(lat, lon, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        j = (i - 1) % n
        yi, xi = poly[i][0], poly[i][1]
        yj, xj = poly[j][0], poly[j][1]
        intersect = ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)
        if intersect:
            inside = not inside
    return inside

# ── DuckDB local mode ─────────────────────────────────────────
# sensor_local.duckdb is auto-detected in the same folder.
# Build it once with:  python export_to_duckdb.py
# Or set LOCAL_DB_PATH in .env to point elsewhere.
_LOCAL_DB  = os.getenv("LOCAL_DB_PATH", os.path.join(_DIR, "sensor_local.duckdb"))

# ── Cloud DB Downloader (Bypasses 1 GB Git limit on Hugging Face Spaces) ──────
_DOWNLOAD_URL = os.getenv("DATABASE_DOWNLOAD_URL")
if not os.path.exists(_LOCAL_DB) and _DOWNLOAD_URL:
    print(f"[app] local database file not found. Initiating cloud download from secret storage...")
    try:
        import urllib.request, sys
        def _download_progress(blocknum, blocksize, totalsize):
            readsofar = blocknum * blocksize
            if totalsize > 0:
                percent = min(100.0, readsofar * 100.0 / totalsize)
                sys.stdout.write(f"\r[app] Download Progress: {percent:.1f}% ({readsofar//(1024*1024)} MB / {totalsize//(1024*1024)} MB)")
                sys.stdout.flush()
            else:
                sys.stdout.write(f"\r[app] Downloaded: {readsofar//(1024*1024)} MB")
                sys.stdout.flush()
        
        temp_download_path = _LOCAL_DB + ".downloading"
        urllib.request.urlretrieve(_DOWNLOAD_URL, temp_download_path, _download_progress)
        os.rename(temp_download_path, _LOCAL_DB)
        print(f"\n[app] Database download completed successfully!")
    except Exception as e:
        print(f"\n[app] ERROR downloading database: {e}")

_USE_DUCK  = os.path.exists(_LOCAL_DB)

if _USE_DUCK:
    import duckdb as _duckdb
    print(f"[app] LOCAL mode — DuckDB: {_LOCAL_DB}")
else:
    print("[app] REMOTE mode — PostgreSQL")


class _DuckCursor:
    """Wraps a DuckDB connection so it looks like a psycopg2 cursor to the rest of the code."""
    def __init__(self, conn):
        self._conn = conn
        self._res = None

    def execute(self, sql, params=()):
        # 1. psycopg2 uses %s → DuckDB uses ?
        # 2. psycopg2 escapes literal % as %% → unescape to %
        duck_sql = sql.replace("%%", "%").replace("%s", "?")

        # 3. DuckDB SET statements are silently ignored (no timeout support)
        if duck_sql.strip().upper().startswith("SET"):
            return

        # 4. PostgreSQL TABLESAMPLE SYSTEM(n) → DuckDB USING SAMPLE in subquery
        #    "FROM tbl TABLESAMPLE SYSTEM(1)" → "FROM (SELECT * FROM tbl USING SAMPLE 1 PERCENT) _s"
        duck_sql = re.sub(
            r'FROM\s+(\w+)\s+TABLESAMPLE\s+SYSTEM\s*\(\s*\d+\s*\)',
            r'FROM (SELECT * FROM \1 USING SAMPLE 1 PERCENT) _s',
            duck_sql, flags=re.IGNORECASE
        )

        self._res = self._conn.execute(duck_sql, list(params))

    def fetchone(self):
        return self._res.fetchone() if self._res else None

    def fetchall(self):
        return self._res.fetchall() if self._res else []

    def close(self):
        pass


class _DuckConn:
    """Thin shim so _conn() always returns something with .cursor() and .close()."""
    def __init__(self):
        self._conn = _duckdb.connect(_LOCAL_DB, read_only=True)

    def cursor(self):
        return _DuckCursor(self._conn)

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def _conn():
    if _USE_DUCK:
        return _DuckConn()
    c = get_connection()
    c.autocommit = True
    return c

# ── Label maps (human-readable) ──────────────────────────────
EVENT_LABELS = {1: "Harsh Braking", 2: "Sudden Acceleration", 3: "Sharp Turn"}
COLLISION_LABELS = {
    16: "Front-Back Collision (filter OFF)",
    17: "Front-Back Collision (Driving)",
    18: "Front-Back Collision (Idling)",
    32: "Side Collision (filter OFF)",
    33: "Side Collision (Driving)",
    34: "Side Collision (Idling)",
}

def _dir_label(deg):
    if deg is None: return None
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg / 22.5) % 16]

def _severity(gx):
    """Collision severity from |gx_acci|."""
    if gx is None: return "unknown"
    a = abs(gx)
    if a <= 3: return "low"
    if a <= 4: return "medium"
    return "high"

# ── Routes ────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def serve_ui():
    return FileResponse(os.path.join(_DIR, "map_report.html"))

@app.get("/style.css", include_in_schema=False)
def serve_css():
    return FileResponse(os.path.join(_DIR, "style.css"), media_type="text/css")

@app.get("/dashboard.js", include_in_schema=False)
def serve_js():
    return FileResponse(os.path.join(_DIR, "dashboard.js"), media_type="application/javascript")


@app.get("/api/meta")
def get_meta():
    """Time range, spatial bounds, label dictionaries, and a suggested start time."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '30000'")
    cur.execute("""
        SELECT MIN(timestamp), MAX(timestamp),
               MIN(lat)::float, MAX(lat)::float,
               MIN(lon)::float, MAX(lon)::float
        FROM sensor
    """)
    row = cur.fetchone()
    if not row or not row[0]:
        cur.close(); conn.close()
        raise HTTPException(500, "Empty dataset")

    t_min, t_max = row[0], row[1]

    # Find the busiest 10-minute window using a fast tablesample scan.
    # TABLESAMPLE SYSTEM(1) reads ~1% of pages — very fast on large tables.
    # This gives us a good starting point so the animation opens on live traffic,
    # not an empty early-morning stretch.
    try:
        cur.execute("SET statement_timeout = '10000'")
        cur.execute("""
            SELECT DATE_TRUNC('hour', timestamp) +
                   (FLOOR(EXTRACT(MINUTE FROM timestamp) / 10) * interval '10 minutes') AS window,
                   COUNT(*) AS cnt
            FROM sensor TABLESAMPLE SYSTEM(1)
            GROUP BY 1
            ORDER BY 2 DESC
            LIMIT 1
        """)
        best = cur.fetchone()
        t_suggested = best[0].isoformat() + "Z" if best else (t_min.isoformat() + "Z")
    except Exception:
        # Fallback: midpoint of the full date range
        t_suggested = (t_min + (t_max - t_min) / 2).isoformat() + "Z"

    cur.close(); conn.close()
    return {
        # All timestamps include "Z" so the browser treats them as UTC regardless
        # of the client's local timezone (critical for Bangkok UTC+7 users).
        "t_start":     t_min.isoformat() + "Z",
        "t_end":       t_max.isoformat() + "Z",
        "t_suggested": t_suggested,   # frontend should start here, not at t_start
        "bounds": {"lat_min": row[2], "lat_max": row[3], "lon_min": row[4], "lon_max": row[5]},
        "event_labels": EVENT_LABELS,
        "collision_labels": COLLISION_LABELS,
    }


@app.get("/api/trajectory")
def get_trajectory(
    t_start: str   = Query(..., description="ISO start of window"),
    t_end:   str   = Query(..., description="ISO end of window"),
    lat_min: float = Query(...), lat_max: float = Query(...),
    lon_min: float = Query(...), lon_max: float = Query(...),
    sample_sec: int = Query(2, ge=1, le=30, description="Sample every N seconds"),
):
    """Sampled waypoints per vehicle for smooth frontend animation.

    Uses UNION ALL so normal cars (Basic stream — no event/collision flags) are
    never crowded out by event rows.  Each branch gets its own LIMIT budget:
      • Normal rows  → up to 9 000 rows (modulo-sampled, every sample_sec seconds)
      • Event rows   → up to 3 000 rows (always included, no modulo gate)
    """
    conn = _conn()
    cur  = conn.cursor()
    cur.execute("SET statement_timeout = '15000'")
    cur.execute("""
        -- Branch 1: normal driving (Basic 0x11 stream).
        -- Modulo-sample so we don't overwhelm the wire; these cars have no
        -- event_type or collision_type so the old single-query LIMIT starved them.
        (SELECT vin, timestamp, lat::float, lon::float, direction,
                vehicle_speed, event_type, collision_type, gx_acci
         FROM sensor
         WHERE timestamp BETWEEN %s AND %s
           AND lat BETWEEN %s AND %s
           AND lon BETWEEN %s AND %s
           AND event_type    IS NULL
           AND collision_type IS NULL
           AND EXTRACT(EPOCH FROM timestamp)::bigint %% %s = 0
         ORDER BY vin, timestamp
         LIMIT 9000)

        UNION ALL

        -- Branch 2: PHYD events (0x21) and Accident events (0x32).
        -- Always include every event row regardless of modulo.
        (SELECT vin, timestamp, lat::float, lon::float, direction,
                vehicle_speed, event_type, collision_type, gx_acci
         FROM sensor
         WHERE timestamp BETWEEN %s AND %s
           AND lat BETWEEN %s AND %s
           AND lon BETWEEN %s AND %s
           AND (event_type IS NOT NULL OR collision_type IS NOT NULL)
         ORDER BY vin, timestamp
         LIMIT 3000)

        ORDER BY vin, timestamp
    """, (
        # Branch 1 params
        t_start, t_end, lat_min, lat_max, lon_min, lon_max, sample_sec,
        # Branch 2 params
        t_start, t_end, lat_min, lat_max, lon_min, lon_max,
    ))
    rows = cur.fetchall()
    cur.close(); conn.close()

    # Identify VINs that have at least one non-null telemetry value across their
    # waypoints.  The seven fields the user cares about are:
    #   vehicle_speed, event_type, collision_type, gx_acci, gy_acci, gx_phyd, gy_phyd
    # The trajectory query fetches vehicle_speed (spd), event_type (evt),
    # collision_type (col), and gx_acci (gx).  The remaining three (gy_acci,
    # gx_phyd, gy_phyd) are always co-null with the ones we already have:
    #   • gy_acci is non-null iff gx_acci is non-null  (same collision burst)
    #   • gx_phyd / gy_phyd are non-null iff event_type is non-null
    # So checking (spd, evt, col, gx) is sufficient to cover all seven.
    vins_with_telemetry: set[str] = {
        vin for vin, _, _, _, _, spd, evt, col, gx in rows
        if any(x is not None for x in (spd, evt, col, gx))
    }

    trajs: dict[str, list] = {}
    for vin, ts, la, lo, d, spd, evt, col, gx in rows:
        if vin not in vins_with_telemetry:
            continue   # skip pure-GPS vehicles with no useful telemetry
        trajs.setdefault(vin, []).append({
            "t":   ts.isoformat() + "Z",  # UTC-explicit so browser parses correctly
            "la":  la, "lo": lo,
            "d":   d,
            "s":   spd,
            "e":   evt,
            "c":   col,
            "g":   gx,
        })
    skipped = len({r[0] for r in rows}) - len(trajs)
    print(f"[trajectory] returned {len(rows)} rows for {len(trajs)} vehicles "
          f"({skipped} pure-GPS vehicles filtered out)")
    return {"vehicle_count": len(trajs), "trajectories": trajs}


@app.get("/api/accidents")
def get_accidents(
    lat_min: float = Query(...), lat_max: float = Query(...),
    lon_min: float = Query(...), lon_max: float = Query(...),
    t_start: str = Query(...), t_end: str = Query(...),
):
    """Collision events with severity and human labels."""
    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '12000'")
    cur.execute("""
        SELECT vin, timestamp, lat::float, lon::float,
               collision_type, gx_acci, gy_acci, vehicle_speed
        FROM sensor
        WHERE collision_type IS NOT NULL
          AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
          AND timestamp BETWEEN %s AND %s
        ORDER BY timestamp
        LIMIT 500
    """, (lat_min, lat_max, lon_min, lon_max, t_start, t_end))
    rows = cur.fetchall()
    cur.close(); conn.close()

    accidents = []
    vin_first: dict[str, str] = {}
    for vin, ts, la, lo, ct, gx, gy, spd in rows:
        ts_str = ts.isoformat() + "Z"  # UTC-explicit
        accidents.append({
            "vin": vin, "timestamp": ts_str,
            "lat": la, "lon": lo,
            "collision_type": ct,
            "collision_label": COLLISION_LABELS.get(ct, f"Type {ct}"),
            "severity": _severity(gx),
            "gx": gx, "gy": gy, "speed": spd,
        })
        if vin not in vin_first:
            vin_first[vin] = ts_str
    return {"accidents": accidents, "accident_vins": vin_first}


@app.get("/api/analytics")
def get_analytics(
    lat_min: float = Query(...), lat_max: float = Query(...),
    lon_min: float = Query(...), lon_max: float = Query(...),
    t_start: Optional[str] = Query(None),
    t_end:   Optional[str] = Query(None),
    countermeasure_date: Optional[str] = Query(None),
):
    """Event breakdown, crash frequency, before/after, risk score."""
    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '25000'")
    bp = (lat_min, lat_max, lon_min, lon_max)
    time_sql = "AND timestamp BETWEEN %s AND %s" if (t_start and t_end) else ""
    tp = (t_start, t_end) if (t_start and t_end) else ()

    # 1. Crash frequency (daily) — fill every date with 0 so the chart
    #    x-axis has no unexplained gaps between days with events.
    cur.execute(f"""
        SELECT DATE_TRUNC('day', timestamp)::date, COUNT(*)
        FROM sensor WHERE collision_type IS NOT NULL
          AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s {time_sql}
        GROUP BY 1 ORDER BY 1 LIMIT 365
    """, bp + tp)
    freq_raw = cur.fetchall()
    # Build a dense date series
    from datetime import date as _date
    freq_map = {r[0]: r[1] for r in freq_raw}
    if t_start and t_end:
        d_lo = datetime.fromisoformat(t_start.replace("Z","")).date()
        d_hi = datetime.fromisoformat(t_end.replace("Z","")).date()
    elif freq_map:
        d_lo, d_hi = min(freq_map.keys()), max(freq_map.keys())
    else:
        d_lo = d_hi = datetime.utcnow().date()
    crash_freq, cur_day = [], d_lo
    while cur_day <= d_hi:
        crash_freq.append({"date": str(cur_day), "count": int(freq_map.get(cur_day, 0))})
        cur_day += timedelta(days=1)

    # 1b. Daily driving events — powers the per-day risk score that updates
    #     as playback advances through the timeline.  Same dense back-fill as
    #     crash_freq so the two series align one-to-one on date.
    cur.execute(f"""
        SELECT DATE_TRUNC('day', timestamp)::date, COUNT(*)
        FROM sensor WHERE event_type IS NOT NULL
          AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s {time_sql}
        GROUP BY 1 ORDER BY 1 LIMIT 365
    """, bp + tp)
    ev_raw = cur.fetchall()
    ev_map = {r[0]: r[1] for r in ev_raw}
    daily_events, cur_day = [], d_lo
    while cur_day <= d_hi:
        daily_events.append({"date": str(cur_day), "count": int(ev_map.get(cur_day, 0))})
        cur_day += timedelta(days=1)

    # 2. Event breakdown (FIXED: actual values 1, 2, 3)
    cur.execute(f"""
        SELECT
          SUM(CASE WHEN event_type = 1 THEN 1 ELSE 0 END),
          SUM(CASE WHEN event_type = 2 THEN 1 ELSE 0 END),
          SUM(CASE WHEN event_type = 3 THEN 1 ELSE 0 END),
          SUM(CASE WHEN collision_type IS NOT NULL THEN 1 ELSE 0 END)
        FROM sensor
        WHERE (event_type IS NOT NULL OR collision_type IS NOT NULL)
          AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s {time_sql}
    """, bp + tp)
    eb = cur.fetchone()
    hb, sa, st, co = (eb[0] or 0), (eb[1] or 0), (eb[2] or 0), (eb[3] or 0)
    tot = max(hb + sa + st + co, 1)
    event_breakdown = {
        "harsh_brake":   {"count": hb, "pct": round(hb/tot*100), "label": "Harsh Braking"},
        "sudden_accel":  {"count": sa, "pct": round(sa/tot*100), "label": "Sudden Acceleration"},
        "sharp_turn":    {"count": st, "pct": round(st/tot*100), "label": "Sharp Turn"},
        "collision":     {"count": co, "pct": round(co/tot*100), "label": "Collision"},
    }

    # 3. Before / After
    before_after = None
    if countermeasure_date:
        cur.execute("""
            SELECT
              SUM(CASE WHEN timestamp < %s THEN 1 ELSE 0 END),
              SUM(CASE WHEN timestamp >= %s THEN 1 ELSE 0 END)
            FROM sensor WHERE collision_type IS NOT NULL
              AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
        """, (countermeasure_date, countermeasure_date) + bp)
        row = cur.fetchone()
        bc, ac = (row[0] or 0), (row[1] or 0)
        # Also count driving events
        cur.execute("""
            SELECT
              SUM(CASE WHEN timestamp < %s THEN 1 ELSE 0 END),
              SUM(CASE WHEN timestamp >= %s THEN 1 ELSE 0 END)
            FROM sensor WHERE event_type IS NOT NULL
              AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
        """, (countermeasure_date, countermeasure_date) + bp)
        erow = cur.fetchone()
        eb_before, eb_after = (erow[0] or 0), (erow[1] or 0)
        before_after = {
            "before": {"crashes": bc, "events": eb_before},
            "after":  {"crashes": ac, "events": eb_after},
            "countermeasure_date": countermeasure_date,
        }

    # 4. Risk score — weighted by collision rate + event rate.
    #    Old formula (events/days * 0.05) always saturated at 10.0 because
    #    a 700 m² study area logs 1 400+ events/day.  New formula:
    #      base   = collision rate * 10  (0.4 crashes/day → 4.0 pts)
    #      bonus  = event rate * 0.001   (1 400 events/day → 1.4 pts)
    #    Typical result: ~5.5 (MEDIUM) — leaves headroom for worse sites.
    if t_start and t_end:
        days = max((datetime.fromisoformat(t_end.replace("Z","")) - datetime.fromisoformat(t_start.replace("Z",""))).days, 1)
    else:
        days = 60  # safe fallback
    collision_rate = co / days
    event_rate = (hb + sa + st) / days
    risk_score = round(min(10.0, collision_rate * 10 + event_rate * 0.001), 1)

    cur.close(); conn.close()
    return {
        "crash_frequency": crash_freq,
        "daily_events":    daily_events,
        "event_breakdown": event_breakdown,
        "before_after":    before_after,
        "risk_score":      risk_score,   # kept for backwards-compat / whole-range view
    }


@app.get("/api/vehicle-trajectory")
def get_vehicle_trajectory(
    vin: str = Query(..., description="Vehicle VIN to retrieve"),
    t_center: str = Query(..., description="ISO timestamp to centre the window on"),
    window_minutes: int = Query(5, ge=1, le=60, description="Half-window in minutes"),
):
    """Full trajectory of a single vehicle around a time centre.

    Returns every sensor row (no sampling) for detailed replay during
    collision investigation.  The window is ±window_minutes around t_center.
    """
    center_dt = datetime.fromisoformat(t_center.replace("Z", ""))
    t_lo = center_dt - timedelta(minutes=window_minutes)
    t_hi = center_dt + timedelta(minutes=window_minutes)

    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '15000'")
    cur.execute("""
        SELECT timestamp, lat::float, lon::float, direction,
               vehicle_speed, event_type, collision_type, gx_acci
        FROM sensor
        WHERE vin = %s AND timestamp BETWEEN %s AND %s
        ORDER BY timestamp
        LIMIT 5000
    """, (vin, t_lo.isoformat(), t_hi.isoformat()))
    rows = cur.fetchall()
    cur.close(); conn.close()

    waypoints = [{
        "t": r[0].isoformat() + "Z",
        "la": r[1], "lo": r[2], "d": r[3],
        "s": r[4], "e": r[5], "c": r[6], "g": r[7],
    } for r in rows]

    return {
        "vin": vin,
        "waypoints": waypoints,
        "collision_at": t_center,
        "window": {"from": t_lo.isoformat() + "Z", "to": t_hi.isoformat() + "Z"},
    }


@app.get("/api/blackspots")
def get_blackspots():
    """Return the configured blackspot list from blackspots.json."""
    path = os.path.join(_DIR, "blackspots.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


@app.get("/api/heatmap")
def get_heatmap(
    lat_min: float = Query(...), lat_max: float = Query(...),
    lon_min: float = Query(...), lon_max: float = Query(...),
    event_type:    int   = Query(0,  description="0=all events, 1=braking, 2=accel, 3=turning"),
    speed_bracket: int   = Query(0,  description="0=all, 1=0-60 km/h, 2=61-100, 3=100+"),
    hour:          int   = Query(-1, description="-1=all, 0-23=local Bangkok hour (UTC+7)"),
):
    """Heatmap point cloud for the chosen event-type / speed-bracket / hour filter.

    Returns raw (lat, lon) pairs for event rows inside the bbox so Leaflet.heat
    can render a kernel-density heatmap in the browser.  At most 60 000 points
    are returned; the caller should display a warning if total exceeds this.
    """
    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '30000'")

    # --- Dynamic filter clauses (all inline — no user-controlled interpolation) ---
    if event_type == 0:
        event_sql = "AND event_type IS NOT NULL"
    else:
        # event_type is already validated as int by FastAPI; safe to inline
        event_sql = f"AND event_type = {event_type}"

    if speed_bracket == 1:
        speed_sql = "AND vehicle_speed BETWEEN 0 AND 60"
    elif speed_bracket == 2:
        speed_sql = "AND vehicle_speed BETWEEN 61 AND 100"
    elif speed_bracket == 3:
        speed_sql = "AND vehicle_speed > 100"
    else:
        speed_sql = ""   # 0 = all speeds (include NULL)

    if 0 <= hour <= 23:
        # Bangkok is UTC+7; add 7 hours before extracting the hour
        hour_sql = f"AND CAST(EXTRACT(HOUR FROM timestamp + INTERVAL '7' HOUR) AS INTEGER) = {hour}"
    else:
        hour_sql = ""    # -1 = all hours

    cur.execute(f"""
        SELECT lat::float, lon::float
        FROM sensor
        WHERE lat  BETWEEN %s AND %s
          AND lon  BETWEEN %s AND %s
          {event_sql}
          {speed_sql}
          {hour_sql}
        LIMIT 60000
    """, (lat_min, lat_max, lon_min, lon_max))

    rows = cur.fetchall()
    cur.close(); conn.close()

    # Filter points precisely inside our oriented road section polygons
    filtered_points = []
    for r in rows:
        lat, lon = r[0], r[1]
        in_zone = False
        for poly in _ROAD_POLYGONS:
            if _point_in_polygon(lat, lon, poly):
                in_zone = True
                break
        if in_zone:
            filtered_points.append([lat, lon])

    return {
        "points": filtered_points,
        "total":  len(filtered_points),
        "capped": len(rows) == 60000,
        "filter": {
            "event_type":    event_type,
            "speed_bracket": speed_bracket,
            "hour":          hour,
        },
    }


@app.get("/api/road")
def get_road():
    """Return the focused-road configuration (Kamphaeng Phet 6 Rd) from road.json.

    Used by the frontend's Road-Focus mode to draw section rectangles and
    fan out per-section analytics fetches.
    """
    path = os.path.join(_DIR, "road.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"name": "", "sections": []}


@app.get("/api/debug")
def get_debug():
    """Quick health-check: confirms DB is reachable and returns key data stats.
    Visit http://localhost:8000/api/debug in the browser to diagnose issues."""
    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '20000'")

    cur.execute("SELECT COUNT(*) FROM sensor")
    total = cur.fetchone()[0]

    cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM sensor")
    t_min, t_max = cur.fetchone()

    cur.execute("SELECT COUNT(*) FROM sensor WHERE event_type IS NOT NULL")
    evt_rows = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM sensor WHERE collision_type IS NOT NULL")
    col_rows = cur.fetchone()[0]

    # 5 sample rows near the data midpoint
    mid = t_min + (t_max - t_min) / 2
    cur.execute("""
        SELECT vin, timestamp, lat::float, lon::float, direction,
               vehicle_speed, event_type, collision_type
        FROM sensor WHERE timestamp BETWEEN %s AND %s
        LIMIT 5
    """, (mid, mid + timedelta(minutes=10)))
    samples = [
        {"vin": r[0], "ts": r[1].isoformat()+"Z", "lat": r[2], "lon": r[3],
         "dir": r[4], "spd": r[5], "evt": r[6], "col": r[7]}
        for r in cur.fetchall()
    ]

    cur.close(); conn.close()
    return {
        "status": "ok",
        "total_rows": total,
        "event_rows": evt_rows,
        "collision_rows": col_rows,
        "t_min": t_min.isoformat() + "Z",
        "t_max": t_max.isoformat() + "Z",
        "sample_rows_near_midpoint": samples,
    }


@app.post("/api/predict")
async def predict(request: Request):
    """Stub for prediction model. Returns dummy risk zones.

    Expected input (from ML teammate):
        {"vehicles": [{"vin": ..., "lat": ..., "lon": ..., "speed": ..., "direction": ...}, ...]}
    Expected output:
        {"predictions": [{"lat": ..., "lon": ..., "risk": 0-1, "label": "..."}, ...], "model_ready": false}
    """
    return {
        "predictions": [],
        "model_ready": False,
        "message": "Prediction model not yet connected. See PREDICTION_API.md for integration guide.",
    }
