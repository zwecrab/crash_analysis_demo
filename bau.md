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
* **Dynamic Bidirectional Route Matrix**: Calculates gate-to-gate traffic flows across two main combined routes: **`A ↔ C`** (North ↔ West) and **`B ↔ C`** (South ↔ West) to isolate where risks are highest.
* **Toggleable Heatmap Layer**: Evaluates driving event density as a toggleable map overlay sitting non-disruptively under vehicle animations.

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
Gate coordinate boundaries and spatial math are defined cleanly in `coordinates.py` for maximum reuse:
* **Gate A (North Entrance/Exit)**: 
  * Vertices: `[[13.8402134, 100.5568106], [13.8402584, 100.5567211], [13.8401686, 100.5566678], [13.8401220, 100.5567560]]`
* **Gate B (South Entrance/Exit)**: 
  * Vertices: `[[13.8405280, 100.5568707], [13.8404676, 100.5569855], [13.8405560, 100.5570320], [13.8406097, 100.5569150]]`
* **Gate C (West/Gate C Connector)**: 
  * Vertices: `[[13.8404666, 100.5568276], [13.8405093, 100.5567459], [13.8403466, 100.5566593], [13.8403101, 100.5567385]]`
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
* **Hugging Face Spaces**: Deployed as a Dockerized Python Space. Automatic builds are triggered on every `git push origin main`. (Note: Pushes are currently withheld and kept local for review).

---

## 4. Code Implemented & Architectural Enhancements

Recent development cycles refactored the app into a premium, decoupled architecture and consolidated routes for maximum versatility:

### 1. Code Decoupling & Modularization (Task 4)
* **The Refactoring**: Removed all hardcoded coordinates, bounding boxes, and raw SQL queries from the main router in `app.py`.
* **The Modules**:
  * `coordinates.py`: Standardizes gate geometries and polygon-containment filters. Includes a **dynamic SQL gate clause compiler** (`get_gate_sql()`) which automatically computes precise gate bounding boxes and spatial checks from polygons, allowing instant scaling to new gate positions or datasets.
  * `sql_schemas.py`: Houses clean, parameterized Python query functions, completely abstracting data access from endpoint routing.
* **Result**: `app.py` is now a pure, lightweight endpoint shell, highly readable and reusable.

### 2. Consolidated Bidirectional Routes (Task 1)
* **The Change**: Disabled the `A ➔ B` and `B ➔ A` routes which represent bypass highway traffic. Combined `A ➔ C` and `C ➔ A` into a single bidirectional route **`A ↔ C`** (North ↔ West), and `B ➔ C` and `C ➔ B` into **`B ↔ C`** (South ↔ West).
* **Backend Aggregations**: Updated `sql_schemas.py` and `app.py` to aggregate bidirectional transitions and count distinct trip IDs. `/api/route-trips` returns the specific `origin` and `destination` fields for each crossing.
* **Sidebar Details**: The sidebar trips list displays the actual direction of travel (e.g. **`A ➔ C`** or **`C ➔ A`**) in the trip metadata for complete clarity.

### 3. Non-Disruptive Heatmap Overlay (Task 2)
* **The Change**: Refactored the heatmap from an exclusive screen "view mode" into a **toggleable layer overlay** (`S.heatmapEnabled`). Toggling it ON shows the floating `#heatmap-panel` filter control card, and toggling it OFF hides it.
* **Continuous Playback**: Because `S.mode` remains in its native mode (`'full'` or `'road'`), clicking the heatmap toggle does **not** stop the timeline, does **not** hide vehicle markers, and does **not** hide accident pins. The heatmap sits in a dedicated map pane (`heatmapPane`, `zIndex: 450`) below vehicle markers but above the base map layer, ensuring perfect layered stacking.

### 4. Context-Specific Heatmap Filtering (Task 3)
* **Road Section Cropping**: Clicking a breakdown card in road mode calls `setActiveSection(id)` which automatically triggers `renderHeatLayer(S.heatmapPoints)`, instantly cropping the active heatmap points to show only inside that section's boundary in the browser on the fly.
* **Route Traversal Filtering**: Clicking a Route Matrix row (`A ↔ C` or `B ↔ C`) filters the heatmap strictly to events recorded during those specific bidirectional route transitions (`AC` or `BC`) by querying `/api/heatmap?route=...` from the backend.

