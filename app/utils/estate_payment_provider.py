from __future__ import annotations

"""Dispatches between the two Estates payment gateways by a stored/declared provider string.
See estate_paystack.py's module docstring for why both modules share an identical interface -
this is the one place that picks which of them a given call site actually talks to."""

from types import ModuleType

from app.utils import estate_flutterwave as flw
from app.utils import estate_paystack as pst

SUPPORTED_PROVIDERS = ("flutterwave", "paystack")
DEFAULT_PROVIDER = "flutterwave"


def module_for(provider: str | None) -> ModuleType:
    return pst if str(provider or "").strip().lower() == "paystack" else flw


def is_configured(provider: str) -> bool:
    return pst.configured() if provider == "paystack" else flw.configured()


def available_providers() -> dict[str, bool]:
    return {name: is_configured(name) for name in SUPPORTED_PROVIDERS}
