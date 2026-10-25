from types import SimpleNamespace

from app.routers.green import _green_idempotency_fingerprint, _green_idempotency_scope


def test_green_idempotency_fingerprint_is_order_insensitive_for_payload_objects():
    first = {"tree": {"species": "Gmelina", "lat": 6.4}, "photos": ["a"]}
    second = {"photos": ["a"], "tree": {"lat": 6.4, "species": "Gmelina"}}

    assert _green_idempotency_fingerprint(first) == _green_idempotency_fingerprint(second)
    assert _green_idempotency_fingerprint(first) != _green_idempotency_fingerprint({**first, "photos": ["b"]})


def test_green_idempotency_scope_changes_with_authorization_context():
    session = SimpleNamespace(
        auth_mode="green",
        user_id=10,
        sponsor_account_id=None,
        session_uid="session-a",
        organization_id=4,
        role_key="field_agent",
    )

    original = _green_idempotency_scope(session, "create-tree", 8)
    session.organization_id = 5
    other_organization = _green_idempotency_scope(session, "create-tree", 8)
    session.organization_id = 4
    session.role_key = "manager"
    other_role = _green_idempotency_scope(session, "create-tree", 8)

    assert original != other_organization
    assert original != other_role
