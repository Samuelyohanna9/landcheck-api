from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field
from decimal import Decimal

class EstateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    state: str | None = None
    locality: str | None = None
    location_text: str | None = None
    description: str | None = None
    crs: str = Field(default="EPSG:4326", min_length=3, max_length=80)
    datum: str | None = Field(default=None, max_length=80)
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
    project_reference: str | None = Field(default=None, max_length=120)
    project_owner: str | None = Field(default=None, max_length=255)
    ownership_details: str | None = Field(default=None, max_length=4000)
    approximate_area_sqm: Decimal | None = Field(default=None, gt=0)
    boundary: dict[str, Any] | None = None

class PlotCreate(BaseModel):
    plot_number: str
    geometry: dict[str, Any]
    block_id: int | None = None
    land_use: str | None = None
    geometry_status: str = "draft"

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

class PaymentCreate(BaseModel):
    amount: Decimal = Field(gt=0)
    payment_date: datetime
    payment_method: str
    reference_no: str | None = None
    notes: str | None = None

class VoidAction(BaseModel):
    reason: str = Field(min_length=1)
