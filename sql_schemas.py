"""
sql_schemas.py — Parametric SQL schemas for L-DCM Crash Risk Analysis visualizer.

This module houses all database query templates as pure Python functions, built on
coordinates.py geometry (centerline projection, gate progress, section polygons).

Canonical counting definitions (shared by analytics, heatmap, route matrix, trips):

- EVENT IDENTITY: one physical event = DISTINCT (vin, timestamp, event_type, lat, lon).
  The raw table contains exact duplicate rows; every counting query dedupes on this key.
- VISIT: a vehicle's continuous in-corridor presence (|perp offset| <= CORRIDOR_HALF_M,
  inside ROAD_BOUNDS), split whenever the gap between consecutive points exceeds the
  5-minute transition interval.
- GATE CROSSING: interior gates use the straddle test (two consecutive in-visit points on
  opposite sides of the gate's centerline progress, continuous movement only). Edge gates
  (telemetry exists on one side only) are credited ONCE per visit, at the visit's
  minimum-progress point below the section cap.
- EVENT ATTRIBUTION: an event joins its vehicle's visit containing its timestamp
  (fallback: nearest visit within 5 minutes — covers events whose GPS point falls inside
  a section polygon but just outside the corridor band). If the visit crossed BOTH gates
  of the event's section, the event belongs to the directional route (direction from the
  first-crossing order); otherwise it belongs to the section's PARTIAL bucket.
  Directional + partial therefore partition the section total exactly.
"""

from coordinates import (
    ROAD_BOUNDS, CORRIDOR_HALF_M, GATE_PROGRESS, progress_expr,
    EDGE_GATES, ROUTE_SECTION, SECTION_GATES,
    polygon_to_wkt,
)

# Gate ids ordered by centerline progress; the column order of every per-gate
# crossing-time field (tg<id>) in queries built here.
GATE_IDS = [g for g, _ in sorted(GATE_PROGRESS.items(), key=lambda kv: kv[1])]

# All adjacent directional routes, NE then SW per gate pair (e.g. 12, 21, 23, 32, ...).
ROUTES = [r for pair in zip(GATE_IDS, GATE_IDS[1:]) for r in (pair[0] + pair[1], pair[1] + pair[0])]


# ── Shared filter clause helpers ──────────────────────────────────

def _speed_sql(speed_bracket: int) -> str:
    """Speed bracket filter. Brackets are contiguous (<=60, 60–100, >100) so no value
    falls in a gap; NULL speeds (most rows) only match bracket 0 (= no filter)."""
    if speed_bracket == 1:
        return "AND vehicle_speed >= 0 AND vehicle_speed <= 60"
    if speed_bracket == 2:
        return "AND vehicle_speed > 60 AND vehicle_speed <= 100"
    if speed_bracket == 3:
        return "AND vehicle_speed > 100"
    return ""


def _hour_sql(hour: int, col: str = "timestamp") -> str:
    """Bangkok local-hour filter (UTC+7)."""
    if 0 <= hour <= 23:
        return f"AND CAST(EXTRACT(HOUR FROM {col} + INTERVAL '7' HOUR) AS INTEGER) = {hour}"
    return ""


def _road_bbox_sql(lat: str = "lat", lon: str = "lon") -> str:
    return (f"{lat} BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']} "
            f"AND {lon} BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}")


# ── Visit sessionization core ─────────────────────────────────────