### 5. Origin-Gate Spawn Alignment & Trajectory Cropping
* **The Math**: Previously, start/end markers were drawn at the absolute beginning and end of the fetched 10-minute trajectory window (which includes driving before/after gate crossings).
* **The Fix**: We updated `drawTripFocusedTrack()` in `modules/routes.js` to locate the exact waypoints closest to `t_start` and `t_end`. The start/end markers are placed precisely at these gate crossing coordinates, and the Coral trajectory line is cropped strictly between these points. This ensures the vehicle marker spawns precisely on the green `"Trip origin gate"` marker when loaded, and moves exactly along the highlighted path.

### 6. Phase 2: Per-Section Breakdown Route Filtering & SRMA Event Restrictions
* **Per-Section Breakdown Route Filtering**: When a bidirectional route (e.g. `A ↔ C` or `B ↔ C`) is active in the Route Matrix, the section tiles at the bottom are automatically filtered to show only safety events recorded for vehicles during their active crossing. The backend `/api/analytics` endpoint is updated to support this transition CTE filter, and the frontend ES6 modules automatically sync fetches on route selections or clears.
* **SRMA Event Restrictions**: To target marking effectiveness, safety warnings (`harsh_braking`, `sudden_acceleration`, and `sharp_turn`) in both the Route Analysis Matrix and trips sidebar details lists are restricted strictly to coordinates inside the **SRMA (Section B) Warning Zone** polygon and bounding box (dynamically loaded from `coordinates.py`), while normal traversals (Trips) remain open over the wide corridor.

---

## 5. Webapp Functionality & UI/UX Mechanics

The dashboard layout is divided into three primary segments:

### A. Left Sidebar: "Route Trips" Panel
* **Purpose**: Visible only when a route row in the Analytics table is active.
* **Trip Filter**: Dropdown menu allows selecting `All Trips`, `Sudden Acceleration`, `Harsh Braking`, `Sharp Turn`, or `Normal Driving`.
* **Trip List**: Displays vehicle records containing tail-VIN labels, start timestamps, direction of travel (e.g. `C ➔ A`), maximum speeds, and semantic alert badges.
* **Trip Highlight**: Selecting a trip triggers playback focus, draws the active trajectory on the map, draws specific event markers, and displays a peach `"Highlighting ···[VIN]"` status bar with a `"Clear"` option.

### B. Center Canvas: Interactive Leaflet Map
* **Clutter-Free Centerlines**: Clicking a route in the Analytics table highlights the centerline path with an animated moving-dash micro-animation, highlighting the starting gate in **Green** (origin) and the ending gate in **Red** (destination).
* **Focused Markers**: Markers represent vehicle headings and are color-coded by event. When a trip is focused, event circles appear exactly where safety thresholds were crossed.
* **Styling Toggle**: A toggle in the top bar switches between a dark CartoDB backdrop and Google Maps Satellite/Road views.

### C. Right Sidebar: "Analytics" Panel
* **Route Analysis Matrix**: Interactive 2-row grid (`A ↔ C` and `B ↔ C`) showing trip volume and safety incident counts per route.
* **Event Type Breakdown**: Chart.js donut chart illustrating safety alert distributions.
* **Before vs After Comparisons**: Charts showing crash reduction and warning occurrences around a custom date input (interfaced via standard Date picker and CM Apply triggers).

---

## 6. Current Errors & Technical Limitations

Any taking-over agent should note the following constraints to prevent regressions:

1. **DuckDB Local Write Restrictions**: The DuckDB database `sensor_local.duckdb` is read-only.
2. **Modulo Sampling for Background Traffic**: Modulo-sampling is used to prevent browser performance degradation under high loads, but focused vehicles are animated using complete, unfiltered trajectory coordinates.
3. **Browser Cache Retention on ES6 Modules**: aggressive browser caching may cause outdated scripts to load. Use `Ctrl + F5` if scripts seem out of sync.
4. **FastAPI Query Limits**: The `/api/vehicle-trajectory` endpoint enforces a query limit of 5,000 coordinate rows and restricts windows to 60 minutes to prevent browser crashes.
