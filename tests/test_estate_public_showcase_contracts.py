from pathlib import Path

from app.models.estate_foundation import Estate, EstatePlot, EstatePublicReservationRequest
from app.schemas.estates import PlotListingDefaultsUpdate, PublicEstateSettingsUpdate, PublicReservationCreate, PublicReservationUpdate


ROOT = Path(__file__).parents[1]


def test_public_showcase_model_and_migration_are_reversible():
    model = (ROOT / "app" / "models" / "estate_foundation.py").read_text(encoding="utf-8")
    migration = (ROOT / "alembic" / "versions" / "20260916_0021_estate_public_showcase.py").read_text(encoding="utf-8")
    branding_migration = (ROOT / "alembic" / "versions" / "20260916_0022_public_estate_branding.py").read_text(encoding="utf-8")
    assert "class EstatePublicReservationRequest" in model
    assert "public_slug" in Estate.__table__.c
    assert "public_logo_object_key" in Estate.__table__.c
    assert "public_tagline" in Estate.__table__.c
    assert "asking_price" in EstatePlot.__table__.c
    assert "public_address" in EstatePlot.__table__.c
    assert EstatePublicReservationRequest.__table__.c.status is not None
    assert "def upgrade" in migration and "def downgrade" in migration
    assert "estate_public_reservation_requests" in migration
    assert "uq_estate_estates_public_slug" in migration
    assert "public_tagline" in branding_migration and "public_logo_object_key" in branding_migration
    assert "public_address" in branding_migration and "def downgrade" in branding_migration


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
    assert PublicEstateSettingsUpdate.model_fields["public_tagline"].metadata
    assert PublicReservationCreate.model_fields["full_name"].metadata
    assert PublicReservationCreate.model_fields["phone"].metadata
    assert PublicReservationUpdate.model_fields["status"].metadata


def test_plot_listing_defaults_have_explicit_bulk_controls():
    assert PlotListingDefaultsUpdate.model_fields["apply_address"].default is False
    assert PlotListingDefaultsUpdate.model_fields["apply_price"].default is False
    source = (ROOT / "app" / "routers" / "estates.py").read_text(encoding="utf-8")
    assert '@router.patch("/{estate_id}/plot-listing-defaults")' in source
    assert 'permission="plot.manage"' in source
    assert "estate.plot_listing_defaults_updated" in source


def test_public_address_is_generated_from_estate_name():
    source = (ROOT / "app" / "routers" / "estates.py").read_text(encoding="utf-8")
    assert "_unique_public_slug" in source
    assert "estate.public_slug or _unique_public_slug(db, estate)" in source


def test_container_startup_applies_migrations_before_serving_api():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "alembic upgrade head" in compose
    assert "exec uvicorn app.main:app" in compose
    assert compose.index("alembic upgrade head") < compose.index("exec uvicorn app.main:app")


def test_schema_is_owned_by_alembic_not_runtime_create_all():
    db_init = (ROOT / "app" / "db_init.py").read_text(encoding="utf-8")
    migration = (ROOT / "alembic" / "versions" / "20260916_0021_estate_public_showcase.py").read_text(encoding="utf-8")
    assert 'table.name.startswith("estate_")' in db_init
    assert "has_table(\"estate_public_reservation_requests\")" in migration
