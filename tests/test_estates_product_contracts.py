from pathlib import Path

from app.services.estates.permissions import ROLE_PERMISSIONS, has_permission


def test_current_estate_role_matrix_is_explicit_and_server_side():
    expected = {
        "owner": {"payment.manage", "document.read", "document.manage", "estate.manage"},
        "manager": {"payment.manage", "document.read", "document.manage", "estate.manage"},
        "accounts": {"payment.manage", "document.read", "document.manage"},
        "sales": {"payment.manage", "document.read", "document.manage"},
        "surveyor": {"document.read", "document.manage"},
        "field_officer": {"document.read", "document.manage"},
        "viewer": {"document.read"},
    }
    assert set(ROLE_PERMISSIONS) == set(expected)
    for role, permissions in expected.items():
        for permission in ("payment.manage", "document.read", "document.manage", "estate.manage"):
            assert has_permission(role, permission) is (permission in permissions or role == "owner")


def test_financial_mutations_are_not_granted_to_read_only_or_field_roles():
    for role in ("surveyor", "field_officer", "viewer"):
        assert not has_permission(role, "payment.manage")
    for role in ("owner", "manager", "accounts", "sales"):
        assert has_permission(role, "payment.manage")


def test_estate_workflow_uses_web_development_lifecycle_and_map_publication():
    source = (Path(__file__).parents[1] / "app" / "routers" / "estates.py").read_text(encoding="utf-8")
    schema = (Path(__file__).parents[1] / "app" / "schemas" / "estates.py").read_text(encoding="utf-8")
    allocation_service = (Path(__file__).parents[1] / "app" / "services" / "estates" / "allocations.py").read_text(encoding="utf-8")
    assert '@router.post("/{estate_id}/approve-map")' in source
    assert 'permission="plot.manage"' in source
    assert "site_cleared|foundation|under_construction|developed" in schema
    assert "payment_plan" in schema
    assert "payment_plan=payment_plan" in allocation_service


def test_estate_plot_subdivision_creates_allocatable_estate_children():
    source = (Path(__file__).parents[1] / "app" / "routers" / "estates.py").read_text(encoding="utf-8")
    schema = (Path(__file__).parents[1] / "app" / "schemas" / "estates.py").read_text(encoding="utf-8")
    assert '@router.post("/{estate_id}/plots/{plot_id}/subdivide")' in source
    assert "permission=\"plot.manage\"" in source
    assert 'commercial_status="available"' in source
    assert 'source_type="subdivision"' in source
    assert "class EstateSubdivisionCreate" in schema


def test_estate_layout_proposal_is_reviewable_before_register_approval():
    source = (Path(__file__).parents[1] / "app" / "routers" / "estates.py").read_text(encoding="utf-8")
    model = (Path(__file__).parents[1] / "app" / "models" / "estate_foundation.py").read_text(encoding="utf-8")
    service = (Path(__file__).parents[1] / "app" / "services" / "estates" / "layout_generation.py").read_text(encoding="utf-8")
    assert '@router.post("/{estate_id}/layout-proposals")' in source
    assert '@router.post("/layout-proposals/{proposal_id}/decision")' in source
    assert "class EstateLayoutProposal" in model
    assert "generate_estate_layout" in source
    assert "qualified planner/surveyor" in service


def test_estate_project_metadata_migration_is_reversible():
    migration = (Path(__file__).parents[1] / "alembic" / "versions" / "20260914_0012_estate_project_metadata_and_development_lifecycle.py").read_text(encoding="utf-8")
    assert "def upgrade" in migration
    assert "def downgrade" in migration
    assert "project_reference" in migration
    assert "development_status = 'under_construction'" in migration
