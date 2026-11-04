from app.routers.estates import (
    _CSV_COORDINATE_KEYS,
    _CSV_PLOT_NUMBER_KEYS,
    _CSV_X_KEYS,
    _CSV_Y_KEYS,
    _first_csv_value,
    _csv_number,
    _import_boundary_candidate,
    _import_candidate,
    _geometry_coordinate_sample,
    _infer_estate_import_source_crs,
    _is_placeholder_plot_identifier,
    _transform_import_geometry,
    _repeated_csv_plot_key,
    _polygonize_dxf_linework,
    _read_estate_csv_records,
    _normalize_csv_records,
    _plot_number_from_georeference_feature,
    _polygon_geometry_from_georeference_feature,
)
import json
from types import SimpleNamespace
from io import BytesIO

from openpyxl import Workbook
from shapely.geometry import Polygon
from app.routers import estates as estates_router


def test_normalize_csv_records_matches_capitalized_excel_headers():
    raw = [{"Plot Number": "B-024", "Longitude": "7.1", "Latitude": "9.1"}]
    normalized = _normalize_csv_records(raw)
    assert normalized == [{"plotnumber": "B-024", "longitude": "7.1", "latitude": "9.1"}]


def test_normalize_csv_records_strips_units_and_spacing():
    raw = [{" Easting (m) ": "241123.5", "Northing (m)": "1006432.1"}]
    normalized = _normalize_csv_records(raw)
    assert normalized == [{"eastingm": "241123.5", "northingm": "1006432.1"}]


def test_estate_csv_reader_accepts_semicolon_delimited_survey_exports():
    records = _read_estate_csv_records(
        "Plot Number;Easting (m);Northing (m)\nP-001;241123.5;1006432.1\n".encode()
    )
    normalized = _normalize_csv_records(records)
    assert _first_csv_value(normalized[0], _CSV_PLOT_NUMBER_KEYS) == "P-001"
    assert _first_csv_value(normalized[0], _CSV_X_KEYS) == "241123.5"
    assert _first_csv_value(normalized[0], _CSV_Y_KEYS) == "1006432.1"


def test_estate_csv_reader_accepts_coordinate_pair_alias():
    normalized = _normalize_csv_records([{"Parcel ID": "P-002", "Coordinates": "7.10, 9.10"}])[0]
    assert _first_csv_value(normalized, _CSV_PLOT_NUMBER_KEYS) == "P-002"
    assert _first_csv_value(normalized, _CSV_COORDINATE_KEYS) == "7.10, 9.10"


def test_estate_csv_header_aliases_cover_surveyor_unit_labels():
    normalized = _normalize_csv_records([{
        "Plot No.": "B-024",
        "Easting (metres)": "241123.5",
        "Northing (metres)": "1006432.1",
    }])[0]
    assert _first_csv_value(normalized, _CSV_PLOT_NUMBER_KEYS) == "B-024"
    assert _first_csv_value(normalized, _CSV_X_KEYS, fuzzy=True) == "241123.5"
    assert _first_csv_value(normalized, _CSV_Y_KEYS, fuzzy=True) == "1006432.1"


def test_repeated_point_label_can_group_a_parcel_but_unique_points_cannot():
    repeated = _normalize_csv_records([
        {"Point": "P-001", "X (m)": "1", "Y (m)": "2"},
        {"Point": "P-001", "X (m)": "2", "Y (m)": "2"},
        {"Point": "P-001", "X (m)": "2", "Y (m)": "1"},
    ])
    assert _repeated_csv_plot_key(repeated) == "point"

    unique = _normalize_csv_records([
        {"Point": "A", "X (m)": "1", "Y (m)": "2"},
        {"Point": "B", "X (m)": "2", "Y (m)": "2"},
        {"Point": "C", "X (m)": "2", "Y (m)": "1"},
    ])
    assert _repeated_csv_plot_key(unique) is None


def test_csv_number_accepts_decimal_comma_and_thousands_separator():
    assert _csv_number("241123,5") == 241123.5
    assert _csv_number("241,123.5") == 241123.5


def test_import_candidate_does_not_accept_a_multipart_repair():
    candidate = _import_candidate(
        row_number=1,
        plot_number="P-001",
        geometry={
            "type": "Polygon",
            "coordinates": [[
                [0, 0], [4, 0], [4, 4], [0, 4], [2, 1], [1, 3], [0, 0],
            ]],
        },
    )
    assert candidate["valid"] is False
    assert candidate["issues"]

def test_import_candidate_records_a_warning_when_repair_is_one_polygon(monkeypatch):
    # Exercise the repair branch deterministically without accepting a real multipart/bow-tie
    # source geometry. Production data is accepted only when GEOS returns one valid Polygon.
    monkeypatch.setattr(estates_router, "make_valid", lambda _source: Polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)]))
    candidate = _import_candidate(
        row_number=1,
        plot_number="P-001",
        geometry={
            "type": "Polygon",
            "coordinates": [[
                [0, 0], [4, 4], [0, 4], [4, 0], [0, 0],
            ]],
        },
    )
    assert candidate["valid"] is True
    assert candidate["warnings"]


