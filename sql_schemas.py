"""
sql_schemas.py — Parametric SQL schemas for L-DCM Crash Risk Analysis visualizer.

This module houses all database query templates as clean, pure Python functions,
utilizing coordinates.py configurations to dynamically assemble gate classification
logic and road search limits.
"""

from coordinates import (
    ROAD_BOUNDS, CORRIDOR_HALF_M, GATE_PROGRESS, progress_expr,
    EDGE_GATES,
)

def _get_sessionized_journeys_sql(route: str = None, use_spatial: bool = False, transition_interval_str: str = "5 minutes") -> str:
    """Return the CTEs to sessionize gate crossings into unique journeys and segments.
    Includes time_placeholder to be replaced with time_sql filter.

    Gates are detected as LINE CROSSINGS along the road centerline: a vehicle crosses
    gate g whenever two consecutive in-corridor points straddle that gate's progress
    value. This catches every vehicle that drives through, even when 1 Hz GPS skips the
    narrow gate box, while the corridor constraint keeps detection inside the research zone.
    """
    s_expr, d_expr = progress_expr("lat", "lon")

    # Per gate: interior gates use a straddle test (consecutive in-corridor points on
    # opposite sides of the gate line, continuous movement only so a long stop can't fake
    # a crossing). An edge gate sits at the limit of data coverage (no points on one side
    # to straddle) — but since nothing exists beyond it, any vehicle appearing in its
    # section must have entered through it. So we credit the gate at the vehicle's CLOSEST
    # APPROACH (local progress-minimum) within that section, which lands correctly for both
    # entry (NE) and exit (SW) directions.
    _sorted_sg = sorted(GATE_PROGRESS.values())
    def _section_cap(sg):
        above = [v for v in _sorted_sg if v > sg]
        return (min(above) - 5.0) if above else sg  # stay just shy of the next interior gate

    def _gate_clause(g, sg):
        if g in EDGE_GATES:
            return f"""            SELECT vin, timestamp, '{g}' as gate FROM stepped
            WHERE s < {_section_cap(sg)}
              AND (prev_s IS NULL OR s <= prev_s)
              AND (next_s IS NULL OR s <= next_s)"""
        return f"""            SELECT vin, timestamp, '{g}' as gate FROM stepped
            WHERE prev_s IS NOT NULL
              AND timestamp - prev_t <= INTERVAL '{transition_interval_str}'
              AND (s - {sg}) * (prev_s - {sg}) <= 0"""

    straddle = "\n            UNION ALL\n".join(
        _gate_clause(g, sg) for g, sg in sorted(GATE_PROGRESS.items())
    )

    route_filter = f"WHERE route = '{route}'" if route else ""

    return f"""
        corridor AS (
            SELECT
                vin,
                timestamp,
                {s_expr} as s
            FROM sensor
            WHERE lat BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']}
              AND lon BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}
              AND ABS({d_expr}) <= {CORRIDOR_HALF_M}
              -- time_placeholder --
        ),
        stepped AS (
            SELECT
                vin,
                timestamp,
                s,
                LAG(s)         OVER (PARTITION BY vin ORDER BY timestamp) as prev_s,
                LAG(timestamp) OVER (PARTITION BY vin ORDER BY timestamp) as prev_t,
                LEAD(s)        OVER (PARTITION BY vin ORDER BY timestamp) as next_s
            FROM corridor
        ),
        raw_gates AS (
{straddle}
        ),
        gate_crossings AS (
            SELECT
                vin,
                timestamp,
                gate,
                LAG(timestamp) OVER (PARTITION BY vin ORDER BY timestamp) as prev_ts
            FROM raw_gates
            WHERE gate IS NOT NULL
        ),
        journey_starts AS (
            SELECT 
                vin,
                timestamp,
                gate,
                CASE WHEN prev_ts IS NULL OR timestamp - prev_ts > INTERVAL '{transition_interval_str}' THEN 1 ELSE 0 END as is_start
            FROM gate_crossings
        ),
        journey_ids AS (
            SELECT 
                vin,
                timestamp,
                gate,
                SUM(is_start) OVER (PARTITION BY vin ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) as journey_no
            FROM journey_starts
        ),
        journey_summary AS (
            SELECT 
                vin,
                journey_no,
                MIN(timestamp) as t_start,
                MAX(timestamp) as t_end,
                MIN(CAST(gate AS INTEGER)) as min_gate,
                MAX(CAST(gate AS INTEGER)) as max_gate,
                MIN(CASE WHEN gate = '1' THEN timestamp END) as t1,
                MIN(CASE WHEN gate = '2' THEN timestamp END) as t2,
                MIN(CASE WHEN gate = '3' THEN timestamp END) as t3,
                MIN(CASE WHEN gate = '4' THEN timestamp END) as t4
            FROM journey_ids
            GROUP BY vin, journey_no
        ),
        journey_directions AS (
            SELECT
                vin,
                journey_no,
                t_start,
                t_end,
                min_gate,
                max_gate,
                t1, t2, t3, t4,
                CASE
                    WHEN (t1 IS NOT NULL AND t2 IS NOT NULL AND t1 < t2)
                      OR (t2 IS NOT NULL AND t3 IS NOT NULL AND t2 < t3)
                      OR (t3 IS NOT NULL AND t4 IS NOT NULL AND t3 < t4)
                      OR (t1 IS NOT NULL AND t3 IS NOT NULL AND t1 < t3)
                      OR (t2 IS NOT NULL AND t4 IS NOT NULL AND t2 < t4)
                      OR (t1 IS NOT NULL AND t4 IS NOT NULL AND t1 < t4)
                      THEN 'NE'
                    WHEN (t2 IS NOT NULL AND t1 IS NOT NULL AND t2 < t1)
                      OR (t3 IS NOT NULL AND t2 IS NOT NULL AND t3 < t2)
                      OR (t4 IS NOT NULL AND t3 IS NOT NULL AND t4 < t3)
                      OR (t3 IS NOT NULL AND t1 IS NOT NULL AND t3 < t1)
                      OR (t4 IS NOT NULL AND t2 IS NOT NULL AND t4 < t2)
                      OR (t4 IS NOT NULL AND t1 IS NOT NULL AND t4 < t1)
                      THEN 'SW'
                    ELSE NULL
                END as dir
            FROM journey_summary
        ),
        journey_segments_all AS (
            -- Each segment carries the LEG crossing times (origin gate -> destination gate),
            -- NOT the whole-journey span, so downstream event counts stay strictly between
            -- the two gates the segment is named for.
            SELECT vin, journey_no, t1 as t_start, t2 as t_end, '12' as route FROM journey_directions WHERE dir = 'NE' AND t1 IS NOT NULL AND t2 IS NOT NULL AND t1 < t2
            UNION ALL
            SELECT vin, journey_no, t2 as t_start, t3 as t_end, '23' as route FROM journey_directions WHERE dir = 'NE' AND t2 IS NOT NULL AND t3 IS NOT NULL AND t2 < t3
            UNION ALL
            SELECT vin, journey_no, t3 as t_start, t4 as t_end, '34' as route FROM journey_directions WHERE dir = 'NE' AND t3 IS NOT NULL AND t4 IS NOT NULL AND t3 < t4
            UNION ALL
            SELECT vin, journey_no, t4 as t_start, t3 as t_end, '43' as route FROM journey_directions WHERE dir = 'SW' AND t4 IS NOT NULL AND t3 IS NOT NULL AND t4 < t3
            UNION ALL
            SELECT vin, journey_no, t3 as t_start, t2 as t_end, '32' as route FROM journey_directions WHERE dir = 'SW' AND t3 IS NOT NULL AND t2 IS NOT NULL AND t3 < t2
            UNION ALL
            SELECT vin, journey_no, t2 as t_start, t1 as t_end, '21' as route FROM journey_directions WHERE dir = 'SW' AND t2 IS NOT NULL AND t1 IS NOT NULL AND t2 < t1
        ),
        journey_segments AS (
            SELECT * FROM journey_segments_all
            {route_filter}
        )
    """

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

