"""
sql_schemas.py — Parametric SQL schemas for L-DCM Crash Risk Analysis visualizer.

This module houses all database query templates as clean, pure Python functions,
utilizing coordinates.py configurations to dynamically assemble gate classification
logic and road search limits.
"""

from coordinates import ROAD_BOUNDS, SRMA_BBOX, get_gate_sql

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

    if route and len(route) == 2:
        origin1, dest1 = route[0], route[1]
        origin2, dest2 = route[1], route[0]
        gate_sql = get_gate_sql(use_spatial)

        return f"""
            WITH raw_gates AS (
                SELECT 
                    vin, 
                    timestamp, 
                    {gate_sql} as gate
                FROM sensor
                WHERE lat BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']} 
                  AND lon BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}
                  {time_sql}
            ),
            gate_crossings AS (
                SELECT 
                    vin, 
                    timestamp,
                    gate,
                    ROW_NUMBER() OVER (PARTITION BY vin ORDER BY timestamp) as rn
                FROM raw_gates
                WHERE gate IS NOT NULL
            ),
            gate_transitions AS (
                SELECT 
                    c1.vin,
                    c1.timestamp as t_start,
                    c2.timestamp as t_end
                FROM gate_crossings c1
                JOIN gate_crossings c2 ON c1.vin = c2.vin AND c1.rn + 1 = c2.rn
                WHERE c2.timestamp - c1.timestamp <= INTERVAL '5 minutes'
                  AND (
                    (c1.gate = '{origin1}' AND c2.gate = '{dest1}') OR
                    (c1.gate = '{origin2}' AND c2.gate = '{dest2}')
                  )
            )
            SELECT s.timestamp, s.lat::float8, s.lon::float8, s.event_type, s.collision_type
            FROM sensor s
            JOIN gate_transitions t ON s.vin = t.vin AND s.timestamp BETWEEN t.t_start AND t.t_end
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
        # Route-specific query (Task 3): filter points to crossings matching the chosen route bidirectionally
        # e.g., if route is 'AC', match 'A' ➔ 'C' OR 'C' ➔ 'A'
        origin1, dest1 = route[0], route[1]
        origin2, dest2 = route[1], route[0]
        
        gate_sql = get_gate_sql(use_spatial)
        query = f"""
            WITH raw_gates AS (
                SELECT 
                    vin, 
                    timestamp, 
                    {gate_sql} as gate
                FROM sensor
                WHERE lat BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']} 
                  AND lon BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}
            ),
            gate_crossings AS (
                SELECT 
                    vin, 
                    timestamp,
                    gate,
                    ROW_NUMBER() OVER (PARTITION BY vin ORDER BY timestamp) as rn
                FROM raw_gates
                WHERE gate IS NOT NULL
            ),
            gate_transitions AS (
                SELECT 
                    c1.vin,
                    c1.timestamp as t_start,
                    c2.timestamp as t_end
                FROM gate_crossings c1
                JOIN gate_crossings c2 ON c1.vin = c2.vin AND c1.rn + 1 = c2.rn
                WHERE c2.timestamp - c1.timestamp <= INTERVAL '60 minutes'
                  AND (
                    (c1.gate = '{origin1}' AND c2.gate = '{dest1}') OR
                    (c1.gate = '{origin2}' AND c2.gate = '{dest2}')
                  )
            )
            SELECT s.lat::float8, s.lon::float8
            FROM sensor s
            JOIN gate_transitions t ON s.vin = t.vin AND s.timestamp BETWEEN t.t_start AND t.t_end
            WHERE s.lat BETWEEN %s AND %s AND s.lon BETWEEN %s AND %s
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

