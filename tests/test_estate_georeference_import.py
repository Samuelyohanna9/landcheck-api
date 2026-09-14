from app.routers.estates import (
    _normalize_csv_records,
    _plot_number_from_georeference_feature,
    _polygon_geometry_from_georeference_feature,
)


def test_normalize_csv_records_matches_capitalized_excel_headers():
    raw = [{"Plot Number": "B-024", "Longitude": "7.1", "Latitude": "9.1"}]
    normalized = _normalize_csv_records(raw)
    assert normalized == [{"plotnumber": "B-024", "longitude": "7.1", "latitude": "9.1"}]


def test_normalize_csv_records_strips_units_and_spacing():
    raw = [{" Easting (m) ": "241123.5", "Northing (m)": "1006432.1"}]
    normalized = _normalize_csv_records(raw)
    assert normalized == [{"eastingm": "241123.5", "northingm": "1006432.1"}]


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
