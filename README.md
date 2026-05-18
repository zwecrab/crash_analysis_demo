# L-DCM Crash Risk Analysis Dashboard

Interactive before/after countermeasure study tool for Toyota L-DCM vehicle telemetry at Thailand blackspot locations. FastAPI backend + Leaflet / Chart.js frontend with a warm, editorial Claude-inspired UI.

The dashboard lets an analyst:

- Watch anonymised vehicle trajectories animate through a blackspot radius.
- Browse every detected collision in a left-side list filtered by **type** and **severity**.
- Click any collision to enter **investigation mode** — playback pauses, the timeline jumps to the impact, the vehicle's ±3 min track is drawn, and every other vehicle fades to 22 % opacity.
- Jump to any exact timestamp in the dataset via the bottom bar.
- Compare crash and event rates **before vs. after** a user-configurable countermeasure date.
- See a weighted risk score, event-type breakdown, and daily crash-frequency chart alongside the map.

---

## Requirements

Python 3.11+ and the packages in `requirements.txt`:

- `fastapi`
- `uvicorn[standard]`
- `psycopg2-binary` — PostgreSQL driver (remote mode)
- `python-dotenv`
- `duckdb` *(optional, local mode — see below)*

---

## Setup & Running Locally

### 1. Create / activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure the data source

The app auto-detects **local (DuckDB)** mode if `sensor_local.duckdb` exists in the project folder, otherwise it falls back to **remote (PostgreSQL)**.

- **Local (fast, recommended for dev):** run `python export_to_duckdb.py` once to bake a read-only DuckDB copy of the `sensor` table.
- **Remote:** put PostgreSQL credentials in a `.env` file next to `app.py`. See `db_connection.py` for the expected variables.

### 3. Run the FastAPI server

```powershell
uvicorn app:app --reload --port 8000
```

or directly:

```powershell
.\.venv\Scripts\uvicorn app:app --reload --port 8000
```

### 4. Open the dashboard

