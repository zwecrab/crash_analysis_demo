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
| `event_type`  | 1=Harsh Braking, 2=Sudden Accel, 3=Sharp Turn        |
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

## Tech Stack (V2)
- **Backend:** FastAPI (Python) + psycopg2
- **Frontend:** Leaflet.js + Chart.js + Vanilla JS (`dashboard.js`, `style.css`, `map_report.html`)
- **Animation Engine:** Frontend waypoint interpolation with dead-reckoning for missing timestamps (requestAnimationFrame).
- **Prediction Model Integration:** `POST /api/predict` stub endpoint ready for ML team.

## Files
| File | Purpose |
|------|---------|
| `db_connection.py` | PostgreSQL helper, reads `.env` |
| `sensor_table.md` | Full data dictionary for `sensor` table |
| `app.py` | FastAPI backend |
| `map_report.html` | Frontend dashboard layout |
| `style.css` | Professional dark-theme styling |
| `dashboard.js` | Map rendering, animation loop, and API interaction |
| `PREDICTION_API.md` | Contract for the ML prediction model integration |

---
*Last updated: 2026-04-22*
