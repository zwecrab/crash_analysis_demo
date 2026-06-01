"""
coordinates.py — Geographic boundaries and polygon geometries for L-DCM Crash Risk Analysis.

This module houses all gate vertices, bounding box ranges, and spatial helper functions.
It generates dynamic SQL CASE statements to identify gate regions dynamically, allowing
easy scale-up to new gates or road telemetry datasets without hardcoded changes.
"""

import os
import json

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Gate Polygon Definitions (lat, lon vertices) ─────────────────
GATE_POLYGONS = {
    'A': [[13.8402134, 100.5568106], [13.8402584, 100.5567211], [13.8401686, 100.5566678], [13.8401220, 100.5567560]],
    'B': [[13.8405280, 100.5568707], [13.8404676, 100.5569855], [13.8405560, 100.5570320], [13.8406097, 100.5569150]],
    'C': [[13.8404666, 100.5568276], [13.8405093, 100.5567459], [13.8403466, 100.5566593], [13.8403101, 100.5567385]]
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
    SRMA_POLYGON = [[13.840211, 100.556587], [13.840653, 100.556822], [13.840556, 100.557032], [13.840122, 100.556756]]
    SRMA_BBOX = {"lat_min": 13.840122, "lat_max": 13.840653, "lon_min": 100.556587, "lon_max": 100.557032}


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
    Computes precise bounding boxes automatically for high-performance indexing.
    """
    clauses = []
    for gate, poly in GATE_POLYGONS.items():
        lats = [pt[0] for pt in poly]
        lons = [pt[1] for pt in poly]
        lat_min, lat_max = min(lats), max(lats)
        lon_min, lon_max = min(lons), max(lons)
        
        if use_spatial:
            # Use small bounding box padding (0.0001 deg ~ 11m) as fast pre-index
            pad = 0.0001
            lat_min_p, lat_max_p = lat_min - pad, lat_max + pad
            lon_min_p, lon_max_p = lon_min - pad, lon_max + pad
            wkt = polygon_to_wkt(poly)
            
            clauses.append(
                f"WHEN lat BETWEEN {lat_min_p:.7f} AND {lat_max_p:.7f} AND lon BETWEEN {lon_min_p:.7f} AND {lon_max_p:.7f} "
                f"AND ST_Within(ST_Point(lon, lat), ST_GeomFromText('{wkt}')) THEN '{gate}'"
            )
        else:
            clauses.append(
                f"WHEN lat BETWEEN {lat_min:.7f} AND {lat_max:.7f} AND lon BETWEEN {lon_min:.7f} AND {lon_max:.7f} THEN '{gate}'"
            )
            
    # Combine clauses into standard indentation CASE WHEN statement
    return "CASE\n" + "\n".join(f"                {c}" for c in clauses) + "\n                ELSE NULL\n            END"
