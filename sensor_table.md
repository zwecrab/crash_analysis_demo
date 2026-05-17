# `sensor` Table — Data Dictionary

**Database:** `carcrash`  
**Schema:** `public`  
**Source system:** L-DCM (Data Communication Module) — Toyota Connected Asia Pacific (TCAP)  
**Estimated rows:** ~91,216,712 (~91 million records)

---

## Overview

The `sensor` table is the central fact table of the **L-DCM Crash Risk Analysis** project. It stores raw vehicle probe telemetry uploaded by Toyota vehicles equipped with an L-DCM device. Each row represents a single measurement snapshot from one vehicle, captured at **1-second frequency** for normal driving (BasicData / 0x11) or **0.1-second frequency** during a collision event (AccidentData / 0x32).

Data flows from the vehicle → TCAP Device Server → OneDrive → Linux/TMC server → this PostgreSQL table. VINs in this table are **masked** (converted to anonymized identifiers like `VRD000...`) before ingestion.

---

## Column Reference

| # | Column | Type | Nullable | Default | Description |
|---|--------|------|----------|---------|-------------|
| 1 | `id` | `integer` | ✗ | `nextval('sensor_id_seq')` | Auto-incrementing surrogate primary key. |
| 2 | `vin` | `varchar` | ✗ | — | **Masked Vehicle Identification Number.** Original VIN is anonymized before storage (e.g., `VRD00000000072990`). One VIN corresponds to one physical vehicle. |
| 3 | `timestamp` | `timestamp` | ✗ | — | UTC timestamp of the measurement. Format: `YYYY-MM-DD HH:MM:SS`. For BasicData records, frequency is **1 second**; for AccidentData records, frequency is **0.1 second**. |
| 4 | `lat` | `numeric` | ✗ | — | **GPS Latitude** in decimal degrees (WGS 84). Coverage area is Thailand (approx. 5°N – 21°N). |
| 5 | `lon` | `numeric` | ✗ | — | **GPS Longitude** in decimal degrees (WGS 84). Coverage area is Thailand (approx. 97°E – 106°E). |
| 6 | `direction` | `integer` | ✗ | — | **Vehicle heading direction** in degrees. North = `0`, clockwise. Range: `0`–`359`. |
| 7 | `gy_phyd` | `integer` | ✓ | — | **Lateral G-value (Y-axis) — PHYD data (0x21).** Measures lateral acceleration during normal driving for Pay-How-You-Drive analysis. `NULL` when no PHYD event recorded for this second. |
| 8 | `gx_phyd` | `integer` | ✓ | — | **Longitudinal G-value (X-axis) — PHYD data (0x21).** Measures forward/braking acceleration during normal driving. `NULL` when no PHYD event. |
| 9 | `gy_acci` | `integer` | ✓ | — | **Lateral G-value (Y-axis) — Accident data (0x32).** Recorded at 0.1-second resolution when a collision is detected. `NULL` for non-collision rows. |
| 10 | `gx_acci` | `integer` | ✓ | — | **Longitudinal G-value (X-axis) — Accident data (0x32).** Recorded at 0.1-second resolution during collision. `NULL` for non-collision rows. |
| 11 | `event_type` | `integer` | ✓ | — | **PHYD event type code.** Encodes the type of driving behaviour event detected. See [Event Type Codes](#event-type-codes) below. `NULL` when no event. |
| 12 | `collision_type` | `integer` | ✓ | — | **Collision type code** from AccidentData (0x32). Encodes the direction of impact. See [Collision Type Codes](#collision-type-codes) below. `NULL` for non-collision rows. |
| 13 | `vehicle_speed` | `integer` | ✓ | — | **Vehicle speed in km/h** at the time of the record. Present in BasicData (0x11) and AccidentData (0x32). `NULL` when not transmitted. |

---

## Primary Key

```sql
PRIMARY KEY (id)
```

Auto-generated via sequence `sensor_id_seq`. Not a natural key — queries should join on `vin` + `timestamp` for vehicle-level analysis.

---

## Indexes

| Index Name | Column(s) | Type | Purpose |
|---|---|---|---|
| `sensor_pkey` | `id` | B-tree (Unique) | Primary key lookup |
| `ix_sensor_vin` | `vin` | B-tree | Filter/join by vehicle identity |
| `ix_sensor_timestamp` | `timestamp` | B-tree | Time-range queries, temporal ordering |
| `ix_sensor_lat` | `lat` | B-tree | Geospatial bounding-box filters (latitude) |
| `ix_sensor_lon` | `lon` | B-tree | Geospatial bounding-box filters (longitude) |

> **Note:** There is no composite spatial index. For fast geo-queries (e.g., vehicles within 500 m of a blackspot), consider a PostGIS `GIST` index on a `geometry` column, or filter with `lat` + `lon` indexes together.

---

## Event Type Codes

`event_type` comes from the PHYD data stream (0x21). Values are hexadecimal codes stored as integers.

| Value (int) | Hex | Description |
|---|---|---|
| `16` | `0x10` | Harsh Braking / Front-Back event (speed filter OFF) |
| `32` | `0x20` | Sudden Acceleration / Side event (speed filter OFF) |
| `17` | `0x11` | Front-Back Collision — Driving (speed filter ON) |
| `18` | `0x12` | Front-Back Collision — Idling (speed filter ON) |
| `33` | `0x21` | Side Collision — Driving (speed filter ON) |
| `34` | `0x22` | Side Collision — Idling (speed filter ON) |
| `NULL` | — | No PHYD event recorded for this second |

---

## Collision Type Codes

`collision_type` comes from the AccidentData stream (0x32), triggered when a G-value threshold is exceeded.

| Value (int) | Hex | Description |
|---|---|---|
| `16` | `0x10` | **Front-Back Collision** (speed filter OFF) |
| `32` | `0x20` | **Side Collision** (speed filter OFF) |
| `17` | `0x11` | Front-Back Collision — Driving |
| `18` | `0x12` | Front-Back Collision — Idling |
| `33` | `0x21` | Side Collision — Driving |
| `34` | `0x22` | Side Collision — Idling |
| `NULL` | — | Not a collision event record |

---

## G-Value Notes

- **Axis convention:** `GY` = lateral (side-to-side), `GX` = longitudinal (front-to-back).
- **Units:** Raw integer values (internal L-DCM units). Example from spec: `GY = -1`, `GX = 84` for a recorded collision.
- **Threshold:** G-value thresholds for event/collision detection are configurable and were noted as "to be confirmed" in the data format spec (as of 2025-02-25).
- **Two G columns per axis** (`_phyd` vs `_acci`) reflect the two different data streams (0x21 vs 0x32) merged into a single table row; in practice only one pair will be populated per record.

---

## Sample Records

```
id=51417406 | vin=VRD00000000072990 | ts=2025-03-12 22:40:18
  lat=13.841926 | lon=100.558340 | direction=18
  gy_phyd=NULL | gx_phyd=NULL | gy_acci=NULL | gx_acci=NULL
  event_type=NULL | collision_type=NULL | vehicle_speed=96

id=51417407 | vin=VRD00000000347860 | ts=2025-03-15 13:05:09
  lat=13.843529 | lon=100.559395 | direction=27
  gy_phyd=NULL | gx_phyd=NULL | gy_acci=NULL | gx_acci=NULL
  event_type=NULL | collision_type=NULL | vehicle_speed=NULL

id=51417408 | vin=VRD00000000199453 | ts=2025-03-10 08:57:34
  lat=13.843675 | lon=100.559200 | direction=27
  gy_phyd=NULL | gx_phyd=NULL | gy_acci=NULL | gx_acci=NULL
  event_type=NULL | collision_type=NULL | vehicle_speed=41
```

The samples above are from the **BasicData (0x11)** stream — standard 1-second probe records with no PHYD/accident event triggered.

---

## Data Streams Merged in This Table

The `sensor` table consolidates **three L-DCM data streams** into a unified schema:

| L-DCM Data Type | Hex Code | Frequency | Columns Used |
|---|---|---|---|
| Basic probe data | `0x11` | 1 second | `vin, timestamp, lat, lon, direction, vehicle_speed` |
| PHYD (driving behaviour) | `0x21` | 1 second (on event) | `+ gy_phyd, gx_phyd, event_type` |
| Accident / Collision | `0x32` | 0.1 second (on event) | `+ gy_acci, gx_acci, collision_type` |

---

## Null Patterns

Most rows are **BasicData** records — `gy_phyd`, `gx_phyd`, `gy_acci`, `gx_acci`, `event_type`, and `collision_type` are `NULL` for the vast majority of rows. Only event rows populate those fields.

---

## Project Context

This table feeds the **L-DCM Crash Risk Analysis** pipeline at Toyota Motor Corporation (TMC):

1. Toyota vehicles upload telemetry every 1 second via the L-DCM module.
2. TCAP extracts data for **blackspot locations** (9 risk spots, 5 km radius).
3. Data is ingested into this PostgreSQL table on the TMC Linux server.
4. Python analysis code queries this table to compute crash risk metrics (harsh braking, sudden acceleration, speed, collision events) and visualizes results on a map dashboard.

---

*Generated: 2026-04-22 | Source: `carcrash.public.sensor` + `artifects/LDCMdata_format_20250225.xlsx`*
