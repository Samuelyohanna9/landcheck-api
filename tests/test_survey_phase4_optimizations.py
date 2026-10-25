from types import SimpleNamespace

from app.routers import plots


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def execute(self, _statement, _params=None):
        self.calls += 1
        return _Result(self.rows)


def test_plot_report_builder_preserves_area_and_feature_grouping():
    db = _FakeSession([
        SimpleNamespace(feature_type="road", location="inside", count=2),
        SimpleNamespace(feature_type="building", location="buffer", count=1),
    ])

    report = plots._build_plot_report_from_area(18, 123.456, db)

    assert report == {
        "plot_id": 18,
        "area_m2": 123.46,
        "features": {
            "inside": {"road": 2},
            "buffer": {"building": 1},
        },
    }
    assert db.calls == 1
