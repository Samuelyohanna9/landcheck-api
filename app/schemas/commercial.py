from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+()\- ]{7,64}$")


class EstateDemoRequestCreate(BaseModel):
    company_name: str = Field(min_length=2, max_length=255)
    contact_name: str = Field(min_length=2, max_length=255)
    email: str = Field(min_length=5, max_length=255)
    phone: str = Field(min_length=7, max_length=64)
    message: str | None = Field(default=None, max_length=2000)
    source_path: str | None = Field(default=None, max_length=255)
    website: str | None = Field(default=None, max_length=255)

    @field_validator("company_name", "contact_name", "email", "phone", mode="before")
    @classmethod
    def clean_required_text(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("message", "source_path", "website", mode="before")
    @classmethod
    def clean_optional_text(cls, value: object) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.lower()
        if not _EMAIL_RE.fullmatch(value):
            raise ValueError("Enter a valid business email address")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if not _PHONE_RE.fullmatch(value):
            raise ValueError("Enter a valid phone or WhatsApp number")
        return value