def _get_visits_sql(transition_interval_str: str = "5 minutes") -> str:
    """CTE chain implementing the canonical VISIT and GATE CROSSING definitions.

    Produces:
      visits       — one row per (vin, visit_no): t0/t1 bounds + first crossing time per
                     gate (tg1..tgN, NULL when the gate was not crossed in that visit)
      route_visits — one row per completed directional gate-pair traversal:
                     (vin, visit_no, t0, t1, route, t_origin, t_dest)

    Contains `-- time_placeholder --` in the corridor CTE (2 params when filled).
    """
    s_expr, d_expr = progress_expr("lat", "lon")
    sorted_gates = sorted(GATE_PROGRESS.items(), key=lambda kv: kv[1])

    def _section_cap(sg):
        above = [v for _, v in sorted_gates if v > sg]
        return (min(above) - 5.0) if above else sg  # stay just shy of the next gate

    gate_branches = []
    for g, sg in sorted_gates:
        if g in EDGE_GATES:
            # Edge gate: telemetry starts/ends here, so the straddle test can't fire.
            # Since nothing exists beyond it, any vehicle inside its section must have
            # passed through it: credit ONE crossing per visit, at the visit's closest
            # approach (minimum progress) below the section cap. One deterministic hit
            # per visit — a stationary vehicle no longer generates a hit per second.
            # Caveat: a vehicle that dwells in this section between its two gate events
            # yields a truthful but large gate-to-gate elapsed time (affects the time
            # matrix MAX for routes touching this gate).
            gate_branches.append(f"""            SELECT vin, visit_no, timestamp, '{g}' AS gate
            FROM (
                SELECT vin, visit_no, timestamp, s,
                       ROW_NUMBER() OVER (PARTITION BY vin, visit_no ORDER BY s, timestamp) AS rn
                FROM sessioned
            ) edge_{g}
            WHERE rn = 1 AND s < {_section_cap(sg)}""")
        else:
            gate_branches.append(f"""            SELECT vin, visit_no, timestamp, '{g}' AS gate
            FROM visit_stepped
            WHERE prev_s IS NOT NULL
              AND timestamp - prev_t <= INTERVAL '{transition_interval_str}'
              AND (s - {sg}) * (prev_s - {sg}) <= 0""")
    gate_hits = "\n            UNION ALL\n".join(gate_branches)

    tg_aggs = ",\n                   ".join(
        f"MIN(CASE WHEN gate = '{g}' THEN timestamp END) AS tg{g}" for g in GATE_IDS
    )
    tg_cols = ", ".join(f"g.tg{g}" for g in GATE_IDS)

    # A single GPS step can straddle BOTH gates of a section (gap up to 5 min), giving
    # the two crossings the same timestamp. Such ties resolve to the NE route (<=) —
    # the same rule the Python matrix bucketing and _route_bucket_condition apply.
    route_branches = []
    for lo, hi in zip(GATE_IDS, GATE_IDS[1:]):
        route_branches.append(
            f"            SELECT vin, visit_no, t0, t1, '{lo}{hi}' AS route, tg{lo} AS t_origin, tg{hi} AS t_dest"
            f" FROM visits WHERE tg{lo} IS NOT NULL AND tg{hi} IS NOT NULL AND tg{lo} <= tg{hi}"
        )
        route_branches.append(
            f"            SELECT vin, visit_no, t0, t1, '{hi}{lo}' AS route, tg{hi} AS t_origin, tg{lo} AS t_dest"
            f" FROM visits WHERE tg{lo} IS NOT NULL AND tg{hi} IS NOT NULL AND tg{hi} < tg{lo}"
        )
    route_visits = "\n            UNION ALL\n".join(route_branches)

    return f"""
        corridor AS (
            -- DISTINCT drops the raw table's exact-duplicate rows; the (timestamp, s)
            -- window tiebreak below keeps same-second readings deterministic.
            SELECT DISTINCT vin, timestamp, {s_expr} AS s
            FROM sensor
            WHERE {_road_bbox_sql()}
              AND ABS({d_expr}) <= {CORRIDOR_HALF_M}
              -- time_placeholder --
        ),
        stepped AS (
            SELECT vin, timestamp, s,
                   LAG(timestamp) OVER (PARTITION BY vin ORDER BY timestamp, s) AS prev_t
            FROM corridor
        ),
        sessioned AS (
            SELECT vin, timestamp, s,
                   SUM(CASE WHEN prev_t IS NULL OR timestamp - prev_t > INTERVAL '{transition_interval_str}'
                            THEN 1 ELSE 0 END)
                     OVER (PARTITION BY vin ORDER BY timestamp, s
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS visit_no
            FROM stepped
        ),
        visit_stepped AS (
            SELECT vin, visit_no, timestamp, s,
                   LAG(s)         OVER (PARTITION BY vin, visit_no ORDER BY timestamp, s) AS prev_s,
                   LAG(timestamp) OVER (PARTITION BY vin, visit_no ORDER BY timestamp, s) AS prev_t
            FROM sessioned
        ),
        gate_hits AS (
{gate_hits}
        ),
        visit_bounds AS (
            SELECT vin, visit_no, MIN(timestamp) AS t0, MAX(timestamp) AS t1
            FROM sessioned
            GROUP BY vin, visit_no
        ),
        gate_times AS (
            SELECT vin, visit_no,
                   {tg_aggs}
            FROM gate_hits
            GROUP BY vin, visit_no
        ),
        visits AS (
            SELECT b.vin, b.visit_no, b.t0, b.t1, {tg_cols}
            FROM visit_bounds b
            LEFT JOIN gate_times g ON b.vin = g.vin AND b.visit_no = g.visit_no
        ),
        route_visits AS (
{route_visits}
        )"""


