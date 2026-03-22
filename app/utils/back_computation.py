#back_computation.py
import math
from shapely.geometry import Polygon, Point


# -------------------------------
# Bearing helpers
# -------------------------------

def bearing_deg(p1: Point, p2: Point) -> float:
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    return (math.degrees(math.atan2(dx, dy)) + 360) % 360


def deg_to_dms(angle_deg: float) -> str:
    angle_deg = angle_deg % 360
    deg = int(angle_deg)
    min_float = (angle_deg - deg) * 60
    minute = int(min_float)
    sec = (min_float - minute) * 60
    return f"{deg:03d}°{minute:02d}'{sec:05.2f}\""


def _clockwise_ring_coords_and_labels(poly: Polygon, station_names=None):
    coords = list(poly.exterior.coords)
    if len(coords) < 2:
        return coords, []

    vertex_count = max(0, len(coords) - 1)
    default_stations = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    labels = []
    for idx in range(vertex_count):
        if station_names and idx < len(station_names):
            raw = station_names[idx]
        else:
            raw = default_stations[idx % len(default_stations)]
        label = str(raw or "").strip() or default_stations[idx % len(default_stations)]
        labels.append(label)

    try:
        is_ccw = bool(poly.exterior.is_ccw)
    except Exception:
        is_ccw = False

    if not is_ccw or vertex_count <= 2:
        return coords, labels

    body = coords[:-1]
    reordered_body = [body[0], *reversed(body[1:])]
    reordered_coords = reordered_body + [reordered_body[0]]
    reordered_labels = [labels[0], *reversed(labels[1:])] if labels else labels
    return reordered_coords, reordered_labels


# -------------------------------
# Back computation
# -------------------------------

def compute_back_computation(poly: Polygon, station_names=None):
    coords, stations = _clockwise_ring_coords_and_labels(poly, station_names=station_names)

    rows = []
    sum_de = 0.0
    sum_dn = 0.0

    for i in range(len(coords) - 1):
        p1 = Point(coords[i])
        p2 = Point(coords[i + 1])

        de = p2.x - p1.x
        dn = p2.y - p1.y

        dist = math.hypot(de, dn)
        fb = bearing_deg(p1, p2)
        bb = (fb + 180) % 360

        sum_de += de
        sum_dn += dn

        rows.append({
            "from": stations[i % len(stations)],
            "to": stations[(i + 1) % len(stations)],
            "E": round(p1.x, 3),
            "N": round(p1.y, 3),
            "dE": round(de, 3),
            "dN": round(dn, 3),
            "distance": round(dist, 3),
            "fb": deg_to_dms(fb),
            "bb": deg_to_dms(bb),
        })

    return rows, round(sum_de, 3), round(sum_dn, 3)
