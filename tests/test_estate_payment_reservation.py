from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.services.estates import payments
from app.services.estates.authorization import EstatePrincipal


class _FakeSession:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    def flush(self):
        row = self.rows[-1]
        row.id = 41
        row.payment_uid = row.payment_uid or str(uuid4())


def _allocation(*, status: str = "reserved"):
    return SimpleNamespace(
        id=12,
        organization_id=3,
        customer_id=8,
        plot_id=19,
        status=status,
        agreed_price=Decimal("100000"),
        reservation_expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        reservation_reminder_sent_at=datetime.now(timezone.utc),
    )


def test_recorded_payment_clears_reserved_allocation_expiry(monkeypatch):
    db = _FakeSession()
    events = []
    monkeypatch.setattr(
        payments,
        "financial_summary",
        lambda *_args, **_kwargs: SimpleNamespace(
            agreed_price=Decimal("100000"),
            confirmed_paid=Decimal("0"),
        ),
    )
    monkeypatch.setattr(payments, "append_estate_audit_event", lambda *args, **kwargs: events.append(kwargs))

    allocation = _allocation()
    payment = payments.record_payment(
        db,
        allocation=allocation,
        amount=Decimal("50000"),
        payment_date=datetime.now(timezone.utc),
        method="bank_transfer",
        reference="TRX-1",
        notes=None,
        actor=EstatePrincipal("survey_user", "7", "Estate manager"),
    )

    assert payment.status == "pending_confirmation"
    assert allocation.reservation_expires_at is None
    assert allocation.reservation_reminder_sent_at is None
    assert any(event["action"] == "allocation.reservation_protected_by_payment" for event in events)


def test_recorded_payment_does_not_change_allocated_sale_expiry(monkeypatch):
    db = _FakeSession()
    monkeypatch.setattr(
        payments,
        "financial_summary",
        lambda *_args, **_kwargs: SimpleNamespace(
            agreed_price=Decimal("100000"),
            confirmed_paid=Decimal("0"),
        ),
    )
    monkeypatch.setattr(payments, "append_estate_audit_event", lambda *args, **kwargs: None)

    allocation = _allocation(status="allocated")
    original_expiry = allocation.reservation_expires_at
    payments.record_payment(
        db,
        allocation=allocation,
        amount=Decimal("50000"),
        payment_date=datetime.now(timezone.utc),
        method="bank_transfer",
        reference=None,
        notes=None,
        actor=EstatePrincipal("survey_user", "7", "Estate manager"),
    )

    assert allocation.reservation_expires_at == original_expiry