def _deduped_events_sql(
    time_sql: str = "",
    extra_sql: str = "",
    include_collisions: bool = False,
    caller_bbox: bool = False,
) -> str:
    """`events` CTE applying the canonical EVENT IDENTITY dedup, with a synthetic
    event_id so downstream best-visit selection can never collapse distinct events.
    `caller_bbox` adds 4 positional params (lat_min, lat_max, lon_min, lon_max)."""
    if include_collisions:
        cols = "vin, timestamp, event_type, collision_type, lat, lon"
        pred = "(event_type IS NOT NULL OR collision_type IS NOT NULL)"
    else:
        cols = "vin, timestamp, event_type, lat, lon"
        pred = "event_type IN (1, 2, 3)"
    bbox_sql = "AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s" if caller_bbox else ""
    # NOTE: time placeholders render BEFORE the caller-bbox placeholders — callers bind
    # params as ([corridor time,] [events time,] lat_min, lat_max, lon_min, lon_max).
    return f"""        events AS (
            SELECT ROW_NUMBER() OVER () AS event_id, {cols}
            FROM (
                SELECT DISTINCT {cols}
                FROM sensor
                WHERE {pred}
                  AND {_road_bbox_sql()}
                  {time_sql}
                  {bbox_sql}
                  {extra_sql}
            ) deduped
        )"""


def _attributed_sql() -> str:
    """`attributed` CTE joining each event to its vehicle's best visit: strict time
    containment when possible, otherwise the nearest visit within 5 minutes."""
    tg_cols = ", ".join(f"v.tg{g}" for g in GATE_IDS)
    return f"""        attributed AS (
            SELECT * FROM (
                SELECT e.*, {tg_cols}, v.t0, v.t1,
                       ROW_NUMBER() OVER (
                           PARTITION BY e.event_id
                           ORDER BY CASE
                               WHEN v.vin IS NULL THEN INTERVAL '6 minutes'
                               WHEN e.timestamp BETWEEN v.t0 AND v.t1 THEN INTERVAL '0 seconds'
                               WHEN e.timestamp < v.t0 THEN v.t0 - e.timestamp
                               ELSE e.timestamp - v.t1 END,
                               v.t0  -- deterministic tiebreak: earlier visit wins
                       ) AS best_rn
                FROM events e
                LEFT JOIN visits v
                  ON v.vin = e.vin
                 AND e.timestamp BETWEEN v.t0 - INTERVAL '5 minutes' AND v.t1 + INTERVAL '5 minutes'
            ) ranked
            WHERE best_rn = 1
        )"""


