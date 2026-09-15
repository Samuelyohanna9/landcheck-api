from fastapi import Response

from app.routers.analytics import get_public_survey_proof, router


class _ScalarResult:
    def __init__(self, value: int):
        self.value = value

    def scalar(self) -> int:
        return self.value


class _CountDatabase:
    def __init__(self, values: list[int]):
        self.values = iter(values)

    def execute(self, statement):
        assert str(statement).strip().lower() == "select count(*) from plots"
        return _ScalarResult(next(self.values))


def test_public_survey_proof_is_registered_and_reflects_latest_plot_total():
    route = next(route for route in router.routes if route.path == "/analytics/public-proof")
    assert "GET" in route.methods
    db = _CountDatabase([42, 43])

    first_response = Response()
    first = get_public_survey_proof(first_response, db)
    second = get_public_survey_proof(Response(), db)

    assert first_response.status_code == 200
    assert first_response.headers["cache-control"] == "no-store"
    assert first == {"total_plots_generated": 42}
    assert second == {"total_plots_generated": 43}
