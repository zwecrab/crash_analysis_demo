"""
coordinates.py — Geographic boundaries and polygon geometries for L-DCM Crash Risk Analysis.

This module houses all gate vertices, bounding box ranges, and spatial helper functions.
It generates dynamic SQL CASE statements to identify gate regions dynamically, allowing
easy scale-up to new gates or road telemetry datasets without hardcoded changes.
"""

import os
import json
import math

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Gate Polygon Definitions (lat, lon vertices) ─────────────────
GATE_POLYGONS = {
    '1': [[13.8398024, 100.5563432], [13.839059, 100.555904], [13.8388941, 100.5563938], [13.839613, 100.556771]],
    '2': [[13.8400601, 100.5569981], [13.840267, 100.5566172], [13.8401549, 100.5565541], [13.8399455, 100.5569403]],
    '3': [[13.8405286, 100.557234], [13.840707, 100.5568478], [13.8406107, 100.5567995], [13.8404375, 100.5571864]],
    '4': [[13.8412588, 100.5571058], [13.8410703, 100.5575306], [13.8411528, 100.5575752], [13.841343, 100.557146]]
}

# ── Road and Gates Area Bounding Box ──────────────────────────────
# Restricts SQL spatial queries to proximity of coordinates of interest.
ROAD_BOUNDS = {
    "lat_min": 13.8380,
    "lat_max": 13.8420,
    "lon_min": 100.5550,
    "lon_max": 100.5580
}