def _route_bucket_condition(route: str) -> str:
    """SQL predicate (on `attributed` columns): the event's best visit completed this
    directional route — both section gates crossed, in this order. Same-timestamp
    crossings (one GPS step straddling both gates) resolve to the NE route."""
    sec = ROUTE_SECTION[route]
    lo, hi = SECTION_GATES[sec]
    cmp = f"tg{lo} <= tg{hi}" if route[0] == lo else f"tg{hi} < tg{lo}"
    return f"tg{lo} IS NOT NULL AND tg{hi} IS NOT NULL AND {cmp}"


# ── Simple queries (unchanged behavior) ───────────────────────────

def get_meta_query() -> str:
    """Return the SQL query to fetch database time range and coordinate bounds."""
    return """
        SELECT MIN(timestamp), MAX(timestamp),
               MIN(lat)::float8, MAX(lat)::float8,
               MIN(lon)::float8, MAX(lon)::float8
        FROM sensor
    """


def get_suggested_window_query() -> str:
    """Return the tablesampled SQL query to discover the busiest 10-minute window."""
    return """
        SELECT DATE_TRUNC('hour', timestamp) +
               (FLOOR(EXTRACT(MINUTE FROM timestamp) / 10) * interval '10 minutes') AS window,
               COUNT(*) AS cnt
        FROM sensor TABLESAMPLE SYSTEM(1)
        GROUP BY 1
        ORDER BY 2 DESC
        LIMIT 1
    """


def get_trajectory_query(sample_sec: int) -> str:
    """Return the UNION ALL query to sample vehicle trajectories and events concurrently.
    Positional params: (t_start, t_end, lat_min, lat_max, lon_min, lon_max) twice."""
    query = f"""
        -- Branch 1: normal driving (Basic 0x11 stream).
        -- Modulo-sample so we don't overwhelm the wire.
        (SELECT vin, timestamp, lat::float8, lon::float8, direction,
                vehicle_speed, event_type, collision_type, gx_acci
        FROM sensor
        WHERE timestamp BETWEEN %s AND %s
          AND lat BETWEEN %s AND %s
          AND lon BETWEEN %s AND %s
          AND event_type    IS NULL
          AND collision_type IS NULL
          AND EXTRACT(EPOCH FROM timestamp)::bigint %% {sample_sec} = 0
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
    """
    return query


def get_accidents_query() -> str:
    """Return the parameterized query to fetch collision pins inside window and bbox."""
    return """
        SELECT vin, timestamp, lat::float8, lon::float8,
               collision_type, gx_acci, gy_acci, vehicle_speed
        FROM sensor
        WHERE collision_type IS NOT NULL
          AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
          AND timestamp BETWEEN %s AND %s
        ORDER BY timestamp
        LIMIT 500
    """


def get_vehicle_trajectory_query() -> str:
    """Return query for fetching full non-sampled telemetry for single vehicle investigation."""
    return """
        SELECT timestamp, lat::float8, lon::float8, direction,
               vehicle_speed, event_type, collision_type, gx_acci
        FROM sensor
        WHERE vin = %s AND timestamp BETWEEN %s AND %s
        ORDER BY timestamp
        LIMIT 5000
    """


# ── Analytics ─────────────────────────────────────────────────────

