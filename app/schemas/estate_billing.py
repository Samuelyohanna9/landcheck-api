from __future__ import annotations

from pydantic import BaseModel


class ChoosePlanRequest(BaseModel):
    plan_key: str
    billing_cycle: str


class ChangePlanRequest(BaseModel):
    plan_key: str
