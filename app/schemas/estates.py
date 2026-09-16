from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field, model_validator
from decimal import Decimal

class EstateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    state: str | None = None
    locality: str | None = None
    location_text: str | None = None
    description: str | None = None
    crs: str = Field(default="EPSG:4326", min_length=3, max_length=80)
    datum: str | None = Field(default=None, max_length=80)
    unit_system: str = Field(default="m", pattern="^(m|ft)$")
    project_reference: str | None = Field(default=None, max_length=120)
    project_owner: str | None = Field(default=None, max_length=255)
    ownership_details: str | None = Field(default=None, max_length=4000)
    approximate_area_sqm: Decimal | None = Field(default=None, gt=0)
    boundary: dict[str, Any] | None = None


class EstateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    state: str | None = None
    locality: str | None = None
    location_text: str | None = None
    description: str | None = None
    crs: str | None = Field(default=None, min_length=3, max_length=80)
    datum: str | None = Field(default=None, max_length=80)
    unit_system: str | None = Field(default=None, pattern="^(m|ft)$")
    project_reference: str | None = Field(default=None, max_length=120)
    project_owner: str | None = Field(default=None, max_length=255)
    ownership_details: str | None = Field(default=None, max_length=4000)
    approximate_area_sqm: Decimal | None = Field(default=None, gt=0)
    boundary: dict[str, Any] | None = None


class PublicPaymentPlanItem(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    percentage: Decimal = Field(gt=0, le=100)


class PublicEstateSettingsUpdate(BaseModel):
    public_enabled: bool = False
    public_description: str | None = Field(default=None, max_length=4000)
    public_tagline: str | None = Field(default=None, max_length=255)
    public_contact_phone: str | None = Field(default=None, max_length=64)
    public_show_prices: bool = True
    payment_plan: list[PublicPaymentPlanItem] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def validate_payment_plan(self):
        if self.payment_plan:
            if any(not item.label.strip() for item in self.payment_plan):
                raise ValueError("Payment plan stages need a name")
            if sum((item.percentage for item in self.payment_plan), Decimal("0")) != Decimal("100"):
                raise ValueError("Payment plan percentages must add up to 100")
        return self

class PlotCreate(BaseModel):
    plot_number: str
    geometry: dict[str, Any]
    block_id: int | None = None
    land_use: str | None = None
    geometry_status: str = "draft"
    public_address: str | None = Field(default=None, max_length=255)
    asking_price: Decimal | None = Field(default=None, gt=0)


class PlotPriceUpdate(BaseModel):
    asking_price: Decimal | None = Field(default=None, gt=0)


class PlotAddressUpdate(BaseModel):
    public_address: str | None = Field(default=None, max_length=255)


class PlotListingDefaultsUpdate(BaseModel):
    """Apply shared public-listing values to the plots in one Estate."""

    apply_address: bool = False
    public_address: str | None = Field(default=None, max_length=255)
    apply_price: bool = False
    asking_price: Decimal | None = Field(default=None, gt=0)


class EstateSubdivisionCreate(BaseModel):
    split_count: int = Field(default=2, ge=2, le=100)


class PlotGeometryUpdate(BaseModel):
    geometry: dict[str, Any]


class EstateLayoutCriteria(BaseModel):
    """Editable planning assumptions used to generate a concept layout.

    These are not statutory defaults. The relevant planning authority and a qualified planner
    remain responsible for the final layout, road reserve and subdivision requirements.
    """

    target_plot_area_sqm: float = Field(default=500, ge=100, le=100000)
    road_width_m: float = Field(default=10, ge=4, le=50)
    edge_reserve_m: float = Field(default=5, ge=0, le=50)
    drainage_reserve_m: float = Field(default=3, ge=0, le=30)
    open_space_percent: float = Field(default=10, ge=0, le=40)
    frontage_m: float | None = Field(default=None, ge=5, le=200)
    orientation_deg: float = Field(default=0, ge=-180, le=180)
    plot_prefix: str = Field(default="P", min_length=1, max_length=16, pattern="^[A-Za-z0-9_-]+$")
    max_plots: int = Field(default=500, ge=2, le=5000)
    include_open_space: bool = True
    include_drainage: bool = True
    include_roads: bool = True


class EstateLayoutDecision(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    notes: str | None = Field(default=None, max_length=4000)
    # Approving a fresh draft over an Estate that already has an approved layout would otherwise
    # fail outright on the first clashing plot number - this lets the caller explicitly replace
    # the existing plots/features with the new draft's, after the frontend has warned the user and
    # gotten their confirmation. Ignored when status is "rejected".
    replace_existing: bool = False


class EstateLayoutProposalEdit(BaseModel):
    """Lets a reviewer nudge vertices, delete a candidate plot, or edit a road/open-space shape
    on a draft layout before approving it - the same shape the generator itself produces."""
    plot_candidates: list[dict[str, Any]] | None = None
    feature_candidates: list[dict[str, Any]] | None = None


class EstateLayoutFeatureAdd(BaseModel):
    """Adds one hand-drawn road or open-space shape to a draft layout. The server carves the
    resulting footprint out of every plot candidate it overlaps, so the draft reflects the new
    infrastructure instead of showing plots that overlap it. plot_candidates lets the caller pass
    along any vertex edits made in the same session that haven't been saved yet, so they aren't
    lost when this call replaces the candidate list with the carved result."""
    feature_type: str = Field(pattern="^(road|open_space)$")
    geometry: dict[str, Any]
    width_m: float | None = Field(default=None, ge=1, le=60)
    name: str | None = Field(default=None, max_length=160)
    plot_candidates: list[dict[str, Any]] | None = None


class EstateLayoutFeatureRemove(BaseModel):
    """Removes a road or open-space shape from a draft layout and gives the vacated space back to
    the plots that fronted it - either by restoring the exact pre-carve shape of every plot this
    feature carved when it was added (if it has that history), or, for a plain generated road with
    no such history, by splitting the vacated corridor along its centreline and merging each half
    into whichever plots actually front it. plot_candidates/feature_candidates let the caller pass
    along any edits made in the same session that haven't been saved yet."""
    feature_index: int = Field(ge=0)
    plot_candidates: list[dict[str, Any]] | None = None
    feature_candidates: list[dict[str, Any]] | None = None


class DevelopmentStatusUpdate(BaseModel):
    status: str = Field(pattern="^(not_started|site_cleared|foundation|under_construction|developed)$")


class SurveyEligibilityUpdate(BaseModel):
    is_enabled: bool = True
    percentage: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    description: str | None = Field(default=None, max_length=4000)

class FieldInspectionCreate(BaseModel):
    inspection_type: str = Field(default="site_visit", min_length=1, max_length=64)
    outcome: str = Field(default="observed", pattern="^(observed|attention_required|passed|failed)$")
    notes: str | None = Field(default=None, max_length=4000)

class SpatialFeatureCreate(BaseModel):
    feature_type: str = Field(pattern="^(road|drainage|open_space|infrastructure)$")
    name: str | None = Field(default=None, max_length=160)
    geometry: dict[str, Any]

class SpatialFeatureUpdate(BaseModel):
    feature_type: str | None = Field(default=None, pattern="^(road|drainage|open_space|infrastructure)$")
    name: str | None = Field(default=None, max_length=160)
    geometry: dict[str, Any] | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")

class BlockCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, max_length=160)
    geometry: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=4000)

class BlockUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    name: str | None = Field(default=None, max_length=160)
    geometry: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=4000)

class MemberCreate(BaseModel):
    subject_type: str = Field(min_length=1, max_length=64)
    subject_id: str = Field(min_length=1, max_length=128)
    role_key: str = Field(pattern="^(owner|manager|accounts|surveyor|field_officer|sales|viewer)$")

class MemberUpdate(BaseModel):
    role_key: str | None = Field(default=None, pattern="^(owner|manager|accounts|surveyor|field_officer|sales|viewer)$")
    is_active: bool | None = None

class ImportReviewCreate(BaseModel):
    source_type: str = Field(pattern="^(coordinates|csv|gis|cad|raster|pdf)$")
    survey_georeference_session_id: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)

class ImportReviewDecision(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    notes: str | None = Field(default=None, max_length=4000)
    candidate_data: list[dict[str, Any]] | None = None
    # When true, the single approved candidate becomes the Estate's boundary (for subdividing or
    # designing a layout next) instead of being created as an individual operational plot.
    as_boundary: bool = False

class ImportReviewFromGeoreferenceSession(BaseModel):
    survey_georeference_session_id: str = Field(min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)

class GeoreferenceSessionLink(BaseModel):
    survey_georeference_session_id: str = Field(min_length=1, max_length=128)

class ImportFromGeoreference(BaseModel):
    plot_prefix: str = Field(default="P", max_length=20)

class CustomerCreate(BaseModel):
    full_name: str
    reference_no: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    company_name: str | None = None
    notes: str | None = None

class AllocationAction(BaseModel):
    customer_id: int
    expires_at: datetime | None = None
    agreed_price: Decimal | None = Field(default=None, gt=0)
    payment_plan: str | None = Field(default=None, max_length=4000)
    notes: str | None = None
    # Lets a reviewer capture money already received at the same time they reserve/allocate the
    # plot, instead of a separate follow-up step - recorded as a confirmed payment immediately
    # since it represents an already-received amount the org is attesting to, not a pending claim.
    initial_payment_amount: Decimal | None = Field(default=None, gt=0)
    initial_payment_method: str | None = Field(default=None, max_length=64)
    # Credits an org member with this sale for commission tracking - optional, and only ever
    # priced (tier/rate/amount) once the allocation actually reaches "allocated" (fully paid).
    sales_agent_subject_type: str | None = Field(default=None, max_length=64)
    sales_agent_subject_id: str | None = Field(default=None, max_length=128)

class CommissionTierItem(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    min_cumulative_sales: Decimal = Field(ge=0)
    rate_percent: Decimal = Field(ge=0, le=100)

class CommissionTiersUpdate(BaseModel):
    tiers: list[CommissionTierItem] = Field(min_length=1, max_length=20)

class CommissionPayoutCreate(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0)  # defaults to the full outstanding balance when omitted
    payment_date: datetime
    payment_method: str
    reference_no: str | None = None
    notes: str | None = None

class PaymentCreate(BaseModel):
    amount: Decimal = Field(gt=0)
    payment_date: datetime
    payment_method: str
    reference_no: str | None = None
    notes: str | None = None

class VoidAction(BaseModel):
    reason: str = Field(min_length=1)


class PublicReservationCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    phone: str = Field(min_length=5, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    message: str | None = Field(default=None, max_length=2000)


class PublicReservationUpdate(BaseModel):
    status: str = Field(pattern="^(new|contacted|converted|declined)$")
    staff_notes: str | None = Field(default=None, max_length=4000)


class PublicReservationConvert(BaseModel):
    agreed_price: Decimal | None = Field(default=None, gt=0)
    payment_plan: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
