from pathlib import Path

from app.routers.estates import _spatial_feature_write_permission
from app.schemas.estates import SpatialFeatureCreate, SpatialFeatureUpdate


def test_boundary_layers_use_plot_permissions_and_accept_polygon_geometry():
    payload = SpatialFeatureCreate(
        feature_type="boundary",
        name="Additional boundary",
        geometry={"type": "Polygon", "coordinates": [[[3.0, 6.0], [3.001, 6.0], [3.001, 6.001], [3.0, 6.0]]]},
    )
    assert payload.feature_type == "boundary"
    assert _spatial_feature_write_permission(payload.feature_type) == "plot.manage"
    assert _spatial_feature_write_permission("road") == "infrastructure.manage"


def test_boundary_update_type_is_supported_and_migration_protects_downgrade():
    payload = SpatialFeatureUpdate(feature_type="boundary")
    assert payload.feature_type == "boundary"
    migration = (Path(__file__).parents[1] / "alembic" / "versions" / "20261026_0071_estate_boundary_layers.py").read_text(encoding="utf-8")
    assert "feature_type = 'boundary'" in migration
    assert "Cannot downgrade while Estate boundary layers exist" in migration
