from app.services.survey.dgps import alpha_station, render_dgps_staking_csv


def test_dgps_station_labels_continue_after_z():
    assert [alpha_station(index) for index in (0, 25, 26, 27, 701)] == ["A", "Z", "AA", "AB", "ZZ"]


def test_dgps_csv_is_receiver_safe_and_can_be_strict_raw():
    rows = [{
        "point_type": "Plot vertex",
        "station": "A",
        "feature": "Plot B-024",
        "coordinate_system": "utm_32n",
        "easting": 206836.766,
        "northing": 1027652.612,
        "longitude": 7.123456789,
        "latitude": 9.987654321,
    }]

    excel_csv = render_dgps_staking_csv(rows)
    raw_csv = render_dgps_staking_csv(rows, raw=True)

    assert excel_csv.startswith("\ufeffsep=,\r\n")
    assert not raw_csv.startswith("\ufeffsep=,")
    assert "206836.7660" in raw_csv
    assert "1027652.6120" in raw_csv
    assert "7.12345679" in raw_csv
    assert "9.98765432" in raw_csv
