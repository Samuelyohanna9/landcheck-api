from app.routers.estates import (
    _CSV_COORDINATE_KEYS,
    _CSV_PLOT_NUMBER_KEYS,
    _CSV_X_KEYS,
    _CSV_Y_KEYS,
    _first_csv_value,
    _csv_number,
    _repeated_csv_plot_key,
    _polygonize_dxf_linework,
    _read_estate_csv_records,
    _normalize_csv_records,
    _plot_number_from_georeference_feature,
    _polygon_geometry_from_georeference_feature,
)
from types import SimpleNamespace


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