Navigate to [http://localhost:8000](http://localhost:8000).

The first paint jumps to the busiest 10-minute window found in the data (see `/api/meta`), so you won't land on an empty midnight stretch.

---

## Using the Dashboard

### Top bar
- **⟲ Reset View** — re-fit the map to the dataset bounds and re-snap the circle to the first blackspot.
- **Accidents: Persist / Flash / Hidden** — toggle how collision markers behave on the map as time advances.
- **🔮 Predictions** — placeholder for the ML integration (see `PREDICTION_API.md`).

### Left panel — Collisions
- Two dropdown filters at the top: **type** (populated dynamically from the data) and **severity** (low / medium / high). Filters apply to both the list **and** the map markers.
- Each card shows: severity dot, collision label, date · time · speed · G-force, masked VIN tail, severity tag.
- **Click a card** to enter *investigation mode*:
  - Playback pauses.
  - Timeline jumps to the impact timestamp.
  - A coral polyline draws the vehicle's ±3 min track with a pulsing red *impact* marker plus green *start* and blue *end* anchors.
  - Every other vehicle on the map fades to 22 % opacity.
  - Map auto-zooms to fit the track.
- Hit **✕ Clear** in the focus bar (or click another collision) to exit.
- The **◀ / ▶** button collapses the panel to a vertical rail when you need more map area.

### Map
- Dashed circle marks the blackspot radius (pulled from `blackspots.json`). The area outside is dimmed by a polygon mask so the eye stays inside the analysis zone.
- Coloured arrow markers encode driver behaviour: blue (normal), teal (accelerating), amber (harsh brake), purple (sharp turn), red (collision).
- **Legend** (top-left) and live **Vehicles in View** counter (bottom-left).

### Right panel — Analytics
- **Risk Score** gauge (0–10, weighted by collision + event rate) with per-day breakdown.
- **Event Type Breakdown** doughnut.
- **Crash Frequency Over Time** line chart (dense daily series, no gaps).
- **Before vs. After Countermeasure** — set a date, click Apply. Shows crashes and events side-by-side pre/post.

### Analytics Metrics

How each number in the right-hand panel is computed. All metrics respect the current map bounding-box and time range; collision counts always use rows where `collision_type IS NOT NULL`.

#### Risk Score (0–10) — updates per playback day

The gauge tracks the **currently-playing day**. As the timeline advances (live playback, scrubbing, or exact-time jump), the score recomputes the moment playback crosses into a new UTC day.

```
crashes_today = COUNT(collision_type IS NOT NULL on the current day)
events_today  = COUNT(event_type     IS NOT NULL on the current day)

risk_score = min(10,  crashes_today × 10  +  events_today × 0.001)
```

- Weighting rationale: a single crash on the day pushes the score by 10 pts — crashes dominate. Events are scaled down 10 000× because a 700 m² blackspot can log 1 400+ events/day, which would otherwise saturate the gauge at 10.
- Gauge thresholds: **≤ 3 LOW** · **≤ 6 MEDIUM** · **> 6 HIGH**.
- The detail line below the gauge shows the exact date + raw crash and event counts for that day, so the user can see why the score moved.
- Data source: `/api/analytics` returns parallel `crash_frequency` and `daily_events` series (both dense-filled over the selected range); the client caches them keyed by date and looks up `ISO-date(playback-time)` each frame. Day key is UTC to match the server's `date_trunc('day', timestamp)` grouping.
- The whole-range version of this score is still returned as `risk_score` for non-animated callers. See `get_analytics()` in [`app.py`](app.py), `updateRiskForCurrentDay()` and `riskLevel()` in [`dashboard.js`](dashboard.js).

#### Collision Severity

Derived from the longitudinal G-force recorded in `gx_acci` (Accident 0x32 stream):

```
|gx_acci| ≤ 3  →  low
|gx_acci| ≤ 4  →  medium
|gx_acci| >  4 →  high
gx_acci = NULL →  unknown
```

Used for the collision-list dots, severity tags, and map marker colours.

#### Event Type Breakdown

Raw counts from the `event_type` column (PHYD 0x21 stream) within the time + bbox window, plus the collision count:

| Slice | Source |
|---|---|
| Harsh Braking | `event_type = 1` |
| Sudden Acceleration | `event_type = 2` |
| Sharp Turn | `event_type = 3` |
| Collision | `collision_type IS NOT NULL` |

The doughnut shows each as a percentage of the four-category total.

#### Crash Frequency Over Time

Daily count of collision rows, back-filled so every day in `[t_start, t_end]` has a point (zero where no crashes occurred — prevents misleading x-axis gaps):

```sql
SELECT date_trunc('day', timestamp)::date, COUNT(*)
FROM sensor
WHERE collision_type IS NOT NULL
  AND lat / lon / time filters
GROUP BY 1
```

#### Before vs After Countermeasure

Two `SUM(CASE …)` queries splitting on the user-supplied countermeasure date — one for crashes, one for driving events:

```
before.crashes = Σ (timestamp <  countermeasure_date  AND collision_type IS NOT NULL)
after.crashes  = Σ (timestamp >= countermeasure_date  AND collision_type IS NOT NULL)
before.events  = Σ (timestamp <  countermeasure_date  AND event_type    IS NOT NULL)
after.events   = Σ (timestamp >= countermeasure_date  AND event_type    IS NOT NULL)
```

> *More metrics — daily event rates, ΔCrash %, per-vehicle risk, speed-distribution changes, etc. — will be added here as new analytics cards land in the sidebar.*

### Bottom bar
- Timeline slider spanning the full dataset range (2025-01-31 → 2025-03-31 for the current sample).
- Play / Pause / Stop / Step ± buttons.
- **Exact-time jump** — `datetime-local` input with a Go button. Press **Enter** in the input to jump. Out-of-range values are rejected with a toast; the input is pre-filled with the current playback time.
- **Speed** selector: 1 sec/s (real-time) → 30 min/s.

---

## API Reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Serves `map_report.html` |
| `/api/meta` | GET | Time range, spatial bounds, event / collision label dictionaries, and a suggested "busy window" start timestamp |
| `/api/trajectory` | GET | Sampled per-vehicle waypoints for a time + bbox window. Uses `UNION ALL` so normal-driving rows never crowd out event rows |
| `/api/accidents` | GET | Collision events with severity, G-values, numeric `collision_type`, and human label |
| `/api/vehicle-trajectory` | GET | Full (un-sampled) trajectory for a **single** VIN around a centre time — powers investigation mode |
| `/api/analytics` | GET | Event breakdown, daily crash frequency, before/after comparison, weighted risk score |
| `/api/blackspots` | GET | Configured blackspot locations from `blackspots.json` |
| `/api/predict` | POST | Stub for the ML risk-prediction model (see `PREDICTION_API.md`) |
| `/api/debug` | GET | DB health-check with row counts and sample rows near the data midpoint |

Query-parameter details are documented in each handler's docstring in [`app.py`](app.py).

---

## Project Layout

| File | Purpose |
|---|---|
| `app.py` | FastAPI backend, DuckDB/Postgres shim, all endpoints |
| `dashboard.js` | Map rendering, animation loop, collision list, investigation mode, time-jump, charts |
| `map_report.html` | Dashboard layout (top bar, collision panel, map, analytics, bottom bar) |
| `style.css` | Claude-inspired design system — warm palette, serif/sans pairing, full component styles |
| `db_connection.py` | PostgreSQL helper, reads `.env` |
| `export_to_duckdb.py` | One-shot exporter: Postgres → local `sensor_local.duckdb` |
| `blackspots.json` | Blackspot coordinates and radii |
| `sensor_table.md` | Full data dictionary for the `sensor` table |
| `PREDICTION_API.md` | Contract for ML team integration |
| `CLAUDE.md` | Project context & research notes |

---

## Design Notes

- The UI follows a **warm editorial** aesthetic: cream paper sidebars (`#faf7f2`), warm near-black map chrome, coral accent (`#c7613c`). Source Serif 4 for headings and italic captions, Inter for UI, tabular numerics for every timestamp and metric.
- Numerical values on the map (popup speeds, headings) use real measured values when available; ~77 % of `sensor` rows have NULL `vehicle_speed`, so the frontend also dead-reckons from GPS position delta where needed (see `interp()` in `dashboard.js`).
- Trajectory fetches auto-tune `sample_sec` to the playback speed so the wire payload stays manageable (1 s at real-time, 10 s at 30 min/s).
