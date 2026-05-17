import duckdb
import os
import math

db_path = r"d:\LDCM\L-DCM Crash Risk Analysis\sensor_local.duckdb"
conn = duckdb.connect(db_path, read_only=True)

# Define the three zones as polygons
zones = {
    "Area before SRMA": [
        [13.839059, 100.555904],
        [13.840211, 100.556587],
        [13.840122, 100.556756],
        [13.838954, 100.556206]
    ],
    "SRMA": [
        [13.840211, 100.556587],
        [13.840653, 100.556822],
        [13.840556, 100.557032],
        [13.840122, 100.556756]
    ],
    "Area after SRMA": [
        [13.840653, 100.556822],
        [13.841343, 100.557146],
        [13.841244, 100.557370],
        [13.840556, 100.557032]
    ]
}

def point_in_polygon(lat, lon, poly):
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

# Fetch relevant telemetry records
print("Fetching vehicle records from bounding box...")
lat_min, lat_max = 13.838954, 13.841343
lon_min, lon_max = 100.555904, 100.557370

records = conn.execute("""
    SELECT vin, timestamp, lat, lon, vehicle_speed, event_type, collision_type
    FROM sensor
    WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
""", [lat_min, lat_max, lon_min, lon_max]).fetchall()

print(f"Fetched {len(records):,} records. Processing point-in-polygon mapping...")

# Structure to store stats
stats = {name: {
    "vins": set(),
    "speeds": [],
    "braking": 0,
    "accel": 0,
    "turn": 0,
    "collision": 0,
    "hb_hours": []
} for name in zones.keys()}

outside_vins = set()
outside_records = 0

for vin, timestamp, lat, lon, speed, event_type, collision_type in records:
    matched = False
    for name, poly in zones.items():
        if point_in_polygon(lat, lon, poly):
            matched = True
            stats[name]["vins"].add(vin)
            if speed is not None:
                stats[name]["speeds"].append(speed)
            if event_type == 1:
                stats[name]["braking"] += 1
                stats[name]["hb_hours"].append(timestamp)
            elif event_type == 2:
                stats[name]["accel"] += 1
            elif event_type == 3:
                stats[name]["turn"] += 1
            if collision_type is not None:
                stats[name]["collision"] += 1
            break
    if not matched:
        outside_vins.add(vin)
        outside_records += 1

print("\n=== Real-World Traffic and Behavior Analysis ===")
print(f"{'Zone Corridor':<20} | {'VINs':<6} | {'Braking':<7} | {'Accel':<5} | {'Turn':<4} | {'Coll':<4} | {'Avg Spd':<7} | {'Min Spd':<7} | {'Max Spd':<7} | {'Event Rate':<10}")
print("-" * 110)

for name, data in stats.items():
    vins_count = len(data["vins"])
    hb = data["braking"]
    sa = data["accel"]
    st = data["turn"]
    co = data["collision"]
    
    speeds = data["speeds"]
    if len(speeds) > 0:
        avg_speed = sum(speeds) / len(speeds)
        min_speed = min(speeds)
        max_speed = max(speeds)
    else:
        avg_speed = min_speed = max_speed = 0.0
        
    # Event rate definition: Braking Events / Distinct Vehicle Volume
    event_rate = (hb / vins_count * 100.0) if vins_count > 0 else 0.0
    
    print(f"{name:<20} | {vins_count:<6} | {hb:<7} | {sa:<5} | {st:<4} | {co:<4} | {avg_speed:<7.1f} | {min_speed:<7.1f} | {max_speed:<7.1f} | {event_rate:<9.2f}%")

print("\n=== Time of Day Distribution for Harsh Braking (Event 1) ===")

time_categories = {
    "Morning Peak (07:00-09:00)": 0,
    "Evening Peak (16:30-19:30)": 0,
    "Night Window (22:00-06:00)": 0,
    "Off-Peak Hours": 0
}

hourly_histogram = {h: 0 for h in range(24)}

for name, data in stats.items():
    for ts in data["hb_hours"]:
        # Add 7 hours to convert UTC to Bangkok time
        local_ts = ts + os.sys.modules['datetime'].timedelta(hours=7)
        hour = local_ts.hour
        minute = local_ts.minute
        
        hourly_histogram[hour] += 1
        
        # Categorize peak/off-peak precisely
        total_minutes = hour * 60 + minute
        if 7 * 60 <= total_minutes <= 9 * 60:
            time_categories["Morning Peak (07:00-09:00)"] += 1
        elif 16 * 60 + 30 <= total_minutes <= 19 * 60 + 30:
            time_categories["Evening Peak (16:30-19:30)"] += 1
        elif 22 * 60 <= total_minutes or total_minutes <= 6 * 60:
            time_categories["Night Window (22:00-06:00)"] += 1
        else:
            time_categories["Off-Peak Hours"] += 1

for cat, count in time_categories.items():
    pct = (count / sum(time_categories.values()) * 100) if sum(time_categories.values()) > 0 else 0
    print(f"  {cat:<26}: {count:<3} ({pct:.1f}%)")

print("\n=== Hourly Histogram of Harsh Braking Events (Bangkok Local Time) ===")
for hr in sorted(hourly_histogram.keys()):
    count = hourly_histogram[hr]
    if count > 0:
        bar = "*" * (count // 5)
        print(f"  {hr:02d}:00 - {hr:02d}:59 : {count:<3} events {bar}")

conn.close()