# ── Load Road Section Configurations ─────────────────────────────
def _load_road_sections():
    path = os.path.join(_DIR, "road.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f).get("sections", [])
        except Exception as e:
            print(f"[coordinates] Error loading road.json: {e}")
    return []

ROAD_SECTIONS = _load_road_sections()
ROAD_POLYGONS = [s["polygon"] for s in ROAD_SECTIONS]

if ROAD_SECTIONS:
    ROAD_BBOX = {
        "lat_min": min(s["lat_min"] for s in ROAD_SECTIONS),
        "lat_max": max(s["lat_max"] for s in ROAD_SECTIONS),
        "lon_min": min(s["lon_min"] for s in ROAD_SECTIONS),
        "lon_max": max(s["lon_max"] for s in ROAD_SECTIONS),
    }
else:
    ROAD_BBOX = None

# ── SRMA Configuration (Section B Warning Zone) ──────────────────
_srma = next((s for s in ROAD_SECTIONS if s["id"] == "B"), None)
if _srma:
    SRMA_POLYGON = _srma["polygon"]
    SRMA_BBOX = {
        "lat_min": _srma["lat_min"],
        "lat_max": _srma["lat_max"],
        "lon_min": _srma["lon_min"],
        "lon_max": _srma["lon_max"]
    }
else:
    # safe fallback matching road.json Section B
    SRMA_POLYGON = [[13.8400061, 100.5569719], [13.8404753, 100.5572063], [13.840653, 100.556822], [13.840211, 100.556587]]
    SRMA_BBOX = {"lat_min": 13.8400061, "lat_max": 13.840653, "lon_min": 100.556587, "lon_max": 100.5572063}


# ── Centerline Projection (for robust gate-line crossing detection) ──
# A gate is treated as a line drawn across the road at a fixed distance ("progress")
# along the centerline. A vehicle crosses the gate whenever its path passes that
# progress value while staying inside the corridor — robust to 1 Hz GPS skipping the
# tiny gate box, yet never counting anything outside the Kamphaeng Phet 6 corridor.
def _load_road_meta():
    path = os.path.join(_DIR, "road.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            print(f"[coordinates] Error loading road.json meta: {e}")
    return {}

_ROAD_META = _load_road_meta()
_CENTERLINE = _ROAD_META.get("centerline", {
    "start": [13.83897655, 100.5561489], "end": [13.8412479, 100.5573606]
})
CORRIDOR_WIDTH_M = _ROAD_META.get("corridor_width_m", 34.5)
CORRIDOR_HALF_M = CORRIDOR_WIDTH_M / 2.0

# Half-width (metres) of the presence band used for EDGE gates that sit at the limit of
# data coverage (e.g. gate 1, where the telemetry starts — no upstream points exist to
# straddle). Kept well under the gate-to-gate spacing so bands never overlap or leave the zone.
GATE_BAND_M = 20.0
# Gates with no telemetry on one side; detected by presence within GATE_BAND_M instead of
# by line-crossing. Computed from where data exists relative to each gate's progress.
EDGE_GATES = {"1"}

# Local equirectangular metres-per-degree at the road's latitude.
_S0 = _CENTERLINE["start"]   # [lat, lon]
_E0 = _CENTERLINE["end"]
_MLAT = 111000.0
_MLON = 111000.0 * math.cos(math.radians((_S0[0] + _E0[0]) / 2.0))
# Centerline unit vector in (east, north) metre space.
_EX = (_E0[1] - _S0[1]) * _MLON
_EY = (_E0[0] - _S0[0]) * _MLAT
_L = math.hypot(_EX, _EY) or 1.0
_UX, _UY = _EX / _L, _EY / _L

def progress_expr(lat: str = "lat", lon: str = "lon") -> tuple[str, str]:
    """Return (along-centerline progress in metres, perpendicular offset in metres)
    SQL expressions for the given lat/lon column names."""
    x = f"(({lon} - {_S0[1]}) * {_MLON})"
    y = f"(({lat} - {_S0[0]}) * {_MLAT})"
    s = f"({x} * {_UX} + {y} * {_UY})"
    d = f"({x} * ({-_UY}) + {y} * {_UX})"
    return s, d

def gate_progress() -> dict:
    """Progress (metres along centerline) of each gate, taken at its polygon centroid."""
    out = {}
    for g, poly in GATE_POLYGONS.items():
        clat = sum(p[0] for p in poly) / len(poly)
        clon = sum(p[1] for p in poly) / len(poly)
        x = (clon - _S0[1]) * _MLON
        y = (clat - _S0[0]) * _MLAT
        out[g] = x * _UX + y * _UY
    return out

GATE_PROGRESS = gate_progress()


# ── Geometric helper functions ────────────────────────────────────

def point_in_polygon(lat: float, lon: float, poly: list) -> bool:
    """Ray-casting algorithm to detect if point (lat, lon) is inside a polygon."""
    inside = False
    n = len(poly)
    for i in range(n):
        j = (i - 1) % n
        yi, xi = poly[i][0], poly[i][1]
        yj, xj = poly[j][0], poly[j][1]
        intersect = ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)
        if intersect:
            inside = not inside
    return inside

def relevant_polygons(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> list:
    """Return matching road section polygon(s) for a given bounding box.
    Uses tight tolerances to avoid section overlaps at borders.
    """
    _TOL = 0.0001
    matches = [
        s for s in ROAD_SECTIONS
        if abs(s["lat_min"] - lat_min) <= _TOL
        and abs(s["lat_max"] - lat_max) <= _TOL
        and abs(s["lon_min"] - lon_min) <= _TOL
        and abs(s["lon_max"] - lon_max) <= _TOL
    ]
    return [matches[0]["polygon"]] if len(matches) == 1 else ROAD_POLYGONS

# ── Dynamic SQL Generator for Gate Classifications ────────────────

# Maps an adjacent directional route to the road section that lies between its two gates.
ROUTE_SECTION = {
    '12': 'A', '21': 'A',
    '23': 'B', '32': 'B',
    '34': 'C', '43': 'C',
}

def section_polygon_for_route(route: str) -> list:
    """Return the polygon of the section that sits strictly between the route's two gates.
    e.g. '23'/'32' -> Section B (between gate 2 and gate 3). Falls back to all road
    polygons combined only if the route or section is unknown.
    """
    sec_id = ROUTE_SECTION.get(route)
    if sec_id:
        sec = next((s for s in ROAD_SECTIONS if s["id"] == sec_id), None)
        if sec:
            return sec["polygon"]
    return None

def polygon_to_wkt(poly: list) -> str:
    """Convert coordinate polygon list to Well-Known Text (WKT) string."""
    if not poly:
        return ""
    pts = [f"{pt[1]} {pt[0]}" for pt in poly]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return f"POLYGON(({', '.join(pts)}))"

def get_gate_sql(use_spatial: bool = False) -> str:
    """Compile GATE_POLYGONS into a clean dynamic SQL CASE statement.
    Computes precise bounding boxes automatically for high-performance pre-filtering.
    When use_spatial is True, every gate (1-4) additionally uses ST_Within against its
    precise polygon. The bbox alone over-counts Gate 1 by ~45% (corners outside the quad),
    so spatial containment matters for accurate gate-crossing detection.
    """
    clauses = []
    for gate, poly in GATE_POLYGONS.items():
        lats = [pt[0] for pt in poly]
        lons = [pt[1] for pt in poly]
        lat_min, lat_max = min(lats), max(lats)
        lon_min, lon_max = min(lons), max(lons)

        if use_spatial:
            wkt = polygon_to_wkt(poly)
            clauses.append(
                f"WHEN lat BETWEEN {lat_min:.7f} AND {lat_max:.7f} AND lon BETWEEN {lon_min:.7f} AND {lon_max:.7f} "
                f"AND ST_Within(ST_Point(lon, lat), ST_GeomFromText('{wkt}')) THEN '{gate}'"
            )
        else:
            clauses.append(
                f"WHEN lat BETWEEN {lat_min:.7f} AND {lat_max:.7f} AND lon BETWEEN {lon_min:.7f} AND {lon_max:.7f} THEN '{gate}'"
            )
            
    # Combine clauses into standard indentation CASE WHEN statement
    return "CASE\n" + "\n".join(f"                {c}" for c in clauses) + "\n                ELSE NULL\n            END"
