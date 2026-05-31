# L-DCM Crash Risk Analysis — "Business As Usual" (BAU) Handover Blueprint

This document serves as the official project handover guide for any developer or AI coding agent taking over the L-DCM Crash Risk Analysis dashboard codebase. It details the project's background, database systems, event mappings, system architecture, modular javascript features, UI/UX behaviors, recent implementations, and existing system constraints.

---

## 1. What This Project Is About

The **L-DCM Crash Risk Analysis Dashboard** is a state-of-the-art telematics and blackspot monitoring system built to analyze vehicle driving behaviors, safety events, and crash risks on a critical segment of **Kamphaeng Phet 6 Road** in Bangkok, Thailand. 

### Core Business Objectives:
* **Blackspot Monitoring**: Visualizes active vehicle tracks, speeds, and heading directions in real-time or via chronological playback.
* **Safety Event Detection**: Identifies harsh braking, sudden acceleration, and sharp turn incidents along the road corridor.
* **Collision Forensic Investigation**: Allows forensically examining vehicle speed, g-forces, and telemetry bursts up to ±30 minutes around a collision event.
* **Before/After Analysis**: Measures the statistical effectiveness of physical road safety interventions (e.g., lane dividers) by comparing crash rates and warning events before and after a specified countermeasure date.
* **Dynamic Route Matrix**: Calculates gate-to-gate traffic flows across six main routes (AB, AC, BA, BC, CA, CB) to isolate where risks are highest.

---

## 2. Database Explanation & Correct Event Mapping

The local backend connects to a high-performance **DuckDB** database engine. DuckDB ensures sub-second analytical queries over large datasets (millions of rows) using vectorized execution.

* **Database File**: `sensor_local.duckdb` (ignored from Git; pre-placed in the project root folder).
* **Primary Table**: `sensor`

### Core Table Schema (`sensor`):
| Column Name | Data Type | Description |
| :--- | :--- | :--- |
| `vin` | `VARCHAR` | Vehicle Identification Number (unique key). |
| `timestamp` | `TIMESTAMP` | Explicit UTC timestamp for the telematics packet. |
| `lat` | `DOUBLE` | Latitude coordinate of the packet. |
| `lon` | `DOUBLE` | Longitude coordinate of the packet. |
| `direction` | `DOUBLE` | Heading direction in degrees ($0^\circ - 360^\circ$ where $0/360 = \text{North}$). |
| `vehicle_speed` | `DOUBLE` | Real-time vehicle speed in km/h. |
| `event_type` | `INTEGER` | Safety alert identifier (see Event Mapping). |
| `collision_type`| `INTEGER` | Collision type identifier (see Collision Mapping). |
| `gx_acci` | `DOUBLE` | Longitudinal acceleration force (burst-only). |
| `gy_acci` | `DOUBLE` | Lateral acceleration force (burst-only). |

---

### Core Mappings & Spatial Geometry

#### A. Event Type Mappings (`event_type`)
* **`1`**: **Sudden Acceleration** (Teal Arrow)
* **`2`**: **Harsh Braking** (Amber Arrow)
* **`3`**: **Sharp Turn** (Purple Arrow)
* **`NULL`**: **Normal Driving** (Blue Arrow)

#### B. Collision Type Mappings (`collision_type`)
* **`16`**: Front-Back Collision (Filter OFF)
* **`17`**: **Front-Back Collision (Driving)** (Red Marker)
* **`18`**: **Front-Back Collision (Idling)** (Red Marker)
* **`32`**: Side Collision (Filter OFF)
* **`33`**: **Side Collision (Driving)** (Red Marker)
* **`34`**: **Side Collision (Idling)** (Red Marker)

#### C. Spatial Gate Boundaries
Spatial bounding boxes are configured as gates to isolate transitions. The backend uses DuckDB's `ST_Within` when spatial extensions are loaded, with a reliable bounding-box coordinate fallback:
* **Gate A (North Entrance/Exit)**: 
  * Bounding Box: `lat BETWEEN 13.8401 AND 13.8403 AND lon BETWEEN 100.5565 AND 100.5569`
* **Gate B (South Entrance/Exit)**: 
  * Bounding Box: `lat BETWEEN 13.8404 AND 13.8407 AND lon BETWEEN 100.5568 AND 100.5571`
