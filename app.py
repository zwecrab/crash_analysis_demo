"""
app.py — L-DCM Crash Risk Analysis Dashboard (V2)

Decoupled main router: All spatial coordinate boundaries are defined in coordinates.py,
and all parameterized database queries are defined in sql_schemas.py.
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
import coordinates
import sql_schemas

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="L-DCM Crash Risk Analysis API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_DIR = os.path.dirname(__file__)

@app.middleware("http")
async def add_cache_control_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/modules/") or path in ["/dashboard.js", "/style.css", "/"]:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

app.mount("/modules", StaticFiles(directory=os.path.join(_DIR, "modules")), name="modules")

# ── DuckDB: download from Dropbox/URL if DATABASE_DOWNLOAD_URL is set ────────
def _maybe_download_duckdb() -> str | None:
    """Download sensor_local.duckdb from DATABASE_DOWNLOAD_URL secret if needed."""
    import urllib.request

    url = os.getenv("DATABASE_DOWNLOAD_URL")
    if not url:
        return None

    destinations = ["/data/sensor_local.duckdb", "/tmp/sensor_local.duckdb"]

    for dest in destinations:
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
                os.remove(dest)

    print("[app] DuckDB download failed — no writable destination found")
    return None


# ── DuckDB local mode ─────────────────────────────────────────
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
        duck_sql = sql.replace("%%", "%").replace("%s", "?")

        if duck_sql.strip().upper().startswith("SET"):
            return

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


_SPATIAL_AVAILABLE = False

class _DuckConn:
    """Thin shim so _conn() always returns something with .cursor() and .close()."""
    def __init__(self):
        global _SPATIAL_AVAILABLE
        self._conn = _duckdb.connect(_LOCAL_DB, read_only=True)
        if not _SPATIAL_AVAILABLE:
            try:
                self._conn.execute("LOAD spatial;")
                _SPATIAL_AVAILABLE = True
            except Exception:
                try:
                    self._conn.execute("INSTALL spatial;")
                    self._conn.execute("LOAD spatial;")
                    _SPATIAL_AVAILABLE = True
                except Exception as e:
                    print(f"[app] Warning: Failed to load DuckDB spatial extension: {e}")
        else:
            try:
                self._conn.execute("LOAD spatial;")
            except Exception:
                pass

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
EVENT_LABELS = {1: "Sudden Acceleration", 2: "Harsh Braking", 3: "Sharp Turn"}
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
    return FileResponse(
        os.path.join(_DIR, "map_report.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )

@app.get("/style.css", include_in_schema=False)
def serve_css():
    return FileResponse(
        os.path.join(_DIR, "style.css"),
        media_type="text/css",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )

@app.get("/dashboard.js", include_in_schema=False)
def serve_js():
    return FileResponse(
        os.path.join(_DIR, "dashboard.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )


@app.get("/api/meta")
def get_meta():
    """Time range, spatial bounds, label dictionaries, and a suggested start time."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '30000'")
    cur.execute(sql_schemas.get_meta_query())
    row = cur.fetchone()
    if not row or not row[0]:
        cur.close(); conn.close()
        raise HTTPException(500, "Empty dataset")

    t_min, t_max = row[0], row[1]

    try:
        cur.execute("SET statement_timeout = '10000'")
        cur.execute(sql_schemas.get_suggested_window_query())
        best = cur.fetchone()
        t_suggested = best[0].isoformat() + "Z" if best else (t_min.isoformat() + "Z")
    except Exception:
        t_suggested = (t_min + (t_max - t_min) / 2).isoformat() + "Z"

    cur.close(); conn.close()
    return {
        "t_start":     t_min.isoformat() + "Z",
        "t_end":       t_max.isoformat() + "Z",
        "t_suggested": t_suggested,
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
    """Sampled waypoints per vehicle for smooth frontend animation."""
    conn = _conn()
    cur  = conn.cursor()
    cur.execute("SET statement_timeout = '15000'")
    query = sql_schemas.get_trajectory_query(sample_sec)
    cur.execute(query, (
        t_start, t_end, lat_min, lat_max, lon_min, lon_max,
        t_start, t_end, lat_min, lat_max, lon_min, lon_max,
    ))
    rows = cur.fetchall()
    cur.close(); conn.close()

    vins_with_telemetry: set[str] = {
        vin for vin, _, _, _, _, spd, evt, col, gx in rows
        if any(x is not None for x in (spd, evt, col, gx))
    }

    trajs: dict[str, list] = {}
    for vin, ts, la, lo, d, spd, evt, col, gx in rows:
        if vin not in vins_with_telemetry:
            continue
        trajs.setdefault(vin, []).append({
            "t":   ts.isoformat() + "Z",
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
    query = sql_schemas.get_accidents_query()
    cur.execute(query, (lat_min, lat_max, lon_min, lon_max, t_start, t_end))
    rows = cur.fetchall()
    cur.close(); conn.close()

    accidents = []
    vin_first: dict[str, str] = {}
    for vin, ts, la, lo, ct, gx, gy, spd in rows:
        ts_str = ts.isoformat() + "Z"
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
    route: Optional[str] = Query(None, description="Optional active route filter, e.g., AC, BC"),
):
    """Event breakdown, crash frequency, before/after, risk score."""
    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '25000'")
    bp = (lat_min, lat_max, lon_min, lon_max)
    tp = (t_start, t_end) if (t_start and t_end) else ()

    query = sql_schemas.get_analytics_query(bool(t_start and t_end), route, _SPATIAL_AVAILABLE)
    
    # Task 1: Param order varies depending on if query contains raw_gates transitions CTE
    if route:
        params = tp + bp
    else:
        params = bp + tp

    cur.execute(query, params)
    raw_rows = cur.fetchall()
    cur.close(); conn.close()

    _TOL = 0.001
    _road_focused = (
        coordinates.ROAD_BBOX is not None
        and lat_min >= coordinates.ROAD_BBOX["lat_min"] - _TOL
        and lat_max <= coordinates.ROAD_BBOX["lat_max"] + _TOL
        and lon_min >= coordinates.ROAD_BBOX["lon_min"] - _TOL
        and lon_max <= coordinates.ROAD_BBOX["lon_max"] + _TOL
    )

    if _road_focused:
        polys = coordinates.relevant_polygons(lat_min, lat_max, lon_min, lon_max)
        rows = [
            (ts, la, lo, et, ct)
            for ts, la, lo, et, ct in raw_rows
            if any(coordinates.point_in_polygon(la, lo, p) for p in polys)
        ]
    else:
        rows = raw_rows

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

    sa = sum(1 for _, _, _, et, _  in rows if et == 1)
    hb = sum(1 for _, _, _, et, _  in rows if et == 2)
    st = sum(1 for _, _, _, et, _  in rows if et == 3)
    co = sum(1 for _, _, _, _,  ct in rows if ct is not None)
    tot = max(hb + sa + st + co, 1)
    event_breakdown = {
        "harsh_brake":  {"count": hb, "pct": round(hb / tot * 100), "label": "Harsh Braking"},
        "sudden_accel": {"count": sa, "pct": round(sa / tot * 100), "label": "Sudden Acceleration"},
        "sharp_turn":   {"count": st, "pct": round(st / tot * 100), "label": "Sharp Turn"},
        "collision":    {"count": co, "pct": round(co / tot * 100), "label": "Collision"},
    }

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
    """Full trajectory of a single vehicle around a time centre."""
    center_dt = datetime.fromisoformat(t_center.replace("Z", ""))
    t_lo = center_dt - timedelta(minutes=window_minutes)
    t_hi = center_dt + timedelta(minutes=window_minutes)

    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '15000'")
    query = sql_schemas.get_vehicle_trajectory_query()
    cur.execute(query, (vin, t_lo.isoformat(), t_hi.isoformat()))
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
    route:         Optional[str] = Query(None, description="Optional bidirectional route filter, e.g. AC, BC"),
):
    """Heatmap point cloud for the chosen event-type / speed-bracket / hour / route filter."""
    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '30000'")

    query = sql_schemas.get_heatmap_query(event_type, speed_bracket, hour, route, _SPATIAL_AVAILABLE)

    try:
        cur.execute(query, (lat_min, lat_max, lon_min, lon_max))
        rows = cur.fetchall()
    except Exception as e:
        cur.close(); conn.close()
        raise HTTPException(500, f"Database heatmap query failed: {e}")
    cur.close(); conn.close()

    # Apply precise road polygon containment in python fallback if spatial is off and no route filter is specified
    if not _SPATIAL_AVAILABLE and not route and coordinates.ROAD_POLYGONS:
        filtered_points = []
        for r in rows:
            lat, lon = r[0], r[1]
            in_zone = False
            for poly in coordinates.ROAD_POLYGONS:
                if coordinates.point_in_polygon(lat, lon, poly):
                    in_zone = True
                    break
            if in_zone:
                filtered_points.append([lat, lon])
    else:
        filtered_points = [[r[0], r[1]] for r in rows]

    return {
        "points": filtered_points,
        "total":  len(filtered_points),
        "capped": len(rows) == 60000,
        "filter": {
            "event_type":    event_type,
            "speed_bracket": speed_bracket,
            "hour":          hour,
            "route":         route
        },
    }


@app.get("/api/road")
def get_road():
    """Return the focused-road configuration (Kamphaeng Phet 6 Rd) from road.json."""
    path = os.path.join(_DIR, "road.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"name": "", "sections": []}


@app.get("/api/route-matrix")
def get_route_matrix(
    event_type:    int   = Query(0,  description="0=all events, 1=accel, 2=brake, 3=turning"),
    speed_bracket: int   = Query(0,  description="0=all, 1=0-60 km/h, 2=61-100, 3=100+"),
    hour:          int   = Query(-1, description="-1=all, 0-23=local Bangkok hour (UTC+7)"),
    t_start: Optional[str] = Query(None),
    t_end:   Optional[str] = Query(None),
):
    """Origin-Destination (O-D) Route Matrix calculated dynamically in SQL.
    Task 1: Groups bidirectional AC/CA transitions under 'AC' and BC/CB under 'BC' and excludes AB/BA.
    """
    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '20000'")

    query = sql_schemas.get_route_matrix_query(speed_bracket, hour, bool(t_start and t_end), _SPATIAL_AVAILABLE)
    params = (t_start, t_end, t_start, t_end) if (t_start and t_end) else ()

    try:
        cur.execute(query, params)
        rows = cur.fetchall()
    except Exception as e:
        cur.close(); conn.close()
        raise HTTPException(500, f"Database query failed: {e}")

    cur.close(); conn.close()

    # Pre-populate bidirectional rows
    matrix = {
        "AC": {"trips": 0, "brake": 0, "turn": 0, "accel": 0},
        "BC": {"trips": 0, "brake": 0, "turn": 0, "accel": 0},
    }

    for route, trips, brake, turn, accel in rows:
        if route in matrix:
            matrix[route] = {
                "trips": trips,
                "brake": brake,
                "turn": turn,
                "accel": accel
            }

    return {
        "matrix": matrix,
        "filter": {
            "event_type": event_type,
            "speed_bracket": speed_bracket,
            "hour": hour,
            "t_start": t_start,
            "t_end": t_end
        }
    }


@app.get("/api/route-trips")
def get_route_trips(
    route: str = Query(..., description="Route name, e.g. AC, BC"),
    t_start: str = Query(..., description="ISO start timestamp"),
    t_end: str = Query(..., description="ISO end timestamp"),
    event_filter: str = Query("all", description="all, 1, 2, 3, normal"),
):
    """Fetch vehicle crossings and their events on a specific bidirectional route inside a time window."""
    if route not in ("AC", "BC"):
        raise HTTPException(400, "Invalid route. Expected 'AC' or 'BC'")

    conn = _conn(); cur = conn.cursor()
    cur.execute("SET statement_timeout = '20000'")
    query = sql_schemas.get_route_trips_query(route, _SPATIAL_AVAILABLE)

    try:
        cur.execute(query, (t_start, t_end))
        rows = cur.fetchall()
    except Exception as e:
        cur.close(); conn.close()
        raise HTTPException(500, f"Database query failed: {e}")

    cur.close(); conn.close()

    # Process and group rows by unique trip key (vin, t_start, t_end)
    trips_dict = {}
    for vin, t_start_dt, t_end_dt, ev_time, lat, lon, ev_type, spd, origin, dest in rows:
        key = (vin, t_start_dt, t_end_dt)
        if key not in trips_dict:
            trips_dict[key] = {
                "vin": vin,
                "t_start": t_start_dt.isoformat() + "Z",
                "t_end": t_end_dt.isoformat() + "Z",
                "origin": origin,
                "destination": dest,
                "max_speed": 0.0,
                "events": []
            }
        
        trip = trips_dict[key]
        if spd is not None:
            trip["max_speed"] = max(trip["max_speed"], float(spd))
            
        if ev_type in (1, 2, 3) and ev_time is not None and lat is not None and lon is not None:
            # Restricts warnings strictly to the SRMA zone (Section B) for routes (Task 2)
            if coordinates.SRMA_BBOX['lat_min'] <= float(lat) <= coordinates.SRMA_BBOX['lat_max'] and coordinates.SRMA_BBOX['lon_min'] <= float(lon) <= coordinates.SRMA_BBOX['lon_max']:
                if coordinates.point_in_polygon(float(lat), float(lon), coordinates.SRMA_POLYGON):
                    # Avoid duplicate events at the exact same millisecond
                    if not any(e["timestamp"] == ev_time.isoformat() + "Z" and e["event_type"] == ev_type for e in trip["events"]):
                        trip["events"].append({
                            "event_type": int(ev_type),
                            "timestamp": ev_time.isoformat() + "Z",
                            "lat": float(lat),
                            "lon": float(lon),
                            "speed": float(spd) if spd is not None else 0.0
                        })

    # Apply the event filter
    filtered_trips = []
    for trip in trips_dict.values():
        has_accel = any(e["event_type"] == 1 for e in trip["events"])
        has_brake = any(e["event_type"] == 2 for e in trip["events"])
        has_turn = any(e["event_type"] == 3 for e in trip["events"])
        has_any_event = len(trip["events"]) > 0

        if event_filter == "1":
            if has_accel:
                filtered_trips.append(trip)
        elif event_filter == "2":
            if has_brake:
                filtered_trips.append(trip)
        elif event_filter == "3":
            if has_turn:
                filtered_trips.append(trip)
        elif event_filter == "normal":
            if not has_any_event:
                filtered_trips.append(trip)
        else: # "all"
            filtered_trips.append(trip)

    return {"trips": filtered_trips}


@app.get("/api/debug")
def get_debug():
    """Quick health-check: confirms DB is reachable and returns key data stats."""
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
    """Stub for prediction model. Returns dummy risk zones."""
    return {
        "predictions": [],
        "model_ready": False,
        "message": "Prediction model not yet connected. See PREDICTION_API.md for integration guide.",
    }
