"""DGPS staking CSV formatting shared by Survey workflows."""

import csv
import io
from collections.abc import Iterable, Mapping
from typing import Any


def alpha_station(index: int) -> str:
    """Return conventional alphabetical station labels: A..Z, AA.."""
    label = ""
    value = int(index)
    while True:
        label = chr(65 + (value % 26)) + label
        value = (value // 26) - 1
        if value < 0:
            return label


def format_coordinate_number(value: float, decimals: int) -> str:
    return f"{float(value):.{decimals}f}"


def render_dgps_staking_csv(rows: Iterable[Mapping[str, Any]], *, raw: bool = False) -> str:
    """Render a receiver-safe CSV, optionally omitting Excel's delimiter hint."""
    buffer = io.StringIO(newline="")
    if not raw:
        buffer.write("sep=,\r\n")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["Type", "Station", "Feature", "Coordinate System", "Easting (m)", "Northing (m)", "Longitude", "Latitude"])
    for row in rows:
        writer.writerow([
            row.get("point_type", ""),
            row["station"],
            row["feature"],
            str(row["coordinate_system"]).upper(),
            format_coordinate_number(row["easting"], 4),
            format_coordinate_number(row["northing"], 4),
            format_coordinate_number(row["longitude"], 8),
            format_coordinate_number(row["latitude"], 8),
        ])
    return "\ufeff" + buffer.getvalue()
