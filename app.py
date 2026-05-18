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
def _load_road_sections():
    path = os.path.join(_DIR, "road.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f).get("sections", [])
        except Exception as e:
            print(f"[app] Error loading road.json: {e}")
    return []

_ROAD_SECTIONS = _load_road_sections()
_ROAD_POLYGONS = [s["polygon"] for s in _ROAD_SECTIONS]

# Combined bbox of all sections — used to gate polygon filtering in analytics.
if _ROAD_SECTIONS:
    _ROAD_BBOX = {
        "lat_min": min(s["lat_min"] for s in _ROAD_SECTIONS),
        "lat_max": max(s["lat_max"] for s in _ROAD_SECTIONS),
        "lon_min": min(s["lon_min"] for s in _ROAD_SECTIONS),
        "lon_max": max(s["lon_max"] for s in _ROAD_SECTIONS),
    }
else:
    _ROAD_BBOX = None


def _relevant_polygons(lat_min: float, lat_max: float,
                       lon_min: float, lon_max: float) -> list:
    """Return the road polygon(s) relevant for this request bbox.

    Tight tolerance (0.0001 deg ~ 11 m) matches an exact section bbox.
    If exactly one section matches → use only that section's polygon so
    per-section analytics are not inflated by the adjacent-section overlap
    zone (sections B & C share bbox values within 0.001 deg of each other,
    which broke the earlier looser tolerance).
    If zero or multiple sections match → fall back to all polygons
    (combined road view or circle/general view).
    """
    _TOL = 0.0001
    matches = [
        s for s in _ROAD_SECTIONS
        if abs(s["lat_min"] - lat_min) <= _TOL
        and abs(s["lat_max"] - lat_max) <= _TOL
        and abs(s["lon_min"] - lon_min) <= _TOL
        and abs(s["lon_max"] - lon_max) <= _TOL
    ]
    return [matches[0]["polygon"]] if len(matches) == 1 else _ROAD_POLYGONS

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

# ── DuckDB: download from Dropbox/URL if DATABASE_DOWNLOAD_URL is set ────────
def _maybe_download_duckdb() -> str | None:
    """Download sensor_local.duckdb from DATABASE_DOWNLOAD_URL secret if needed.

    Tries /data/ first (HF persistent storage — survives restarts),
    then /tmp/ (ephemeral fallback). Skips download if file already exists.
    Returns the local path on success, None otherwise.
    """
    import urllib.request

    url = os.getenv("DATABASE_DOWNLOAD_URL")
    if not url:
        return None

    destinations = ["/data/sensor_local.duckdb", "/tmp/sensor_local.duckdb"]

    for dest in destinations:
        # Already downloaded — reuse it
        if os.path.exists(dest):
            mb = os.path.getsize(dest) / 1_000_000
            print(f"[app] DuckDB already present at {dest} ({mb:.0f} MB) — skipping download")
            return dest

        dest_dir = os.path.dirname(dest)
        if not (os.path.isdir(dest_dir) and os.access(dest_dir, os.W_OK)):
            print(f"[app] {dest_dir} not writable, trying next location")
            continue

        print(f"[app] Downloading DuckDB → {dest} ...")
        try:
            def _log_progress(block_count, block_size, total_size):
                if total_size <= 0 or block_count % 500 != 0:
                    return
                done = min(block_count * block_size, total_size)
                pct  = done / total_size * 100
                print(f"[app]   {done / 1_000_000:.0f} / {total_size / 1_000_000:.0f} MB  ({pct:.0f}%)")

            urllib.request.urlretrieve(url, dest, reporthook=_log_progress)
            mb = os.path.getsize(dest) / 1_000_000
            print(f"[app] Download complete: {mb:.0f} MB → {dest}")
            return dest
        except Exception as exc:
            print(f"[app] Download to {dest} failed: {exc}")
            if os.path.exists(dest):
                os.remove(dest)   # remove partial file

    print("[app] DuckDB download failed — no writable destination found")
    return None


# ── DuckDB local mode ─────────────────────────────────────────
# Search order:
#   1. LOCAL_DB_PATH env var (explicit override)
#   2. DATABASE_DOWNLOAD_URL secret → download to /data/ or /tmp/
#   3. Same folder as app.py (local dev)
#   4. /data/sensor_local.duckdb (HF persistent storage, pre-placed)
_downloaded = _maybe_download_duckdb()
_DUCK_CANDIDATES = [
    os.getenv("LOCAL_DB_PATH"),
    _downloaded,
    os.path.join(_DIR, "sensor_local.duckdb"),
    "/data/sensor_local.duckdb",
]
_LOCAL_DB = next((p for p in _DUCK_CANDIDATES if p and os.path.exists(p)), None)
_USE_DUCK = _LOCAL_DB is not None

if _USE_DUCK:
    import duckdb as _duckdb
    print(f"[app] LOCAL mode — DuckDB: {_LOCAL_DB}")
else:
    checked = [p for p in _DUCK_CANDIDATES if p]
    print(f"[app] DuckDB not found (checked: {checked})")
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
               MIN(lat)::float8, MAX(lat)::float8,
               MIN(lon)::float8, MAX(lon)::float8
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
        (SELECT vin, timestamp, lat::float8, lon::float8, direction,
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
        (SELECT vin, timestamp, lat::float8, lon::float8, direction,
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
        SELECT vin, timestamp, lat::float8, lon::float8,
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
    """Event breakdown, crash frequency, before/after, risk score.

    Option B polygon fix: fetch all event/collision rows from the bbox once,
    then apply the same _point_in_polygon filter used by /api/heatmap.
    This eliminates the 1-2 count discrepancy caused by overlapping section
    bounding boxes double-counting events at section boundaries.
    Falls back to bbox-only when no road polygons are configured.
    """
    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '25000'")
    bp = (lat_min, lat_max, lon_min, lon_max)
    time_sql = "AND timestamp BETWEEN %s AND %s" if (t_start and t_end) else ""
    tp = (t_start, t_end) if (t_start and t_end) else ()

    # ── Single pre-fetch: all event/collision rows inside the bbox ──────────
    # bbox is the fast index pre-filter; polygon check below refines it.
    # Only event rows are fetched (~few thousand), never the full 91 M table.
    cur.execute(f"""
        SELECT timestamp, lat::float8, lon::float8, event_type, collision_type
        FROM sensor
        WHERE (event_type IS NOT NULL OR collision_type IS NOT NULL)
          AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s {time_sql}
        ORDER BY timestamp
    """, bp + tp)
    raw_rows = cur.fetchall()
    cur.close(); conn.close()

    # ── Polygon filter — gated by bbox match, uses only relevant polygon(s) ────
    # Circle/full view sends a bbox larger than the road extent → no filtering.
    # Road-focus view sends the road bbox or a single section bbox → filter with
    # only the matching polygon(s) so per-section counts stay exact.
    _TOL = 0.001
    _road_focused = (
        _ROAD_BBOX is not None
        and lat_min >= _ROAD_BBOX["lat_min"] - _TOL
        and lat_max <= _ROAD_BBOX["lat_max"] + _TOL
        and lon_min >= _ROAD_BBOX["lon_min"] - _TOL
        and lon_max <= _ROAD_BBOX["lon_max"] + _TOL
    )

    if _road_focused:
        polys = _relevant_polygons(lat_min, lat_max, lon_min, lon_max)
        rows = [
            (ts, la, lo, et, ct)
            for ts, la, lo, et, ct in raw_rows
            if any(_point_in_polygon(la, lo, p) for p in polys)
        ]
    else:
        rows = raw_rows   # circle/general view: all bbox events, no polygon trim

    # ── 1. Crash frequency & daily events (dense date fill) ─────────────────
    freq_map: dict = {}
    ev_map:   dict = {}
    for ts, _la, _lo, et, ct in rows:
        d = ts.date()
        if ct is not None:
            freq_map[d] = freq_map.get(d, 0) + 1
        if et is not None:
            ev_map[d]   = ev_map.get(d, 0) + 1

    if t_start and t_end:
        d_lo = datetime.fromisoformat(t_start.replace("Z", "")).date()
        d_hi = datetime.fromisoformat(t_end.replace("Z", "")).date()
    elif freq_map or ev_map:
        all_dates = list(freq_map) + list(ev_map)
        d_lo, d_hi = min(all_dates), max(all_dates)
    else:
        d_lo = d_hi = datetime.utcnow().date()

    crash_freq, daily_events, cur_day = [], [], d_lo
    while cur_day <= d_hi:
        crash_freq.append({"date": str(cur_day), "count": int(freq_map.get(cur_day, 0))})
        daily_events.append({"date": str(cur_day), "count": int(ev_map.get(cur_day, 0))})
        cur_day += timedelta(days=1)

    # ── 2. Event breakdown ───────────────────────────────────────────────────
    hb = sum(1 for _, _, _, et, _  in rows if et == 1)
    sa = sum(1 for _, _, _, et, _  in rows if et == 2)
    st = sum(1 for _, _, _, et, _  in rows if et == 3)
    co = sum(1 for _, _, _, _,  ct in rows if ct is not None)
    tot = max(hb + sa + st + co, 1)
    event_breakdown = {
        "harsh_brake":  {"count": hb, "pct": round(hb / tot * 100), "label": "Harsh Braking"},
        "sudden_accel": {"count": sa, "pct": round(sa / tot * 100), "label": "Sudden Acceleration"},
        "sharp_turn":   {"count": st, "pct": round(st / tot * 100), "label": "Sharp Turn"},
        "collision":    {"count": co, "pct": round(co / tot * 100), "label": "Collision"},
    }

    # ── 3. Before / After ────────────────────────────────────────────────────
    before_after = None
    if countermeasure_date:
        cm_dt = datetime.fromisoformat(countermeasure_date.replace("Z", ""))
        bc        = sum(1 for ts, _, _, _,  ct in rows if ct is not None and ts <  cm_dt)
        ac        = sum(1 for ts, _, _, _,  ct in rows if ct is not None and ts >= cm_dt)
        eb_before = sum(1 for ts, _, _, et, _  in rows if et is not None and ts <  cm_dt)
        eb_after  = sum(1 for ts, _, _, et, _  in rows if et is not None and ts >= cm_dt)
        before_after = {
            "before": {"crashes": bc, "events": eb_before},
            "after":  {"crashes": ac, "events": eb_after},
            "countermeasure_date": countermeasure_date,
        }

    # ── 4. Risk score ────────────────────────────────────────────────────────
    if t_start and t_end:
        days = max((
            datetime.fromisoformat(t_end.replace("Z", "")) -
            datetime.fromisoformat(t_start.replace("Z", ""))
        ).days, 1)
    else:
        days = 60
    risk_score = round(min(10.0, (co / days) * 10 + ((hb + sa + st) / days) * 0.001), 1)

    return {
        "crash_frequency": crash_freq,
        "daily_events":    daily_events,
        "event_breakdown": event_breakdown,
        "before_after":    before_after,
        "risk_score":      risk_score,
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
        SELECT timestamp, lat::float8, lon::float8, direction,
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
        SELECT lat::float8, lon::float8
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
        SELECT vin, timestamp, lat::float8, lon::float8, direction,
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
