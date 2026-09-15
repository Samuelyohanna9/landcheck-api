from __future__ import annotations

"""Transactional emails sent to Estate customers across the plot lifecycle - reservation/
allocation, payment updates, survey plan completion, development and staking milestones.

Every public call here is best-effort: it never raises, so a customer without an email address,
a misconfigured SMTP server, or a transient delivery failure never blocks the underlying business
action (recording a payment, allocating a plot, etc still succeeds either way). Callers don't need
their own try/except around this - just call it after the real database commit.
"""

import html
import logging
import os
import smtplib
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def format_naira(value: Decimal | float | int | None) -> str:
    if value is None:
        return "₦0.00"
    return f"₦{Decimal(value):,.2f}"


def _send_email(*, to_email: str, from_display_name: str, subject: str, body_text: str, body_html: str) -> None:
    smtp_host = str(os.getenv("SMTP_HOST") or "").strip()
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")
    smtp_port = int(str(os.getenv("SMTP_PORT") or "587").strip() or "587")
    smtp_user = str(os.getenv("SMTP_USERNAME") or "").strip()
    smtp_pass = str(os.getenv("SMTP_PASSWORD") or "").strip()
    smtp_from_email = str(os.getenv("SMTP_FROM_EMAIL") or smtp_user or "").strip()
    if not smtp_from_email:
        raise RuntimeError("SMTP_FROM_EMAIL (or SMTP_USERNAME) is not configured")
    use_ssl = _env_bool("SMTP_USE_SSL", False)
    use_tls = _env_bool("SMTP_USE_TLS", not use_ssl)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_display_name} <{smtp_from_email}>" if from_display_name else smtp_from_email
    msg["To"] = to_email
    msg["Message-ID"] = make_msgid(domain=smtp_from_email.split("@")[-1] or None)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")

    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as server:
            if smtp_user:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
        try:
            server.ehlo()
        except Exception:
            pass
        if use_tls:
            server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def _wrap_html(*, org_name: str, heading: str, message_html: str, financial_html: str) -> str:
    return f"""
    <html>
      <body style="margin:0;padding:0;background:#eef4f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#173624;">
        <div style="max-width:560px;margin:0 auto;padding:32px 16px;">
          <div style="background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 18px 46px rgba(14,46,28,0.14);border:1px solid #dceee0;padding:32px;">
            <div style="font-size:12.5px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:#5c7a68;margin:0 0 10px;">{html.escape(org_name)}</div>
            <h1 style="margin:0 0 16px;font-size:21px;color:#173624;">{html.escape(heading)}</h1>
            <div style="font-size:14.5px;line-height:1.75;color:#345542;">{message_html}</div>
            {financial_html}
            <p style="margin:26px 0 0;font-size:12px;line-height:1.6;color:#8199a5;">This is an automated update from {html.escape(org_name)} about your property. If anything here looks wrong, please contact {html.escape(org_name)} directly.</p>
          </div>
          <p style="text-align:center;font-size:11px;color:#9fb0a4;margin:16px 0 0;">Sent via LandCheck Estates &middot; Secure &middot; Private &middot; For a more certain tomorrow</p>
        </div>
      </body>
    </html>
    """


def _financial_block_html(agreed_price: Decimal, confirmed_paid: Decimal, outstanding: Decimal) -> str:
    row = "display:flex;justify-content:space-between;padding:6px 0;font-size:14px;"
    return f"""
    <div style="margin:22px 0 0;padding:16px 18px;background:#f4f9f5;border:1px solid #dceee0;border-radius:12px;">
      <div style="{row}"><span style="color:#5c7a68;">Agreed price</span><strong style="color:#173624;">{html.escape(format_naira(agreed_price))}</strong></div>
      <div style="{row}border-top:1px solid #e2efe4;"><span style="color:#5c7a68;">Total paid so far</span><strong style="color:#1d8a49;">{html.escape(format_naira(confirmed_paid))}</strong></div>
      <div style="{row}border-top:1px solid #e2efe4;"><span style="color:#5c7a68;">Outstanding balance</span><strong style="color:{'#1d8a49' if outstanding <= 0 else '#b5790a'};">{html.escape(format_naira(outstanding))}</strong></div>
    </div>
    """


_CREDIBILITY_BLURB = (
    "{org_name} is a verified developer on LandCheck Estates, where every plot boundary, "
    "allocation and payment is recorded against a surveyed, GIS-mapped record - so you always "
    "know exactly what you own and where it stands."
)


