from pathlib import Path

from app.models.estate_foundation import Estate, EstatePlot, EstatePublicReservationRequest
from app.schemas.estates import PublicEstateSettingsUpdate, PublicReservationCreate, PublicReservationUpdate


ROOT = Path(__file__).parents[1]


def test_public_showcase_model_and_migration_are_reversible():
    model = (ROOT / "app" / "models" / "estate_foundation.py").read_text(encoding="utf-8")
    migration = (ROOT / "alembic" / "versions" / "20260916_0021_estate_public_showcase.py").read_text(encoding="utf-8")
    assert "class EstatePublicReservationRequest" in model
    assert "public_slug" in Estate.__table__.c
    assert "asking_price" in EstatePlot.__table__.c
    assert EstatePublicReservationRequest.__table__.c.status is not None
    assert "def upgrade" in migration and "def downgrade" in migration
    assert "estate_public_reservation_requests" in migration
    assert "uq_estate_estates_public_slug" in migration


def test_public_showcase_routes_are_safe_and_separate_from_allocations():
    source = (ROOT / "app" / "routers" / "estates.py").read_text(encoding="utf-8")
    assert '@router.get("/public/{slug}")' in source
    assert '@router.post("/public/{slug}/plots/{plot_id}/reservation", status_code=201)' in source
    assert "ESTATE_PUBLIC_MAP" in source
    assert "public_reservation.created" in source
    assert "without creating a customer or changing plot ownership" in source
    assert "require_estate_access(db, request, estate.organization_id, permission=\"allocation.read\")" in source
    assert '@router.patch("/reservation-requests/{request_id}")' in source


def test_public_inputs_have_bounded_validation():
    assert PublicEstateSettingsUpdate.model_fields["public_slug"].metadata
    assert PublicReservationCreate.model_fields["full_name"].metadata
    assert PublicReservationCreate.model_fields["phone"].metadata
    assert PublicReservationUpdate.model_fields["status"].metadata
