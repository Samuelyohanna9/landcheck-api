from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MeetingPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    label: str | None = Field(default=None, max_length=160)
    note: str | None = Field(default=None, max_length=500)


class PublicEventCreate(BaseModel):
    event_type: Literal["whatsapp_click", "share_click", "directions_click", "guide_open"]
    plot_id: int | None = None
    source: str | None = Field(default=None, max_length=120)


class InspectionBookingCreate(BaseModel):
    slot_id: int
    full_name: str = Field(min_length=2, max_length=255)
    phone: str = Field(min_length=5, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    party_size: int = Field(default=1, ge=1, le=10)
    plot_id: int | None = None
    note: str | None = Field(default=None, max_length=1000)
    source: str | None = Field(default=None, max_length=120)


class InspectionSlotCreate(BaseModel):
    starts_at: datetime
    duration_minutes: int = Field(default=120, ge=30, le=480)
    capacity: int = Field(default=20, ge=1, le=500)
    note: str | None = Field(default=None, max_length=1000)
    repeat_weekly: int = Field(default=0, ge=0, le=12)


class InspectionSlotUpdate(BaseModel):
    status: Literal["open", "closed", "cancelled"] | None = None
    capacity: int | None = Field(default=None, ge=1, le=500)
    note: str | None = Field(default=None, max_length=1000)


class InspectionBookingUpdate(BaseModel):
    status: Literal["booked", "cancelled", "attended", "no_show"] | None = None
    staff_notes: str | None = Field(default=None, max_length=2000)


class ProgressUpdatePatch(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=255)
    body: str | None = Field(default=None, max_length=4000)
    is_published: bool | None = None


class AgentLeadUpdate(BaseModel):
    status: Literal["contacted", "declined"]
    note: str | None = Field(default=None, max_length=2000)


class AgentBookingUpdate(BaseModel):
    status: Literal["attended", "no_show"]