def get_analytics_query(has_time: bool, route: str = None) -> str:
    """Pre-fetch query for the analytics panel: deduped event/collision rows for Python
    polygon aggregation.

    Without route — params: (lat_min, lat_max, lon_min, lon_max [, t_start, t_end]).
    With route   — rows restricted to events whose best visit completed the directional
    route; params: ([t_start, t_end,] [t_start, t_end,] lat_min, lat_max, lon_min, lon_max)
    — corridor time pair first, events time pair second, when has_time.
    """
    time_sql = "AND timestamp BETWEEN %s AND %s" if has_time else ""

    if route in ROUTE_SECTION:
        visits_cte = _get_visits_sql().replace("-- time_placeholder --", time_sql)
        events_cte = _deduped_events_sql(time_sql=time_sql, include_collisions=True, caller_bbox=True)
        return f"""
            WITH {visits_cte},
{events_cte},
{_attributed_sql()}
            SELECT timestamp, lat::float8, lon::float8, event_type, collision_type
            FROM attributed
            WHERE {_route_bucket_condition(route)}
            ORDER BY timestamp
        """

    return f"""
        SELECT timestamp, lat::float8, lon::float8, event_type, collision_type
        FROM (
            SELECT DISTINCT vin, timestamp, lat, lon, event_type, collision_type
            FROM sensor
            WHERE (event_type IS NOT NULL OR collision_type IS NOT NULL)
              AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s {time_sql}
        ) deduped
        ORDER BY timestamp
    """


# ── Heatmap ───────────────────────────────────────────────────────

def get_heatmap_query(
    event_type: int,
    speed_bracket: int,
    hour: int,
    route: str = None,
    use_spatial: bool = False,
) -> str:
    """Heatmap point-cloud query (params: lat_min, lat_max, lon_min, lon_max).

    With a route, points are the deduped events whose best visit completed that
    directional route — identical attribution to the route matrix. The section-polygon
    test is left to the caller (app.py, coordinates.point_in_polygon) in BOTH spatial
    modes so the heatmap uses the exact same geometry predicate as the matrix —
    ST_Within and the ray-cast disagree on boundary points.
    """
    event_sql = "AND event_type IS NOT NULL" if event_type == 0 else f"AND event_type = {int(event_type)}"
    speed_sql = _speed_sql(speed_bracket)
    hour_sql = _hour_sql(hour)

    if route in ROUTE_SECTION:
        visits_cte = _get_visits_sql().replace("-- time_placeholder --", "")
        events_cte = _deduped_events_sql(
            extra_sql=f"{event_sql}\n                  {speed_sql}\n                  {hour_sql}",
            caller_bbox=True,
        )
        return f"""
            WITH {visits_cte},
{events_cte},
{_attributed_sql()}
            SELECT lat::float8, lon::float8
            FROM attributed
            WHERE {_route_bucket_condition(route)}
            LIMIT 60000
        """

    if use_spatial:
        from coordinates import ROAD_POLYGONS
        wkt_polys = [polygon_to_wkt(p) for p in ROAD_POLYGONS]
        clauses = [f"ST_Within(ST_Point(lon, lat), ST_GeomFromText('{w}'))" for w in wkt_polys]
        spatial_sql = f"AND ({' OR '.join(clauses)})"
    else:
        spatial_sql = ""

    return f"""
        SELECT lat::float8, lon::float8
        FROM (
            SELECT DISTINCT vin, timestamp, event_type, lat, lon
            FROM sensor
            WHERE lat BETWEEN %s AND %s
              AND lon BETWEEN %s AND %s
              {event_sql}
              {speed_sql}
              {hour_sql}
              {spatial_sql}
        ) deduped
        LIMIT 60000
    """


# ── Route matrix ──────────────────────────────────────────────────

def get_route_matrix_trips_query(hour: int, has_time: bool) -> str:
    """Trips per directional route = completed (vin, visit) traversals.
    Params: (t_start, t_end) when has_time, else none."""
    time_sql = "AND timestamp BETWEEN %s AND %s" if has_time else ""
    visits_cte = _get_visits_sql().replace("-- time_placeholder --", time_sql)
    hour_filter = _hour_sql(hour, col="t_origin")
    return f"""
        WITH {visits_cte}
        SELECT route, COUNT(*) AS trips
        FROM route_visits
        WHERE 1=1
          {hour_filter}
        GROUP BY route
        ORDER BY route
    """


