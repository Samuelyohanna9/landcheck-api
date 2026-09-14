from app.services.estates.qc import validate_polygon


def test_phase1_qc_rejects_non_polygon_geometry():
    _, issues = validate_polygon({"type": "Point", "coordinates": [7.0, 9.0]})
    assert issues[0].severity == "error"


def test_phase1_qc_calculates_polygon_area_server_side():
    area, issues = validate_polygon({"type": "Polygon", "coordinates": [[[7.0, 9.0], [7.001, 9.0], [7.001, 9.001], [7.0, 9.001], [7.0, 9.0]]]})
    assert area > 1
    assert not [issue for issue in issues if issue.severity == "error"]