def get_trajectory_query(sample_sec: int) -> tuple[str, str]:
    """Return the UNION ALL query to sample vehicle trajectories and events concurrently."""
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

def get_analytics_query(has_time: bool, route: str = None, use_spatial: bool = False) -> str:
    """Return the pre-fetch query to retrieve all event rows for polygon statistics aggregation.
    Supports dynamic active route transitions filtering.
    """
    time_sql = "AND timestamp BETWEEN %s AND %s" if has_time else ""
    time_bound_s_sql = "AND s.timestamp BETWEEN CAST(%s AS TIMESTAMP) - INTERVAL '5 minutes' AND CAST(%s AS TIMESTAMP) + INTERVAL '5 minutes'" if has_time else ""

    if route and len(route) == 2:
        cte_sql = _get_sessionized_journeys_sql(route, use_spatial)
        cte_sql = cte_sql.replace("-- time_placeholder --", time_sql)

        return f"""
            WITH {cte_sql},
            journey_bounds AS (
                SELECT 
                    t.vin,
                    t.journey_no,
                    MIN(t.t_start) as t_start,
                    MAX(t.t_end) as t_end,
                    MIN(s.timestamp) as journey_start,
                    MAX(s.timestamp) as journey_end
                FROM journey_segments t
                JOIN sensor s ON t.vin = s.vin 
                  {time_bound_s_sql}
                  AND s.timestamp BETWEEN t.t_start - INTERVAL '2 minutes' AND t.t_end + INTERVAL '2 minutes'
                WHERE s.lat BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']}
                  AND s.lon BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}
                GROUP BY t.vin, t.journey_no
            )
            -- DISTINCT so an event that falls inside two overlapping legs of the same
            -- vehicle is counted once (one physical event = one row).
            SELECT DISTINCT s.timestamp, s.lat::float8, s.lon::float8, s.event_type, s.collision_type
            FROM sensor s
            JOIN journey_bounds t ON s.vin = t.vin
              {time_bound_s_sql}
              -- Strict gate-to-gate leg: only events between the two gate crossings.
              AND s.timestamp BETWEEN t.t_start AND t.t_end
            WHERE (s.event_type IS NOT NULL OR s.collision_type IS NOT NULL)
              AND s.lat BETWEEN %s AND %s AND s.lon BETWEEN %s AND %s
            ORDER BY s.timestamp
        """

    return f"""
        SELECT timestamp, lat::float8, lon::float8, event_type, collision_type
        FROM sensor
        WHERE (event_type IS NOT NULL OR collision_type IS NOT NULL)
          AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s {time_sql}
        ORDER BY timestamp
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

def get_heatmap_query(
    event_type: int,
    speed_bracket: int,
    hour: int,
    route: str = None,
    use_spatial: bool = False
) -> tuple[str, list]:
    """Assemble and return the parameterized SQL heatmap query and parameters.
    Supports dynamic bidirectional route constraints and ST_Within spatial filtering.
    """
    # Event filter clause
    if event_type == 0:
        event_sql = "AND event_type IS NOT NULL"
    else:
        event_sql = f"AND event_type = {event_type}"

    # Speed bracket filter clause
    if speed_bracket == 1:
        speed_sql = "AND vehicle_speed BETWEEN 0 AND 60"
    elif speed_bracket == 2:
        speed_sql = "AND vehicle_speed BETWEEN 61 AND 100"
    elif speed_bracket == 3:
        speed_sql = "AND vehicle_speed > 100"
    else:
        speed_sql = ""

    # Hour filter clause (Bangkok local time UTC+7)
    if 0 <= hour <= 23:
        hour_sql = f"AND CAST(EXTRACT(HOUR FROM timestamp + INTERVAL '7' HOUR) AS INTEGER) = {hour}"
    else:
        hour_sql = ""

    if route and len(route) == 2:
        # Use the same 5-minute journey-grouping interval as the analytics/route panels
        # so the heatmap event count agrees with the per-section breakdown.
        cte_sql = _get_sessionized_journeys_sql(route, use_spatial)
        cte_sql = cte_sql.replace("-- time_placeholder --", "")

        if use_spatial:
            from coordinates import ROAD_POLYGONS, polygon_to_wkt, section_polygon_for_route
            # Restrict strictly to the ONE section between this route's two gates,
            # not the whole corridor (A+B+C) — otherwise events in neighbouring
            # sections leak into the count for this gate pair.
            seg_poly = section_polygon_for_route(route)
            polys = [seg_poly] if seg_poly else ROAD_POLYGONS
            wkt_polys = [polygon_to_wkt(p) for p in polys]
            spatial_clauses = [f"ST_Within(ST_Point(s.lon, s.lat), ST_GeomFromText('{wkt}'))" for wkt in wkt_polys]
            spatial_sql = f"AND ({' OR '.join(spatial_clauses)})"
        else:
            spatial_sql = ""

        query = f"""
            WITH {cte_sql}
            -- Strict gate-to-gate leg: event timestamp must fall between the vehicle's
            -- origin-gate and destination-gate crossings for this segment.
            SELECT s.lat::float8, s.lon::float8
            FROM sensor s
            JOIN journey_segments t ON s.vin = t.vin
              AND s.timestamp BETWEEN t.t_start AND t.t_end
            WHERE s.lat BETWEEN %s AND %s AND s.lon BETWEEN %s AND %s
              {spatial_sql}
              {event_sql}
              {speed_sql}
              {hour_sql}
            LIMIT 60000
        """
        return query

    # General heatmap query with spatial/polygon filtering support
    if use_spatial:
        from coordinates import ROAD_POLYGONS, polygon_to_wkt
        wkt_polys = [polygon_to_wkt(p) for p in ROAD_POLYGONS]
        spatial_clauses = [f"ST_Within(ST_Point(lon, lat), ST_GeomFromText('{wkt}'))" for wkt in wkt_polys]
        spatial_sql = f"AND ({' OR '.join(spatial_clauses)})"
        
        query = f"""
            SELECT lat::float8, lon::float8
            FROM sensor
            WHERE lat  BETWEEN %s AND %s
              AND lon  BETWEEN %s AND %s
              {event_sql}
              {speed_sql}
              {hour_sql}
              {spatial_sql}
            LIMIT 60000
        """
    else:
        query = f"""
            SELECT lat::float8, lon::float8
            FROM sensor
            WHERE lat  BETWEEN %s AND %s
              AND lon  BETWEEN %s AND %s
              {event_sql}
              {speed_sql}
              {hour_sql}
            LIMIT 60000
        """
    return query

def _section_case_sql(use_spatial: bool, lat: str = "lat", lon: str = "lon") -> str:
    """SQL CASE expression classifying a point into its road section id ('A'/'B'/'C').
    Uses precise polygon containment when spatial is available (matches the per-section
    breakdown card's point_in_polygon test); falls back to bounding boxes otherwise."""
    from coordinates import ROAD_SECTIONS, polygon_to_wkt
    if use_spatial:
        whens = [
            f"WHEN ST_Within(ST_Point({lon}, {lat}), ST_GeomFromText('{polygon_to_wkt(s['polygon'])}')) THEN '{s['id']}'"
            for s in ROAD_SECTIONS
        ]
    else:
        whens = [
            f"WHEN {lat} BETWEEN {s['lat_min']} AND {s['lat_max']} AND {lon} BETWEEN {s['lon_min']} AND {s['lon_max']} THEN '{s['id']}'"
            for s in ROAD_SECTIONS
        ]
    return "CASE " + " ".join(whens) + " ELSE NULL END"


def get_route_matrix_query(
    speed_bracket: int,
    hour: int,
    has_time: bool,
    use_spatial: bool = False
) -> str:
    """Return parameterized SQL query for the Origin-Destination Route Analysis Matrix.

    BRAKE/TURN/ACCEL are EVENT counts within each route's OWN section (12/21→A, 23/32→B,
    34/43→C), scoped to the strict gate-to-gate leg window — so they match the per-section
    breakdown card exactly. TRIPS remains the distinct-journey count for the route.
    """
    time_sql = "AND timestamp BETWEEN %s AND %s" if has_time else ""

    if 0 <= hour <= 23:
        hour_filter_events = f"AND CAST(EXTRACT(HOUR FROM timestamp + INTERVAL '7' HOUR) AS INTEGER) = {hour}"
        hour_filter_trips = f"AND CAST(EXTRACT(HOUR FROM t.t_start + INTERVAL '7' HOUR) AS INTEGER) = {hour}"
    else:
        hour_filter_events = ""
        hour_filter_trips = ""

    if speed_bracket == 1:
        speed_filter = "AND vehicle_speed BETWEEN 0 AND 60"
    elif speed_bracket == 2:
        speed_filter = "AND vehicle_speed BETWEEN 61 AND 100"
    elif speed_bracket == 3:
        speed_filter = "AND vehicle_speed > 100"
    else:
        speed_filter = ""

    cte_sql = _get_sessionized_journeys_sql(None, use_spatial)
    cte_sql = cte_sql.replace("-- time_placeholder --", time_sql)

    section_case = _section_case_sql(use_spatial)

    # Identity of one physical event (timestamp + location) — dedupes an event that falls
    # inside two overlapping legs of the same vehicle, matching the card's DISTINCT rows.
    _EVENT_KEY = "CAST(e.timestamp AS VARCHAR) || '|' || CAST(e.lat AS VARCHAR) || '|' || CAST(e.lon AS VARCHAR)"

    query = f"""
        WITH {cte_sql},
        events AS (
            SELECT
                vin,
                timestamp,
                lat,
                lon,
                event_type,
                {section_case} as section
            FROM sensor
            WHERE event_type IN (1, 2, 3)
              AND lat BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']}
              AND lon BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}
              {time_sql}
              {speed_filter}
              {hour_filter_events}
        )
        SELECT
            t.route,
            COUNT(DISTINCT t.vin || '_' || CAST(t.journey_no AS VARCHAR)) as trips,
            COUNT(DISTINCT CASE WHEN e.event_type = 2 THEN {_EVENT_KEY} END) as brake,
            COUNT(DISTINCT CASE WHEN e.event_type = 3 THEN {_EVENT_KEY} END) as turn,
            COUNT(DISTINCT CASE WHEN e.event_type = 1 THEN {_EVENT_KEY} END) as accel
        FROM journey_segments t
        LEFT JOIN events e ON e.vin = t.vin
          -- Strict gate-to-gate leg window, event must lie in THIS route's own section
          AND e.timestamp BETWEEN t.t_start AND t.t_end
          AND e.section = CASE
                WHEN t.route IN ('12', '21') THEN 'A'
                WHEN t.route IN ('23', '32') THEN 'B'
                WHEN t.route IN ('34', '43') THEN 'C'
              END
        WHERE 1=1
          {hour_filter_trips}
        GROUP BY t.route
        ORDER BY t.route
    """
    return query

def get_route_time_matrix_query(has_time: bool, use_spatial: bool = False) -> str:
    """Return SQL for the Time Analysis matrix: per route, the min / max / average time
    (in seconds) a vehicle takes to pass the route — i.e. the gap between its origin-gate
    and destination-gate crossings. Cross-DB safe (no MEDIAN / percentile funcs)."""
    time_sql = "AND timestamp BETWEEN %s AND %s" if has_time else ""
    cte_sql = _get_sessionized_journeys_sql(None, use_spatial)
    cte_sql = cte_sql.replace("-- time_placeholder --", time_sql)

    return f"""
        WITH {cte_sql}
        SELECT
            route,
            COUNT(*) as trips,
            MIN(EXTRACT(EPOCH FROM (t_end - t_start))) as min_s,
            MAX(EXTRACT(EPOCH FROM (t_end - t_start))) as max_s,
            AVG(EXTRACT(EPOCH FROM (t_end - t_start))) as avg_s
        FROM journey_segments
        WHERE t_end > t_start
        GROUP BY route
        ORDER BY route
    """

def get_route_trips_query(
    route: str,
    use_spatial: bool = False,
    limit: int = 10,
    offset: int = 0
) -> str:
    """Return SQL query for fetching crossings and events for a bidirectional route (Task 1).
    Takes a combined route ('AC' or 'BC') and retrieves both directions.
    Returns one page of `limit` trips (ordered by journey_start) starting at `offset`, so the
    panel can page through routes with tens of thousands of trips via prev/next controls.
    """
    cte_sql = _get_sessionized_journeys_sql(route, use_spatial)
    cte_sql = cte_sql.replace("-- time_placeholder --", "AND timestamp BETWEEN %s AND %s")

    query = f"""
        WITH {cte_sql},
        journey_bounds AS (
            SELECT
                t.vin,
                t.journey_no,
                t.route,
                MIN(t.t_start) as t_start,
                MAX(t.t_end) as t_end,
                MIN(s.timestamp) as journey_start,
                MAX(s.timestamp) as journey_end
            FROM journey_segments t
            JOIN sensor s ON t.vin = s.vin
              AND s.timestamp BETWEEN CAST(%s AS TIMESTAMP) - INTERVAL '5 minutes' AND CAST(%s AS TIMESTAMP) + INTERVAL '5 minutes'
              AND s.timestamp BETWEEN t.t_start - INTERVAL '2 minutes' AND t.t_end + INTERVAL '2 minutes'
            WHERE s.lat BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']}
              AND s.lon BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}
            GROUP BY t.vin, t.journey_no, t.route
        ),
        limited_trips AS (
            SELECT * FROM journey_bounds
            ORDER BY journey_start
            LIMIT {int(limit)} OFFSET {int(offset)}
        )
        SELECT 
            t.vin,
            t.journey_start as t_start,
            t.journey_end as t_end,
            s.timestamp as event_time,
            s.lat::float8 as lat,
            s.lon::float8 as lon,
            s.event_type,
            s.vehicle_speed,
            CASE 
                WHEN t.route = '12' THEN '1'
                WHEN t.route = '23' THEN '2'
                WHEN t.route = '34' THEN '3'
                WHEN t.route = '43' THEN '4'
                WHEN t.route = '32' THEN '3'
                WHEN t.route = '21' THEN '2'
            END as origin,
            CASE 
                WHEN t.route = '12' THEN '2'
                WHEN t.route = '23' THEN '3'
                WHEN t.route = '34' THEN '4'
                WHEN t.route = '43' THEN '3'
                WHEN t.route = '32' THEN '2'
                WHEN t.route = '21' THEN '1'
            END as destination
        FROM limited_trips t
        JOIN sensor s ON t.vin = s.vin
          AND s.timestamp BETWEEN CAST(%s AS TIMESTAMP) - INTERVAL '5 minutes' AND CAST(%s AS TIMESTAMP) + INTERVAL '5 minutes'
          AND s.timestamp BETWEEN t.t_start - INTERVAL '2 minutes' AND t.t_end + INTERVAL '2 minutes'
          AND s.timestamp BETWEEN t.journey_start AND t.journey_end
        ORDER BY t_start, s.timestamp
    """
    return query