def get_route_matrix_events_query(speed_bracket: int, hour: int, has_time: bool) -> str:
    """Per-event rows for the O-D matrix: deduped events with their best visit's gate
    crossing times. Section membership and route/partial bucketing happen in Python
    (coordinates.point_in_polygon + SECTION_GATES), which keeps spatial and non-spatial
    modes on the identical code path.

    Row shape: (lat, lon, event_type, tg<gate> for gate in GATE_IDS).
    Params: (t_start, t_end, t_start, t_end) when has_time — corridor pair then events
    pair — else none.
    """
    time_sql = "AND timestamp BETWEEN %s AND %s" if has_time else ""
    visits_cte = _get_visits_sql().replace("-- time_placeholder --", time_sql)
    events_cte = _deduped_events_sql(
        time_sql=time_sql,
        extra_sql=f"{_speed_sql(speed_bracket)}\n                  {_hour_sql(hour)}",
    )
    tg_cols = ", ".join(f"tg{g}" for g in GATE_IDS)
    return f"""
        WITH {visits_cte},
{events_cte},
{_attributed_sql()}
        SELECT lat::float8, lon::float8, event_type, {tg_cols}
        FROM attributed
    """


def get_route_time_matrix_query(has_time: bool) -> str:
    """Time Analysis matrix: per route, min / max / average traversal seconds —
    first crossing of the destination gate minus first crossing of the origin gate
    within the same visit. Params: (t_start, t_end) when has_time, else none."""
    time_sql = "AND timestamp BETWEEN %s AND %s" if has_time else ""
    visits_cte = _get_visits_sql().replace("-- time_placeholder --", time_sql)
    return f"""
        WITH {visits_cte}
        SELECT
            route,
            COUNT(*) AS trips,
            MIN(EXTRACT(EPOCH FROM (t_dest - t_origin))) AS min_s,
            MAX(EXTRACT(EPOCH FROM (t_dest - t_origin))) AS max_s,
            AVG(EXTRACT(EPOCH FROM (t_dest - t_origin))) AS avg_s
        FROM route_visits
        GROUP BY route
        ORDER BY route
    """


# ── Route trips panel ─────────────────────────────────────────────

def get_route_trips_query(
    route: str,
    limit: int = 10,
    offset: int = 0,
) -> str:
    """One page of completed traversals for a directional route ('12'…'43'), with each
    trip's telemetry rows over its whole visit window (so the trip's event list matches
    the route-matrix attribution exactly).

    Row shape: (vin, t_origin, t_dest, t0, t1, event_time, lat, lon, event_type,
    vehicle_speed, origin, destination).
    Params: (t_start, t_end, t_start, t_end) — corridor pair, then the padded sensor-join
    pair for index pruning.
    """
    if route not in ROUTE_SECTION:
        raise ValueError(f"Unknown route {route!r}; expected one of {ROUTES}")
    visits_cte = _get_visits_sql().replace(
        "-- time_placeholder --", "AND timestamp BETWEEN %s AND %s"
    )
    origin, dest = route[0], route[1]
    return f"""
        WITH {visits_cte},
        trips AS (
            SELECT vin, visit_no, t0, t1, t_origin, t_dest
            FROM route_visits
            WHERE route = '{route}'
            ORDER BY t_origin
            LIMIT {int(limit)} OFFSET {int(offset)}
        )
        SELECT
            t.vin,
            t.t_origin,
            t.t_dest,
            t.t0,
            t.t1,
            s.timestamp AS event_time,
            s.lat::float8 AS lat,
            s.lon::float8 AS lon,
            s.event_type,
            s.vehicle_speed,
            '{origin}' AS origin,
            '{dest}' AS destination
        FROM trips t
        JOIN sensor s ON s.vin = t.vin
          AND s.timestamp BETWEEN CAST(%s AS TIMESTAMP) - INTERVAL '5 minutes'
                              AND CAST(%s AS TIMESTAMP) + INTERVAL '5 minutes'
          AND s.timestamp BETWEEN t.t0 AND t.t1
          AND s.lat BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']}
          AND s.lon BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}
        ORDER BY t.t_origin, s.timestamp
    """
