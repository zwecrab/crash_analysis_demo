# L-DCM Crash Risk Analysis — Project Memory

## Project Goal

**Before/After Countermeasure Study** — Evaluate whether road safety interventions at known high-risk locations ("blackspots") in Thailand actually reduce dangerous driving behaviour and collisions, using Toyota L-DCM vehicle telemetry.

### Core Analytical Questions
1. Are vehicles exhibiting dangerous driving at the blackspot locations (harsh braking, sudden acceleration, collisions)?
2. Do collision rates and risky driving behaviour **decrease after a countermeasure is applied**?

---

## Data Pipeline

```
Toyota Car (L-DCM) → TCAP Device Server (Thailand) → OneDrive → TMC Linux Server (PostgreSQL) → Python Analysis
```

- **L-DCM** = Local Data Communication Module fitted to Toyota vehicles
- **TCAP** = Toyota Connected Asia Pacific
- **TMC** = Toyota Motor Corporation (analysis team)
- Data is extracted within **500 m – 5 km radius** of each blackspot
- VINs are **masked** before storage (privacy)

---

## Database: `carcrash` (PostgreSQL)

### Key Table: `public.sensor`
- ~91 million rows
- **1-second frequency** for normal driving
- **0.1-second frequency** during collision events
- Indexes on: `vin`, `timestamp`, `lat`, `lon`

| Column        | Meaning                                              |
|---------------|------------------------------------------------------|
| `vin`         | Masked vehicle ID                                    |
| `timestamp`   | UTC timestamp                                        |
| `lat` / `lon` | GPS position (WGS84, Thailand coverage)              |
| `direction`   | Heading in degrees (0=North, clockwise)              |
| `vehicle_speed` | km/h (Note: 77% of records are NULL)               |
| `gy_phyd`     | Lateral G-force (PHYD / normal driving event)        |
| `gx_phyd`     | Longitudinal G-force (PHYD / normal driving event)   |
| `gy_acci`     | Lateral G-force at collision (0.1s resolution)       |
| `gx_acci`     | Longitudinal G-force at collision (0.1s resolution)  |
| `event_type`  | 1=Sudden Accel, 2=Harsh Braking, 3=Sharp Turn        |
| `collision_type` | 17=Front-Back (Driving). Only type present.       |

> **CRITICAL DATA FINDING:** The actual database uses integer values `1, 2, 3` for `event_type`, NOT the hex codes `0x10, 0x20` from the documentation. Similarly, `collision_type` only contains `17` in this dataset.

---

## L-DCM Data Streams
| Stream   | Hex Code | Frequency         | Triggered by             |
|----------|----------|-------------------|--------------------------|
| Basic    | 0x11     | Every 1 second    | Always (probe data)      |
| PHYD     | 0x21     | 1 sec, on event   | G-value threshold breach |
| Accident | 0x32     | Every 0.1 second  | Collision detection      |

---

## Study Design
- **Locations:** The current sample data (91M rows) is bounded to a single tiny area (~700m x 700m) in Bangkok.
- **Analysis Zone:** Users can drag and resize a bounding circle (50m - 1000m) to analyze specific intersections.
- **Period:** 2025-01-31 to 2025-03-31 (2 months).
- **Countermeasure date:** User-configurable in the UI.

---

## Visualization Requirements (Guiding Principle)
> The visualization must **serve the analysis goal**, not just look impressive.
> It should help stakeholders answer: *"Did the countermeasure work?"*

Key things the visualization must show:
- Vehicle movement at blackspot areas (geographic context)
- Crash/event hotspots — where do risky events cluster?
- **Before vs After** comparison of event frequency at each blackspot
- Color-coded vehicle behaviour (normal → risky → collision)
- Time-scrollable with auto-play to animate the change over time
- Analytical metrics panel alongside the map (not just the map alone)

---

## Canonical Counting Definitions (all panels share these)

Implemented in `sql_schemas.py` (visit CTE machinery) + `app.py` (polygon bucketing).
Any two panels showing the same scope must show the same number — verified end-to-end.

- **Event identity:** one physical event = `DISTINCT (vin, timestamp, event_type, lat, lon)`.
  The raw table contains exact duplicate rows (e.g. 107 of Section B's 1077 raw rows);
  every count dedupes, so SRMA (Section B) reads **970**, not 1077.
- **Visit:** a vehicle's continuous in-corridor presence (|perp offset| ≤ 17.25 m inside
  `ROAD_BOUNDS`), split at >5-minute gaps between consecutive points.
- **Gate crossing:** interior gates (2,3,4) by straddle test within the visit; edge gate 1
  credited ONCE per visit at the closest-approach (minimum-progress) point below the
  section cap.
- **Event attribution (partition):** event in section S (polygon containment, Python
  ray-cast `point_in_polygon` — the single geometry predicate) joins its vehicle's visit
  (containment, else nearest within 5 min). Visit crossed BOTH of S's gates → directional
  route row (order of first crossings; same-timestamp ties → NE); otherwise → S's
  **partial** row. So `Σ directional + partial == section total == analytics card == heatmap`.
- **Reference numbers (whole dataset, deduped):** Section B = 970 = route 2→3 (187) +
  3→2 (676) + partial (107); per type accel 339 / brake 577 / turn 54.
- **Known limitations:** U-turn round trips within one visit attribute to a single
  direction; gate-1 dwellers (vehicles parking in Section A between gate events) give
  truthful but large MAX traversal times for routes 12/21.

---

## Tech Stack (V2)
- **Backend:** FastAPI (Python) + psycopg2 (remote PostgreSQL) or DuckDB (local `sensor_local.duckdb`)
- **Frontend:** Leaflet.js + Chart.js + Vanilla JS (`dashboard.js`, `modules/*.js`, `style.css`, `map_report.html`)
- **Animation Engine:** Frontend waypoint interpolation with dead-reckoning for missing timestamps (requestAnimationFrame).
- **Prediction Model Integration:** `POST /api/predict` stub endpoint ready for ML team.

## Files
| File | Purpose |
|------|---------|
| `db_connection.py` | PostgreSQL helper, reads `.env` |
| `sensor_table.md` | Full data dictionary for `sensor` table |
| `app.py` | FastAPI backend (endpoints, Python-side polygon bucketing) |
| `sql_schemas.py` | All SQL templates; visit sessionization + event attribution core |
| `coordinates.py` | Gate/section geometry, centerline projection, polygon helpers |
| `map_report.html` | Frontend dashboard layout (incl. matrix partial rows) |
| `style.css` | Professional dark-theme styling |
| `dashboard.js` | Map rendering, animation loop, and API interaction |
| `modules/*.js` | Frontend modules (routes, charts, heatmap, playback, …) |
| `PREDICTION_API.md` | Contract for the ML prediction model integration |
| `scratch/srma_event_partition.py` | Ground-truth reconciliation script (970 = both + one-gate) |

---
*Last updated: 2026-06-11*