* **Gate C (West/Gate C Connector)**: 
  * Bounding Box: `lat BETWEEN 13.8403 AND 13.8405 AND lon BETWEEN 100.5566 AND 100.5569`
* **Active Road Corridor limits**: `lat BETWEEN 13.8380 AND 13.8420 AND lon BETWEEN 100.5550 AND 100.5580`

---

## 3. Technology Stack & Tools Used

### A. Backend (Python 3.11+)
* **FastAPI**: Provides a lightweight, high-performance, asynchronous REST API.
* **DuckDB**: Vectorized SQL query engine used for data sessionization and spatial mapping.
* **Uvicorn**: ASGI web server running local reload sweeps on port `8000`.

### B. Frontend (Vanilla Web)
* **HTML5 & Vanilla CSS3**: Designed with a Claude-inspired warm editorial palette (cream cards `#faf7f2`, display serifs, tabular numerals for perfect alignments).
* **Leaflet.js**: Maps vehicle trajectories, gate overlays, and collision events.
* **Leaflet.heat**: Renders high-performance kernel density overlays for event hotspots.
* **Chart.js**: Generates the dynamic event donut, daily collision frequencies, and before/after countermeasure comparison charts.
* **ES6 Modules**: Modular JS structures (`modules/state.js`, `modules/playback.js`, `modules/routes.js`, etc.) maintain pristine decoupling.

### C. Deployment & Hosting
* **Hugging Face Spaces**: Deployed as a Dockerized Python Space. Automatic builds are triggered on every `git push origin main`.

---

## 4. Code Implemented & Architectural Enhancements

During recent development cycles, several critical mathematical and UX synchronization fixes were implemented to establish clean consistency across the app:

### 1. 60-Minute Telematics Pause-Split Threshold (Sessionization)
* **The Problem**: Consecutive gate crossings occurring hours or days apart (such as a vehicle entering Gate B, parking for 4 hours, then exiting Gate C) were treated as a single massive trip, drawing bypassed diagonal lines across the map.
* **The Fix**: Added the transition window limit `AND c2.timestamp - c1.timestamp <= INTERVAL '60 minutes'` in:
  1. `/api/route-trips` (Vehicle sidebar trips builder)
  2. `/api/route-matrix` (Analytics table compiler)
  3. `/api/heatmap` (Hotspot point cloud loader)
* **Result**: Effectively splits parked or offline vehicles into separate trips while keeping slow-moving bumper-to-bumper traffic jams grouped as a single traversal.

