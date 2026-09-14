from __future__ import annotations

from pydantic import BaseModel, Field


class EstateRegister(BaseModel):
    organization_name: str = Field(min_length=2, max_length=255)
    organization_slug: str | None = Field(default=None, min_length=2, max_length=120)
    full_name: str = Field(min_length=2, max_length=255)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=200)


class EstateLogin(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=1, max_length=200)
