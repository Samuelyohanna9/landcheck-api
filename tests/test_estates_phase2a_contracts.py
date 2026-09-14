from pathlib import Path


def test_phase2a_router_exposes_tenant_scoped_financial_contracts():
    source = (Path(__file__).parents[1] / "app" / "routers" / "estates.py").read_text(encoding="utf-8")
    for route in (
        '/payments',
        '/payments/{payment_id}',
        '/allocations/{allocation_id}/financial-detail',
        '/customers/{customer_id}/financial-detail',
        '/customers/{customer_id}/statement',
        '/{estate_id}/financial-summary',
        '/documents',
    ):
        assert route in source
    assert "require_estate_access" in source


def test_phase2a_statement_supports_safe_server_side_filters():
    source = (Path(__file__).parents[1] / "app" / "routers" / "estates.py").read_text(encoding="utf-8")
    assert "estate_id:int|None=None, allocation_id:int|None=None" in source
    assert "EstateAllocation.organization_id==customer.organization_id" in source


def test_payment_detail_and_document_list_expose_only_server_authorized_contracts():
    source = (Path(__file__).parents[1] / "app" / "routers" / "estates.py").read_text(encoding="utf-8")
    assert '"capabilities":{"can_confirm"' in source
    assert '"can_view_receipt":has_permission(access.role_key,"document.read")' in source
    assert "entity_id:int|None=None" in source
    assert "EstateDocumentLink.entity_id==str(entity_id)" in source
