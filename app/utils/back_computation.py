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


# -------------------------------
# Back computation
# -------------------------------

def compute_back_computation(poly: Polygon):
    coords = list(poly.exterior.coords)
    stations = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

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
            "from": stations[i % 26],
            "to": stations[(i + 1) % 26],
            "E": round(p1.x, 3),
            "N": round(p1.y, 3),
            "dE": round(de, 3),
            "dN": round(dn, 3),
            "distance": round(dist, 3),
            "fb": deg_to_dms(fb),
            "bb": deg_to_dms(bb),
        })

    return rows, round(sum_de, 3), round(sum_dn, 3)
