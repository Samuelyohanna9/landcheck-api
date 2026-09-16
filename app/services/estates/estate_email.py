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


def _plot_link_html(plot_link: str | None, *, label: str = "View your plot on satellite map") -> str:
    if not plot_link:
        return ""
    return f"""
    <div style="margin:22px 0 0;text-align:center;">
      <a href="{html.escape(plot_link)}" style="display:inline-block;padding:13px 26px;background:#1d8a49;color:#ffffff;font-size:14px;font-weight:700;text-decoration:none;border-radius:10px;">{html.escape(label)}</a>
    </div>
    """


def _wrap_html(*, org_name: str, heading: str, message_html: str, financial_html: str, plot_link_html: str = "") -> str:
    return f"""
    <html>
      <body style="margin:0;padding:0;background:#eef4f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#173624;">
        <div style="max-width:560px;margin:0 auto;padding:32px 16px;">
          <div style="background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 18px 46px rgba(14,46,28,0.14);border:1px solid #dceee0;padding:32px;">
            <div style="font-size:12.5px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:#5c7a68;margin:0 0 10px;">{html.escape(org_name)}</div>
            <h1 style="margin:0 0 16px;font-size:21px;color:#173624;">{html.escape(heading)}</h1>
            <div style="font-size:14.5px;line-height:1.75;color:#345542;">{message_html}</div>
            {plot_link_html}
            {financial_html}
            <p style="margin:26px 0 0;font-size:12px;line-height:1.6;color:#8199a5;">This is an automated update from {html.escape(org_name)} about your property. If anything here looks wrong, please contact {html.escape(org_name)} directly.</p>
          </div>
          <p style="text-align:center;font-size:11px;color:#9fb0a4;margin:16px 0 0;">Sent via LandCheck Estates &middot; Secure &middot; Private &middot; For a more certain tomorrow</p>
        </div>
      </body>
    </html>
    """


def _public_customer_wrap_html(*, organization_name: str, heading: str, message_html: str, plot_link_html: str = "") -> str:
    """Customer-facing shell for a public reservation confirmation.

    The internal lead alert deliberately uses the LandCheck account shell above. A buyer should
    instead receive a message that reads as a direct welcome from the Estate company, without
    internal workspace language or LandCheck support details.
    """
    return f"""
    <html>
      <body style="margin:0;padding:0;background:#f4f6f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#14261e;">
        <div style="max-width:560px;margin:0 auto;padding:32px 16px;">
          <div style="background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 18px 46px rgba(14,46,28,0.12);border:1px solid #dce8df;padding:32px;">
            <div style="font-size:12.5px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:#237b49;margin:0 0 10px;">{html.escape(organization_name)}</div>
            <h1 style="margin:0 0 16px;font-size:22px;color:#14261e;">{html.escape(heading)}</h1>
            <div style="font-size:14.5px;line-height:1.75;color:#385247;">{message_html}</div>
            {plot_link_html}
            <p style="margin:26px 0 0;font-size:12px;line-height:1.6;color:#82958a;">This message confirms your enquiry with {html.escape(organization_name)}. Please contact the Estate team directly if you need any help.</p>
          </div>
          <p style="text-align:center;font-size:11px;color:#9aaa9f;margin:16px 0 0;">{html.escape(organization_name)}</p>
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


def _plain_text(heading: str, message_html_stripped: str, financial: tuple[Decimal, Decimal, Decimal] | None, plot_link: str | None = None) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", message_html_stripped)
    text = re.sub(r"\s+", " ", text).strip()
    lines = [heading, "", text]
    if plot_link:
        lines += ["", f"View your plot on satellite map: {plot_link}"]
    if financial:
        agreed, confirmed, outstanding = financial
        lines += ["", f"Agreed price: {format_naira(agreed)}", f"Total paid so far: {format_naira(confirmed)}", f"Outstanding balance: {format_naira(outstanding)}"]
    return "\n".join(lines)


def public_plot_url(share_token: str | None) -> str | None:
    if not share_token:
        return None
    base = str(os.getenv("LANDCHECK_WEB_URL") or "").strip() or "https://landcheck.online"
    return f"{base.rstrip('/')}/estates/plot/{share_token}"


def _account_wrap_html(*, heading: str, message_html: str, button_html: str = "") -> str:
    """Same visual shell as _wrap_html, but for emails about the company's own LandCheck Estates
    account (welcome, billing, password reset) rather than a customer-facing plot update - so it
    isn't signed with a specific estate's org_name, which wouldn't apply here."""
    return f"""
    <html>
      <body style="margin:0;padding:0;background:#eef4f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#173624;">
        <div style="max-width:560px;margin:0 auto;padding:32px 16px;">
          <div style="background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 18px 46px rgba(14,46,28,0.14);border:1px solid #dceee0;padding:32px;">
            <div style="font-size:12.5px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:#5c7a68;margin:0 0 10px;">LandCheck Estates</div>
            <h1 style="margin:0 0 16px;font-size:21px;color:#173624;">{html.escape(heading)}</h1>
            <div style="font-size:14.5px;line-height:1.75;color:#345542;">{message_html}</div>
            {button_html}
            <p style="margin:26px 0 0;font-size:12px;line-height:1.6;color:#8199a5;">If you didn't expect this email, you can safely ignore it, or contact us at landchecktech@gmail.com.</p>
          </div>
          <p style="text-align:center;font-size:11px;color:#9fb0a4;margin:16px 0 0;">Sent via LandCheck Estates &middot; Secure &middot; Private &middot; For a more certain tomorrow</p>
        </div>
      </body>
    </html>
    """


