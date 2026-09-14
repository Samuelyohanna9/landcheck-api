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


def test_estate_project_metadata_migration_is_reversible():
    migration = (Path(__file__).parents[1] / "alembic" / "versions" / "20260914_0012_estate_project_metadata_and_development_lifecycle.py").read_text(encoding="utf-8")
    assert "def upgrade" in migration
    assert "def downgrade" in migration
    assert "project_reference" in migration
    assert "development_status = 'under_construction'" in migration
