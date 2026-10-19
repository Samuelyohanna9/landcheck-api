from __future__ import annotations

"""Manual "send reminder" action for the Due Payments board - on top of the automatic reminder
job (app/services/estates/operations.py::send_due_payment_reminders). A manual send marks
payment_reminder_sent_for_due_at the same way the automatic job does, so the next scheduled run
doesn't duplicate it a few hours later."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.estate_foundation import Estate, EstateAllocation, EstateCustomer, EstateOrganization, EstateOrganizationPayoutAccount, EstatePlot
from app.services.estates import estate_email, estate_sms, marketing_common, social_whatsapp, whatsapp_inbox
from app.services.estates.audit import append_estate_audit_event
from app.services.estates.authorization import EstatePrincipal
from app.services.estates.operations import issue_customer_portal_link, record_notification_log
from app.services.estates.payments import financial_summary
from app.services.estates.payout_accounts import get_payout_account
from app.services.estates.subscriptions import get_subscription, has_sms_access


def compose_reminder_text(
    *,
    customer_name: str,
    org_name: str,
    estate_name: str,
    plot_number: str,
    outstanding: Decimal,
    due_at: datetime | None,
    method: str,
    portal_url: str | None = None,
    payout_account: EstateOrganizationPayoutAccount | None = None,
) -> str:
    first_name = (customer_name or "there").split(" ")[0]
    due_str = due_at.strftime("%d %b %Y") if due_at else "soon"
    amount_str = estate_email.format_naira(outstanding)
    base = f"{org_name}: Hi {first_name}, your payment of {amount_str} for Plot {plot_number} at {estate_name} is due {due_str}."
    parts = [base]
    if method in ("online_link", "both") and portal_url:
        parts.append(f"Pay online: {portal_url}")
    if method in ("bank_details", "both") and payout_account is not None:
        parts.append(f"Or pay by transfer to {payout_account.bank_name or ''} - {payout_account.account_name or ''} - {payout_account.account_number or ''}.")
    return " ".join(parts)


def send_reminder(db: Session, *, allocation: EstateAllocation, channel: str, method: str, actor: EstatePrincipal) -> dict:
    customer = db.get(EstateCustomer, allocation.customer_id)
    estate = db.get(Estate, allocation.estate_id)
    plot = db.get(EstatePlot, allocation.plot_id)
    organization = db.get(EstateOrganization, allocation.organization_id)
    if not customer or not estate or not plot or not organization:
        raise ValueError("Could not find the plot/customer record for this reminder")
    summary = financial_summary(db, allocation)
    if summary.outstanding <= 0:
        raise ValueError("This allocation has no outstanding balance")

    payout_account = get_payout_account(db, allocation.organization_id) if method in ("bank_details", "both") else None
    if method == "bank_details" and not payout_account:
        raise ValueError("This developer has no payout account on file to share bank details from")

    portal_url = None
    if method in ("online_link", "both"):
        _, portal_url = issue_customer_portal_link(db, customer=customer, actor=actor)

    if method == "both" and not portal_url and not payout_account:
        raise ValueError("No payment link or bank details are available yet - set up a payout account first.")

    text = compose_reminder_text(
        customer_name=customer.full_name,
        org_name=organization.name,
        estate_name=estate.name,
        plot_number=plot.plot_number,
        outstanding=summary.outstanding,
        due_at=allocation.next_payment_due_at,
        method=method,
        portal_url=portal_url,
        payout_account=payout_account,
    )

    sent = False
    error_message: str | None = None
    if channel == "sms":
        if not customer.phone:
            raise ValueError("This customer has no phone number on file")
        if not has_sms_access(get_subscription(db, allocation.organization_id)):
            raise ValueError("SMS reminders require the Pro or Enterprise plan")
        sent = bool(estate_sms.send_manual_sms(to_phone=customer.phone, message=text))
        if not sent:
            error_message = "The SMS could not be sent. Check the phone number and try again."
    elif channel == "whatsapp":
        if not customer.phone:
            raise ValueError("This customer has no phone number on file")
        digits = marketing_common.normalize_phone_digits(customer.phone)
        if not digits:
            raise ValueError("This customer's phone number could not be used for WhatsApp")
        if not whatsapp_inbox.can_reply_freely(db, allocation.estate_id, digits):
            raise ValueError("WhatsApp can only be sent within 24 hours of the customer's last message to you. Try SMS or email instead.")
        try:
            social_whatsapp.send_text(digits, text)
            sent = True
        except Exception as exc:
            error_message = f"WhatsApp message failed: {exc}"
    elif channel == "email":
        if not customer.email:
            raise ValueError("This customer has no email on file")
        sent = estate_email.send_payment_reminder_email(
            to_email=customer.email,
            customer_name=customer.full_name,
            org_name=organization.name,
            estate_name=estate.name,
            plot_number=plot.plot_number,
            outstanding=summary.outstanding,
            due_at=allocation.next_payment_due_at,
            method=method,
            portal_url=portal_url,
            payout_account=payout_account,
        )
        if not sent:
            error_message = "The email could not be sent."
    else:
        raise ValueError("Unknown reminder channel")

    record_notification_log(
        db,
        organization_id=allocation.organization_id,
        estate_id=allocation.estate_id,
        customer_id=allocation.customer_id,
        allocation_id=allocation.id,
        event_key="manual_payment_reminder",
        recipient_email=customer.email if channel == "email" else None,
        recipient_name=customer.full_name,
        subject=f"Payment reminder - {estate.name}" if channel == "email" else None,
        status="sent" if sent else "failed",
        error_message=error_message,
        channel=channel,
    )
    if not sent:
        raise ValueError(error_message or "The reminder could not be sent.")

    allocation.payment_reminder_sent_for_due_at = allocation.next_payment_due_at
    append_estate_audit_event(
        db,
        organization_id=allocation.organization_id,
        actor=actor,
        action="allocation.payment_reminder_sent",
        entity_type="estate_allocation",
        entity_id=allocation.id,
        after_data={"channel": channel, "method": method},
    )
    db.flush()
    return {"ok": True, "channel": channel, "method": method}