### 2. Distinct Trip-Level Event Counts in Analytics Table
* **The Problem**: The Route Matrix table was previously aggregating every raw event occurrence. If a single vehicle on route `A ➔ C` triggered 5 brakes during a single traversal, the table cell reported `5` but the sidebar only displayed the `3` distinct trips that had brakes, causing confusion.
* **The Fix**: Rewrote the SQL query inside the `/api/route-matrix` endpoint in [app.py](file:///d:/LDCM/L-DCM%20Crash%20Risk%20Analysis/app.py) to aggregate distinct trip IDs containing alerts instead of counting raw occurrences:
  ```sql
  COUNT(DISTINCT CASE WHEN e.event_type = 2 THEN t.vin || '_' || CAST(t.t_start AS VARCHAR) END) as brake,
  COUNT(DISTINCT CASE WHEN e.event_type = 3 THEN t.vin || '_' || CAST(t.t_start AS VARCHAR) END) as turn,
  COUNT(DISTINCT CASE WHEN e.event_type = 1 THEN t.vin || '_' || CAST(t.t_start AS VARCHAR) END) as accel
  ```
* **Result**: The table counts now perfectly match the trip counts and badges shown in the left sidebar.

### 3. Complete Bypassed Trajectory Animation (`S.focusWaypoints`)
* **The Problem**: When a vehicle started outside the circular Full Map boundary, it was filtered out by the background `/api/trajectory` query limits and did not show up on the map.
* **The Fix**: Updated `renderFrame()` in [modules/playback.js](file:///d:/LDCM/L-DCM%20Crash%20Risk%20Analysis/modules/playback.js) to animate the active focused vehicle using `S.focusWaypoints` (populated by the unfiltered `/api/vehicle-trajectory` endpoint) instead of `S.trajs` (which is filtered by spatial bounds).
* **Result**: The focused vehicle will reliably animate from its absolute origin all the way to its destination, even if it starts or exits outside the visible boundaries.

### 4. Focused-Trip Vehicle Isolation
* **The Fix**: When a trip is selected in the sidebar, `renderFrame()` hides all other vehicle markers, leaving only the focused vehicle visible. This provides a clean, focused, and clutter-free review environment.

### 5. Origin-Gate Spawn Alignment
* **The Fix**: When a sidebar trip is selected, [modules/routes.js](file:///d:/LDCM/L-DCM%20Crash%20Risk%20Analysis/modules/routes.js) jumps the timeline start directly to `tStartMs` (trip beginning) rather than the midpoint `tMidMs`. This ensures the vehicle marker spawns precisely on the green `"Trip origin gate"` marker when loaded.

### 6. Dynamic Trajectory Smoothing Toggle
* **The Fix**: Added a button labeled **"Normal Trip"** in the top header. Clicking it toggles `S.smoothingEnabled` to `false` and updates the label to **"Smoothen Trip"**. 
* In [modules/playback.js](file:///d:/LDCM/L-DCM%20Crash%20Risk%20Analysis/modules/playback.js), `interp()` bypasses linear fractional frame blending and vector dead-reckoning when `smoothingEnabled` is off, displaying raw database coordinate changes discretely.

---

## 5. Webapp Functionality & UI/UX Mechanics

The dashboard layout is divided into three primary segments:

### A. Left Sidebar: "Route Trips" Panel
* **Purpose**: Visible only when a route row in the Analytics table is active.
* **Trip Filter**: Dropdown menu allows selecting `All Trips`, `Sudden Acceleration`, `Harsh Braking`, `Sharp Turn`, or `Normal Driving`.
* **Trip List**: Displays vehicle records containing tail-VIN labels, start timestamps, maximum speeds, and semantic alert badges.
* **Trip Highlight**: Selecting a trip triggers playback focus, draws the active trajectory on the map, draws specific event markers, and displays a peach `"Highlighting ···[VIN]"` status bar with a `"Clear"` option.

### B. Center Canvas: Interactive Leaflet Map
* **Clutter-Free Centerlines**: Clicking a route in the Analytics table highlights the centerline path with an animated moving-dash micro-animation, highlighting the starting gate in **Green** (origin) and the ending gate in **Red** (destination).
* **Focused Markers**: Markers represent vehicle headings and are color-coded by event. When a trip is focused, event circles appear exactly where safety thresholds were crossed.
* **Styling Toggle**: A toggle in the top bar switches between a dark CartoDB backdrop and Google Maps Satellite/Road views.

### C. Right Sidebar: "Analytics" Panel
* **Route Analysis Matrix**: Interactive grid showing trip volume and safety incident counts per route.
* **Event Type Breakdown**: Chart.js donut chart illustrating safety alert distributions.
* **Before vs After Comparisons**: Charts showing crash reduction and warning occurrences around a custom date input (interfaced via standard Date picker and CM Apply triggers).

---

## 6. Current Errors & Technical Limitations

Any taking-over agent should note the following constraints to prevent regressions:

1. **DuckDB Local Write Restrictions**: The DuckDB database `sensor_local.duckdb` is read-only. Attempts to write logs or insert mock telemetry points directly to the `sensor` table will fail.
2. **Modulo Sampling for Background Traffic**: To prevent browser performance degradation under high loads, the general `/api/trajectory` endpoint samples data using modulo checks. This means very brief event spikes (e.g. 1-second accelerations) for background (non-focused) vehicles may occasionally skip frames on the map if they fall on odd-modulo seconds. *(Note: This is completely resolved for focused vehicles, which use unfiltered trajectory coordinates).*
3. **Browser Cache Retention on ES6 Modules**: Since files inside the `/modules` folder are ES6 classes, aggressive browser caching may cause outdated scripts to load. We have applied `Cache-Control: no-store, no-cache, must-revalidate` middleware headers in FastAPI, but clients should still run a hard refresh (`Ctrl + F5`) if they suspect local scripts are out of sync.
4. **FastAPI Query Limits**: The `/api/vehicle-trajectory` endpoint enforces a query limit of 5,000 coordinate rows and restricting windows up to 60 minutes to prevent browser canvas and Leaflet crashes.
