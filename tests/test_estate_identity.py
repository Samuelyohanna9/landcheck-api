from types import SimpleNamespace

from app.services.estates.identity import hash_password, request_token, slugify, verify_password


def test_estate_password_hash_is_salted_and_verifiable():
    first = hash_password("strong-password-123")
    second = hash_password("strong-password-123")

    assert first != second
    assert verify_password("strong-password-123", first)
    assert not verify_password("wrong-password", first)
    assert not verify_password("strong-password-123", "not-a-password-hash")


def test_estate_identity_normalizes_company_slugs():
    assert slugify("Northfield Property & Estates") == "northfield-property-estates"
    assert slugify("   ") == "estate-company"


def test_estate_request_token_only_accepts_bearer_headers():
    bearer = SimpleNamespace(headers={"authorization": "Bearer estate-token"})
    basic = SimpleNamespace(headers={"authorization": "Basic estate-token"})

    assert request_token(bearer) == "estate-token"
    assert request_token(basic) is None