def _event_copy(event: str, *, org_name: str, estate_name: str, plot_number: str, customer_name: str, amount_just_paid: Decimal | None) -> tuple[str, str, str]:
    """Returns (subject, heading, message_html) for one lifecycle event."""
    first_name = (customer_name or "there").split(" ")[0]
    credibility = _CREDIBILITY_BLURB.format(org_name=html.escape(org_name))
    if event == "reserved":
        return (
            f"Welcome to {estate_name} - Plot {plot_number} Reserved",
            f"Welcome to {estate_name}, {html.escape(first_name)}!",
            f"<p>Thank you for choosing {html.escape(org_name)}. {credibility}</p>"
            f"<p>We're delighted to confirm that <strong>Plot {html.escape(plot_number)}</strong> at "
            f"<strong>{html.escape(estate_name)}</strong> has been reserved in your name.</p>",
        )
    if event == "allocated":
        return (
            f"Plot {plot_number} at {estate_name} is Now Allocated to You",
            f"Congratulations, {html.escape(first_name)}!",
            f"<p>{credibility}</p>"
            f"<p>Your allocation for <strong>Plot {html.escape(plot_number)}</strong> at "
            f"<strong>{html.escape(estate_name)}</strong> has now been confirmed. This plot is officially yours.</p>",
        )
    if event == "payment_recorded":
        amount_line = f"<p>We've recorded your payment of <strong>{html.escape(format_naira(amount_just_paid))}</strong> toward Plot {html.escape(plot_number)}.</p>" if amount_just_paid else ""
        return (
            f"Payment Update - Plot {plot_number}, {estate_name}",
            f"Thank you, {html.escape(first_name)}",
            amount_line + "<p>Here is your updated payment summary:</p>",
        )
    if event == "payment_completed":
        return (
            f"Plot {plot_number} is Fully Paid",
            f"Congratulations, {html.escape(first_name)} - fully paid!",
            f"<p>Your plot at <strong>{html.escape(estate_name)}</strong> is now fully paid for. "
            f"Thank you for your trust in {html.escape(org_name)}.</p>",
        )
    if event == "survey_ready":
        return (
            f"Your Official Survey Plan is Ready - Plot {plot_number}",
            f"Your survey plan is ready, {html.escape(first_name)}",
            f"<p>The official survey plan for <strong>Plot {html.escape(plot_number)}</strong> at "
            f"<strong>{html.escape(estate_name)}</strong> has been completed and is ready.</p>",
        )
    if event == "land_developed":
        return (
            f"Development Update - Plot {plot_number}, {estate_name}",
            f"An update on your plot, {html.escape(first_name)}",
            f"<p>Development has progressed on <strong>Plot {html.escape(plot_number)}</strong> at "
            f"<strong>{html.escape(estate_name)}</strong> - it is now marked as developed.</p>",
        )
    if event == "staked":
        return (
            f"Your Plot Has Been Staked - Plot {plot_number}, {estate_name}",
            f"Your boundaries are now marked on site, {html.escape(first_name)}",
            f"<p><strong>Plot {html.escape(plot_number)}</strong> at <strong>{html.escape(estate_name)}</strong> "
            f"has now been staked on the ground - its physical boundary beacons are in place.</p>",
        )
    return (f"Update on Plot {plot_number}", "An update on your plot", "<p>There is an update on your plot.</p>")


def _plain_text(heading: str, message_html_stripped: str, financial: tuple[Decimal, Decimal, Decimal] | None) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", message_html_stripped)
    text = re.sub(r"\s+", " ", text).strip()
    lines = [heading, "", text]
    if financial:
        agreed, confirmed, outstanding = financial
        lines += ["", f"Agreed price: {format_naira(agreed)}", f"Total paid so far: {format_naira(confirmed)}", f"Outstanding balance: {format_naira(outstanding)}"]
    return "\n".join(lines)


def notify_customer(
    *,
    to_email: str | None,
    customer_name: str,
    org_name: str,
    estate_name: str,
    plot_number: str,
    event: str,
    agreed_price: Decimal | None = None,
    confirmed_paid: Decimal | None = None,
    outstanding: Decimal | None = None,
    amount_just_paid: Decimal | None = None,
) -> None:
    to_email = str(to_email or "").strip()
    if not to_email:
        return
    try:
        subject, heading, message_html = _event_copy(
            event, org_name=org_name, estate_name=estate_name, plot_number=plot_number, customer_name=customer_name, amount_just_paid=amount_just_paid
        )
        has_financials = agreed_price is not None and confirmed_paid is not None and outstanding is not None
        financial_html = _financial_block_html(agreed_price, confirmed_paid, outstanding) if has_financials else ""
        body_html = _wrap_html(org_name=org_name, heading=heading, message_html=message_html, financial_html=financial_html)
        body_text = _plain_text(heading, message_html, (agreed_price, confirmed_paid, outstanding) if has_financials else None)
        _send_email(
            to_email=to_email,
            from_display_name=f"{org_name} (via LandCheck Estates)",
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
    except Exception:
        logger.exception("Estate customer notification email failed (event=%s, to=%s)", event, to_email)
