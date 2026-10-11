from __future__ import annotations

from pydantic import BaseModel


class ChoosePlanRequest(BaseModel):
    plan_key: str
    billing_cycle: str
    provider: str = "flutterwave"


class ChangePlanRequest(BaseModel):
    plan_key: str