def _account_button_html(*, label: str, url: str) -> str:
    return f"""
    <div style="margin:24px 0 0;text-align:center;">
      <a href="{html.escape(url)}" style="display:inline-block;padding:13px 26px;background:#1d8a49;color:#ffffff;font-size:14px;font-weight:700;text-decoration:none;border-radius:10px;">{html.escape(label)}</a>
    </div>
    """


def _account_plain_text(heading: str, message_html: str, url: str | None = None) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", message_html)
    text = re.sub(r"\s+", " ", text).strip()
    lines = [heading, "", text]
    if url:
        lines += ["", url]
    return "\n".join(lines)


def notify_reservation_request(
    *,
    to_email: str | None,
    organization_name: str,
    estate_name: str,
    plot_number: str,
    full_name: str,
    phone: str,
    email: str | None = None,
    message: str | None = None,
) -> bool:
    """Notify the developer that a public visitor requested a plot reservation.

    This is deliberately separate from ``notify_customer``: a public reservation is a sales lead,
    not an internal customer or allocation until the team verifies and converts it.
    """
    to_email = str(to_email or "").strip()
    if not to_email:
        return False
    safe_message = html.escape(str(message or "").strip())
    contact_line = f"<p><strong>Phone:</strong> {html.escape(phone)}</p>"
    if email:
        contact_line += f"<p><strong>Email:</strong> {html.escape(email)}</p>"
    if safe_message:
        contact_line += f"<p><strong>Message:</strong><br/>{safe_message}</p>"
    message_html = (
        f"<p><strong>{html.escape(full_name)}</strong> requested a reservation for "
        f"<strong>Plot {html.escape(plot_number)}</strong> at "
        f"<strong>{html.escape(estate_name)}</strong> from the public Estate page.</p>"
        f"{contact_line}"
        "<p>Open your Estate workspace to follow up and convert the lead into a customer allocation "
        "when the details are confirmed.</p>"
    )
    body_html = _account_wrap_html(heading="New plot reservation request", message_html=message_html)
    body_text = _account_plain_text("New plot reservation request", message_html)
    try:
        _send_email(
            to_email=to_email,
            from_display_name="LandCheck Estates",
            subject=f"New reservation request - Plot {plot_number}",
            body_text=body_text,
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate reservation notification failed (estate=%s, plot=%s)", estate_name, plot_number)
        return False


def send_public_reservation_welcome(
    *,
    to_email: str | None,
    full_name: str | None = None,
    organization_name: str,
    estate_name: str,
    plot_number: str,
    plot_address: str | None = None,
    area_sqm: Decimal | float | None = None,
    price: Decimal | float | None = None,
    payment_plan: list[dict] | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    public_page_url: str | None = None,
) -> bool:
    """Welcome a buyer who submitted a reservation request from the public Estate page."""
    to_email = str(to_email or "").strip()
    if not to_email:
        return False
    details = [f"<p><strong>Plot:</strong> {html.escape(plot_number)}</p>"]
    if plot_address:
        details.append(f"<p><strong>Address:</strong> {html.escape(plot_address)}</p>")
    if area_sqm is not None:
        details.append(f"<p><strong>Land area:</strong> {html.escape(f'{Decimal(area_sqm):,.2f} sq m')}</p>")
    if price is not None:
        details.append(f"<p><strong>Advertised price:</strong> {html.escape(format_naira(price))}</p>")
    plan_html = ""
    if payment_plan:
        plan_rows = "".join(
            f"<li>{html.escape(str(item.get('label') or 'Payment'))}: <strong>{html.escape(str(item.get('percentage') or '0'))}%</strong></li>"
            for item in payment_plan
        )
        plan_html = f"<p><strong>Payment plan shown by the Estate:</strong></p><ul style=\"padding-left:20px;line-height:1.8;\">{plan_rows}</ul>"
    contact_bits = []
    if contact_phone:
        contact_bits.append(f"<a href=\"tel:{html.escape(contact_phone)}\">{html.escape(contact_phone)}</a>")
    if contact_email:
        contact_bits.append(f"<a href=\"mailto:{html.escape(contact_email)}\">{html.escape(contact_email)}</a>")
    contact_suffix = f": {' or '.join(contact_bits)}" if contact_bits else "."
    message_html = (
        f"<p>Dear {html.escape((str(full_name or '').strip().split(' ')[0] or 'customer'))},</p>"
        f"<p>Welcome to <strong>{html.escape(estate_name)}</strong>, and thank you for choosing "
        f"<strong>{html.escape(organization_name)}</strong>. We have received your request for "
        f"<strong>Plot {html.escape(plot_number)}</strong>.</p>"
        + "".join(details)
        + plan_html
        + f"<p>A member of the {html.escape(organization_name)} team will contact you shortly to confirm availability, documentation and the next steps for completing your reservation.</p>"
        + f"<p>If you have any questions in the meantime, please contact {html.escape(organization_name)}{contact_suffix}</p>"
    )
    body_html = _public_customer_wrap_html(
        organization_name=organization_name,
        heading=f"Welcome to {estate_name}",
        message_html=message_html,
        plot_link_html=_plot_link_html(public_page_url, label="View the Estate page"),
    )
    body_text = _plain_text(f"Welcome to {estate_name}", message_html, None, public_page_url)
    try:
        _send_email(
            to_email=to_email,
            from_display_name=organization_name,
            subject=f"Welcome to {estate_name} - Plot {plot_number}",
            body_text=body_text,
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Public Estate reservation welcome email failed (estate=%s, plot=%s)", estate_name, plot_number)
        return False


def send_welcome_email(*, organization, account) -> bool:
    """Sent right after a new company registers - the first email a customer ever gets from us,
    so it doubles as a mini product tour and pricing reference rather than a bare "you're in"."""
    to_email = str(getattr(account, "email", "") or "").strip()
    if not to_email:
        return False
    first_name = str(getattr(account, "full_name", "") or "there").split(" ")[0]
    web_url = os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online"
    message_html = f"""
    <p>Welcome to LandCheck Estates, {html.escape(first_name)} - your workspace for
    <strong>{html.escape(str(getattr(organization, "name", "") or "your company"))}</strong> is ready.</p>
    <p>Here's what you can do once you pick a plan:</p>
    <ul style="padding-left:18px;line-height:1.9;">
      <li>Bring in a layout (survey coordinates, CAD, CSV, or a scanned plan) and manage every plot from one map</li>
      <li>Track customers, reservations, allocations, payments and receipts</li>
      <li>Run a tiered sales-agent commission ladder with payout tracking</li>
      <li>Produce survey plans, staking coordinates and DGPS exports</li>
      <li>Screen flood and erosion risk for a whole layout (Plus plan)</li>
    </ul>
    <p><strong>Basic</strong> is &#8358;19,500/month (or &#8358;220,000/year) - everything except flood and erosion hazard analysis.<br/>
    <strong>Plus</strong> is &#8358;24,500/month (or &#8358;285,000/year) - everything, including hazard analysis.</p>
    <p>Both plans start with a 3-day free trial, and you can cancel anytime.</p>
    """
    body_html = _account_wrap_html(heading=f"Welcome to LandCheck Estates, {first_name}!", message_html=message_html, button_html=_account_button_html(label="Choose your plan", url=f"{web_url.rstrip('/')}/estates/choose-plan"))
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject="Welcome to LandCheck Estates", body_text=_account_plain_text("Welcome to LandCheck Estates", message_html), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate welcome email failed (to=%s)", to_email)
        return False


def _subscription_org_email(organization) -> str | None:
    value = str(getattr(organization, "contact_email", "") or "").strip()
    return value or None


def send_trial_started_email(*, organization, subscription) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    plan_label = "Plus" if subscription.plan_key == "plus" else "Basic"
    trial_ends = subscription.trial_ends_at.strftime("%d %b %Y") if subscription.trial_ends_at else "in 3 days"
    message_html = f"<p>Your {html.escape(plan_label)} plan trial has started - you have full access until <strong>{html.escape(trial_ends)}</strong>.</p><p>We'll automatically charge {html.escape(format_naira(subscription.amount))} to your card on file when the trial ends, unless you cancel first. You can cancel anytime from Settings &rarr; Billing.</p>"
    body_html = _account_wrap_html(heading="Your free trial has started", message_html=message_html)
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject=f"Your {plan_label} plan trial has started", body_text=_account_plain_text("Your free trial has started", message_html), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate trial-started email failed (org=%s)", organization.id)
        return False


def send_payment_receipt_email(*, organization, subscription) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    plan_label = "Plus" if subscription.plan_key == "plus" else "Basic"
    period_end = subscription.current_period_end.strftime("%d %b %Y") if subscription.current_period_end else ""
    message_html = f"<p>We've charged {html.escape(format_naira(subscription.amount))} for your {html.escape(plan_label)} plan ({html.escape(subscription.billing_cycle)}).</p><p>Your subscription is active through <strong>{html.escape(period_end)}</strong>.</p>"
    body_html = _account_wrap_html(heading="Payment received", message_html=message_html)
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject="LandCheck Estates - payment receipt", body_text=_account_plain_text("Payment received", message_html), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate payment receipt email failed (org=%s)", organization.id)
        return False


def send_payment_failed_email(*, organization, subscription) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    web_url = os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online"
    message_html = f"<p>We couldn't charge your card on file for {html.escape(format_naira(subscription.amount))}. We'll try again shortly, but please update your payment method to avoid losing access.</p>"
    body_html = _account_wrap_html(heading="Your payment didn't go through", message_html=message_html, button_html=_account_button_html(label="Update payment method", url=f"{web_url.rstrip('/')}/estates/billing"))
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject="Action needed - LandCheck Estates payment failed", body_text=_account_plain_text("Your payment didn't go through", message_html), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate payment-failed email failed (org=%s)", organization.id)
        return False


def send_subscription_canceled_email(*, organization, subscription, reason: str) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    if reason == "payment_failed":
        message_html = "<p>After several failed attempts, we've cancelled your subscription and access has been suspended. You can resubscribe anytime from your workspace.</p>"
        subject = "Your LandCheck Estates subscription was cancelled"
    else:
        message_html = "<p>Your cancellation is confirmed. You'll keep full access until the end of your current billing period, and you won't be charged again.</p>" if subscription.cancel_at_period_end else "<p>Your subscription has been cancelled.</p>"
        subject = "Your LandCheck Estates subscription has been cancelled"
    body_html = _account_wrap_html(heading="Subscription cancelled", message_html=message_html)
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject=subject, body_text=_account_plain_text("Subscription cancelled", message_html), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate subscription-cancelled email failed (org=%s)", organization.id)
        return False


def send_password_reset_email(*, account, reset_link: str) -> bool:
    to_email = str(getattr(account, "email", "") or "").strip()
    if not to_email:
        return False
    message_html = "<p>We received a request to reset your LandCheck Estates password. This link expires in 1 hour.</p>"
    body_html = _account_wrap_html(heading="Reset your password", message_html=message_html, button_html=_account_button_html(label="Reset password", url=reset_link))
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject="Reset your LandCheck Estates password", body_text=_account_plain_text("Reset your password", message_html, reset_link), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate password reset email failed (to=%s)", to_email)
        return False


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
    share_token: str | None = None,
) -> bool:
    """Returns True once the email has actually been sent (not merely queued/attempted) - False if
    there was no address to send to, or delivery failed. Callers use this to tell the person acting
    in the dashboard whether a customer really was notified, rather than assuming it silently."""
    to_email = str(to_email or "").strip()
    if not to_email:
        return False
    try:
        subject, heading, message_html = _event_copy(
            event, org_name=org_name, estate_name=estate_name, plot_number=plot_number, customer_name=customer_name, amount_just_paid=amount_just_paid
        )
        has_financials = agreed_price is not None and confirmed_paid is not None and outstanding is not None
        financial_html = _financial_block_html(agreed_price, confirmed_paid, outstanding) if has_financials else ""
        plot_link = public_plot_url(share_token)
        body_html = _wrap_html(org_name=org_name, heading=heading, message_html=message_html, financial_html=financial_html, plot_link_html=_plot_link_html(plot_link))
        body_text = _plain_text(heading, message_html, (agreed_price, confirmed_paid, outstanding) if has_financials else None, plot_link)
        _send_email(
            to_email=to_email,
            from_display_name=f"{org_name} (via LandCheck Estates)",
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate customer notification email failed (event=%s, to=%s)", event, to_email)
        return False
