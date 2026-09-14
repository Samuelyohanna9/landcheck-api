from pathlib import Path

from shapely.geometry import Polygon, shape

from app.schemas.estates import EstateLayoutCriteria
from app.services.estates.layout_generation import generate_estate_layout


def test_generated_layout_is_valid_non_overlapping_and_includes_planning_layers():
    boundary = Polygon([(7.4, 9.1), (7.404, 9.1), (7.404, 9.104), (7.4, 9.104)])
    result = generate_estate_layout(
        boundary,
        EstateLayoutCriteria(target_plot_area_sqm=600, road_width_m=10, open_space_percent=10),
    )

    plots = [shape(candidate["geometry"]) for candidate in result["plot_candidates"]]
    assert len(plots) >= 4
    assert all(plot.geom_type == "Polygon" and plot.is_valid and plot.area > 0 for plot in plots)
    assert all(first.intersection(second).area < 0.01 for index, first in enumerate(plots) for second in plots[index + 1 :])
    assert result["diagnostics"]["planning_crs"].startswith("EPSG:326")
    assert result["diagnostics"]["open_space_percent"] > 0
    assert {feature["feature_type"] for feature in result["feature_candidates"]} >= {"road", "open_space", "drainage"}


def test_generated_layout_honours_prefix_and_maximum_plot_count():
    boundary = Polygon([(7.4, 9.1), (7.41, 9.1), (7.41, 9.11), (7.4, 9.11)])
    result = generate_estate_layout(boundary, EstateLayoutCriteria(plot_prefix="BLOCK-A", max_plots=6, include_drainage=False))

    assert len(result["plot_candidates"]) <= 6
    assert result["plot_candidates"]
    assert result["plot_candidates"][0]["plot_number"].startswith("BLOCK-A-")
    assert all(feature["feature_type"] != "drainage" for feature in result["feature_candidates"])


def test_open_space_reserve_never_consumes_the_last_usable_plot():
    boundary = Polygon([(7.4, 9.1), (7.401, 9.1), (7.401, 9.101), (7.4, 9.101)])
    result = generate_estate_layout(boundary, EstateLayoutCriteria(open_space_percent=40, target_plot_area_sqm=100))

    assert result["plot_candidates"]


def test_layout_proposal_migration_is_reversible_and_registered():
    migration = Path(__file__).parents[1] / "alembic" / "versions" / "20260914_0015_estate_layout_proposals.py"
    content = migration.read_text(encoding="utf-8")
    assert 'revision = "20260914_0015"' in content
    assert 'down_revision = "20260914_0014"' in content
    assert 'op.create_table(' in content
    assert 'op.drop_table("estate_layout_proposals")' in content