def get_route_matrix_query(
    speed_bracket: int,
    hour: int,
    has_time: bool,
    use_spatial: bool = False
) -> str:
    """Return parameterized SQL query for Origin-Destination Route Analysis Matrix.
    Task 1: Aggregates AC & CA under 'AC' (A ↔ C) and BC & CB under 'BC' (B ↔ C), ignoring AB/BA.
    """
    time_sql = "AND timestamp BETWEEN %s AND %s" if has_time else ""

    if 0 <= hour <= 23:
        hour_filter_events = f"AND CAST(EXTRACT(HOUR FROM timestamp + INTERVAL '7' HOUR) AS INTEGER) = {hour}"
        hour_filter_trips = f"AND CAST(EXTRACT(HOUR FROM t_start + INTERVAL '7' HOUR) AS INTEGER) = {hour}"
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

    gate_sql = get_gate_sql(use_spatial)

    query = f"""
        WITH raw_gates AS (
            SELECT 
                vin, 
                timestamp, 
                event_type, 
                vehicle_speed,
                {gate_sql} as gate
            FROM sensor
            WHERE lat BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']} 
              AND lon BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}
              {time_sql}
        ),
        gate_crossings AS (
            SELECT 
                vin, 
                timestamp,
                gate,
                ROW_NUMBER() OVER (PARTITION BY vin ORDER BY timestamp) as rn
            FROM raw_gates
            WHERE gate IS NOT NULL
        ),
        gate_transitions AS (
            SELECT 
                c1.vin,
                c1.timestamp as t_start,
                c2.timestamp as t_end,
                c1.gate as origin,
                c2.gate as destination
            FROM gate_crossings c1
            JOIN gate_crossings c2 ON c1.vin = c2.vin AND c1.rn + 1 = c2.rn
            WHERE c1.gate != c2.gate
              -- Include A-C, B-C and A-B routes bidirectionally
              AND (
                (c1.gate = 'A' AND c2.gate = 'C') OR (c1.gate = 'C' AND c2.gate = 'A') OR
                (c1.gate = 'B' AND c2.gate = 'C') OR (c1.gate = 'C' AND c2.gate = 'B') OR
                (c1.gate = 'A' AND c2.gate = 'B') OR (c1.gate = 'B' AND c2.gate = 'A')
              )
              AND c2.timestamp - c1.timestamp <= INTERVAL '5 minutes'
              {hour_filter_trips}
        ),
        events AS (
            SELECT 
                vin, 
                timestamp, 
                event_type,
                vehicle_speed
            FROM sensor
            WHERE event_type IN (1, 2, 3)
              -- Restricts warning events strictly to the SRMA Zone (Section B)
              AND lat BETWEEN {SRMA_BBOX['lat_min']} AND {SRMA_BBOX['lat_max']} 
              AND lon BETWEEN {SRMA_BBOX['lon_min']} AND {SRMA_BBOX['lon_max']}
              {time_sql}
              {speed_filter}
              {hour_filter_events}
        )
        SELECT 
            CASE 
                WHEN (t.origin = 'A' AND t.destination = 'C') OR (t.origin = 'C' AND t.destination = 'A') THEN 'AC'
                WHEN (t.origin = 'B' AND t.destination = 'C') OR (t.origin = 'C' AND t.destination = 'B') THEN 'BC'
                WHEN (t.origin = 'A' AND t.destination = 'B') OR (t.origin = 'B' AND t.destination = 'A') THEN 'AB'
            END as route,
            COUNT(DISTINCT t.vin || '_' || CAST(t.t_start AS VARCHAR)) as trips,
            COUNT(DISTINCT CASE WHEN e.event_type = 2 THEN t.vin || '_' || CAST(t.t_start AS VARCHAR) END) as brake,
            COUNT(DISTINCT CASE WHEN e.event_type = 3 THEN t.vin || '_' || CAST(t.t_start AS VARCHAR) END) as turn,
            COUNT(DISTINCT CASE WHEN e.event_type = 1 THEN t.vin || '_' || CAST(t.t_start AS VARCHAR) END) as accel
        FROM gate_transitions t
        LEFT JOIN events e ON t.vin = e.vin AND e.timestamp BETWEEN t.t_start AND t.t_end
        GROUP BY route
        ORDER BY route
    """
    return query

def get_route_trips_query(
    route: str,
    use_spatial: bool = False
) -> str:
    """Return SQL query for fetching all crossings and events for a bidirectional route (Task 1).
    Takes a combined route ('AC' or 'BC') and retrieves both directions.
    """
    if route == "AC":
        origin1, dest1 = "A", "C"
        origin2, dest2 = "C", "A"
    elif route == "BC":
        origin1, dest1 = "B", "C"
        origin2, dest2 = "C", "B"
    elif route == "AB":
        origin1, dest1 = "A", "B"
        origin2, dest2 = "B", "A"

    gate_sql = get_gate_sql(use_spatial)

    query = f"""
        WITH raw_gates AS (
            SELECT 
                vin, 
                timestamp, 
                event_type, 
                vehicle_speed,
                {gate_sql} as gate
            FROM sensor
            WHERE lat BETWEEN {ROAD_BOUNDS['lat_min']} AND {ROAD_BOUNDS['lat_max']} 
              AND lon BETWEEN {ROAD_BOUNDS['lon_min']} AND {ROAD_BOUNDS['lon_max']}
              AND timestamp BETWEEN %s AND %s
        ),
        gate_crossings AS (
            SELECT 
                vin, 
                timestamp,
                gate,
                ROW_NUMBER() OVER (PARTITION BY vin ORDER BY timestamp) as rn
            FROM raw_gates
            WHERE gate IS NOT NULL
        ),
        gate_transitions AS (
            SELECT 
                c1.vin,
                c1.timestamp as t_start,
                c2.timestamp as t_end,
                c1.gate as origin,
                c2.gate as destination
            FROM gate_crossings c1
            JOIN gate_crossings c2 ON c1.vin = c2.vin AND c1.rn + 1 = c2.rn
            WHERE c2.timestamp - c1.timestamp <= INTERVAL '5 minutes'
              AND (
                (c1.gate = '{origin1}' AND c2.gate = '{dest1}') OR
                (c1.gate = '{origin2}' AND c2.gate = '{dest2}')
              )
        )
        SELECT 
            t.vin,
            t.t_start,
            t.t_end,
            s.timestamp as event_time,
            s.lat::float8 as lat,
            s.lon::float8 as lon,
            s.event_type,
            s.vehicle_speed,
            t.origin,
            t.destination
        FROM gate_transitions t
        JOIN sensor s ON t.vin = s.vin AND s.timestamp BETWEEN t.t_start AND t.t_end
        ORDER BY t.t_start, s.timestamp
    """
    return query