def test_placeholder_boundary_self_intersection_is_reordered_for_review():
    candidate = _import_boundary_candidate(
        row_number=1,
        geometry={
            "type": "Polygon",
            "coordinates": [[
                [0, 0], [4, 4], [0, 4], [4, 0], [0, 0],
            ]],
        },
    )
    assert candidate["valid"] is True
    assert any("reordered" in warning for warning in candidate["warnings"])


def test_projected_geometry_column_is_available_for_crs_detection():
    geometry = {
        "type": "Polygon",
        "coordinates": [[
            [217549.731303332, 1027157.73696176],
            [217559.731303332, 1027157.73696176],
            [217559.731303332, 1027167.73696176],
            [217549.731303332, 1027167.73696176],
            [217549.731303332, 1027157.73696176],
        ]],
    }
    assert _geometry_coordinate_sample(geometry) == (217549.731303332, 1027157.73696176)
    source_crs, detected = _infer_estate_import_source_crs(
        SimpleNamespace(boundary=None),
        [{"geometry": json.dumps(geometry)}],
        "EPSG:4326",
    )
    assert source_crs == "EPSG:32632"
    assert detected is True
    transformed = _transform_import_geometry(geometry, source_crs)
    assert abs(transformed["coordinates"][0][0][0]) <= 180
    assert abs(transformed["coordinates"][0][0][1]) <= 90


def test_placeholder_instrument_identifier_is_not_treated_as_a_plot_number():
    assert _is_placeholder_plot_identifier("0.0000") is True
    assert _is_placeholder_plot_identifier("P-001") is False
    assert _is_placeholder_plot_identifier("") is True


def test_projected_estate_csv_defaults_to_a_nigerian_utm_source():
    source_crs, detected = _infer_estate_import_source_crs(
        SimpleNamespace(boundary=None),
        [{"easting": "217549.731303332", "northing": "1027157.73696176"}],
        "EPSG:4326",
    )
    assert source_crs == "EPSG:32632"
    assert detected is True


def test_explicit_import_crs_is_never_overridden():
    source_crs, detected = _infer_estate_import_source_crs(
        SimpleNamespace(boundary=None),
        [{"easting": "217549.731303332", "northing": "1027157.73696176"}],
        "EPSG:32633",
    )
    assert source_crs == "EPSG:32633"
    assert detected is False


def test_estate_csv_reader_accepts_coordinate_rows_without_a_header():
    records = _normalize_csv_records(_read_estate_csv_records(
        "P-001;241123.5;1006432.1\nP-001;241124.5;1006432.1\nP-001;241124.5;1006433.1\n".encode(),
        "layout.csv",
    ))
    assert records == [
        {"plotnumber": "P-001", "x": "241123.5", "y": "1006432.1"},
        {"plotnumber": "P-001", "x": "241124.5", "y": "1006432.1"},
        {"plotnumber": "P-001", "x": "241124.5", "y": "1006433.1"},
    ]


def test_estate_reader_accepts_xlsx_using_existing_spreadsheet_parser():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Plot No.", "Easting (m)", "Northing (m)"])
    sheet.append(["P-001", 241123.5, 1006432.1])
    buffer = BytesIO()
    workbook.save(buffer)

    records = _normalize_csv_records(_read_estate_csv_records(buffer.getvalue(), "layout.xlsx"))
    assert records == [{"plotno": "P-001", "eastingm": "241123.5", "northingm": "1006432.1"}]


def test_dxf_linework_fallback_polygonizes_independent_closed_edges():
    def line(x1, y1, x2, y2):
        return SimpleNamespace(
            dxftype=lambda: "LINE",
            dxf=SimpleNamespace(
                start=SimpleNamespace(x=x1, y=y1),
                end=SimpleNamespace(x=x2, y=y2),
            ),
        )

    document = SimpleNamespace(
        modelspace=lambda: [
            line(0, 0, 10, 0),
            line(10, 0, 10, 10),
            line(10, 10, 0, 10),
            line(0, 10, 0, 0),
        ]
    )
    polygons = _polygonize_dxf_linework(document)
    assert len(polygons) == 1
    assert polygons[0].area == 100


def test_plot_number_falls_back_when_label_is_the_tool_default():
    feature = {"label": "Polygon 3"}
    assert _plot_number_from_georeference_feature(feature, 3, "P") == "P3"


def test_plot_number_falls_back_when_label_is_blank():
    assert _plot_number_from_georeference_feature({}, 1, "P") == "P1"


def test_plot_number_keeps_a_deliberate_label():
    feature = {"label": "B-024"}
    assert _plot_number_from_georeference_feature(feature, 1, "P") == "B-024"


def test_polygon_geometry_wraps_the_solved_wgs84_ring():
    feature = {
        "feature_type": "polygon",
        "wgs84_coordinates": [[7.1, 9.1], [7.2, 9.1], [7.2, 9.2], [7.1, 9.1]],
    }
    geometry = _polygon_geometry_from_georeference_feature(feature)
    assert geometry == {
        "type": "Polygon",
        "coordinates": [[[7.1, 9.1], [7.2, 9.1], [7.2, 9.2], [7.1, 9.1]]],
    }


def test_polygon_geometry_handles_a_missing_ring():
    assert _polygon_geometry_from_georeference_feature({}) == {"type": "Polygon", "coordinates": [[]]}
