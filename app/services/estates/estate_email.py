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

from app.services.estates.billing_plans import ESTATE_PLANS

logger = logging.getLogger(__name__)
EMAIL_VERIFICATION_TTL_HOURS = 24

# Estate's own email palette/typography - mirrors landcheck-web's estate-portal.css (.estate-portal
# scope) so every Estate email reads like an extension of the public site and dashboard, rather
# than the generic LandCheck shell shared Green/Survey emails still use (see
# app/utils/email_branding.py, intentionally left untouched - this is Estate-specific).
_ESTATE_PAPER = "#f5f3eb"
_ESTATE_INK = "#102a27"
_ESTATE_SLATE = "#5d6f69"
_ESTATE_TEAL = "#16816e"
_ESTATE_LINE = "rgba(16,42,39,0.14)"
_ESTATE_LINE_SOFT = "rgba(16,42,39,0.08)"
_ESTATE_FAINT = "#8f9d95"
_ESTATE_SURFACE_TINT = "#faf8f1"
_ESTATE_LOGO_URL = "https://landcheck.online/logo-wordmark-web.png"
# Georgia is the closest email-safe fallback to the landing page's "Iowan Old Style" serif - that
# exact face is Apple-only and would silently fall back anyway in most mail clients.
_ESTATE_SERIF = "Georgia,'Times New Roman','Noto Serif',serif"
_ESTATE_SANS = "Arial,'Segoe UI',Helvetica,sans-serif"


def _estate_initials(name: str) -> str:
    import re

    words = [w for w in re.split(r"\s+", str(name or "").strip()) if w]
    if not words:
        return "E"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _estate_footer_html(*, context_line: str = "", show_landcheck_branding: bool = True) -> str:
    """The "official" Estate footer - a consistent block every Estate email ends with. Buyer-facing
    white-label emails (show_landcheck_branding=False) keep this to just the context line, since
    those are meant to read as coming from the developer, not LandCheck."""
    context_html = f'<div style="margin-top:3px;">{context_line}</div>' if context_line else ""
    if not show_landcheck_branding:
        return f'<div style="font:400 12.5px/1.75 {_ESTATE_SANS};color:{_ESTATE_SLATE};">{context_html}</div>'
    return f"""
    <div style="font:400 12.5px/1.75 {_ESTATE_SANS};color:{_ESTATE_SLATE};">
      <div style="font-family:{_ESTATE_SERIF};font-size:14px;font-weight:600;color:{_ESTATE_INK};">LandCheck Estates</div>
      {context_html}
      <div style="margin-top:10px;">
        <a href="mailto:support@landcheck.online" style="color:{_ESTATE_TEAL};text-decoration:none;">support@landcheck.online</a>
        &nbsp;&middot;&nbsp;
        <a href="https://landcheck.online/estates" style="color:{_ESTATE_TEAL};text-decoration:none;">landcheck.online/estates</a>
      </div>
    </div>
    <div style="margin-top:16px;padding-top:14px;border-top:1px solid {_ESTATE_LINE_SOFT};font-size:11px;color:{_ESTATE_FAINT};">
      LandCheck Geospatial Technologies Limited<br/>
      <a href="https://www.instagram.com/land.check/" style="color:{_ESTATE_FAINT};text-decoration:none;">Instagram</a>&nbsp;&middot;&nbsp;
      <a href="https://www.facebook.com/landcheck/" style="color:{_ESTATE_FAINT};text-decoration:none;">Facebook</a>&nbsp;&middot;&nbsp;
      <a href="https://www.linkedin.com/company/landcheck-geospatial/" style="color:{_ESTATE_FAINT};text-decoration:none;">LinkedIn</a>
    </div>
    """


def _estate_shell(*, brand_name: str, logo_text: str, kicker: str, title: str, subtitle: str, body_html: str, footer_html: str, footer_note: str, logo_image_url: str | None = None) -> str:
    """Estate's own HTML email shell - same structural idea as the shared
    render_branded_email_shell (logo + kicker + title/subtitle header, body, footer, outer
    disclaimer), restyled to the Estate landing page's own palette and serif display type instead
    of the generic LandCheck dashboard green. Inline styles throughout and no web fonts, for the
    same email-client-compatibility reasons the shared shell documents - the one exception is
    logo_image_url (the real wordmark, hosted at a stable landcheck.online path): a logo mark is
    expected to be a real image in a professional transactional email, and alt text covers the
    rare client that blocks it, same tradeoff every other SaaS product's emails make."""
    logo_html = (
        f'<td style="vertical-align:middle;"><img src="{logo_image_url}" alt="{brand_name}" height="28" style="display:block;height:28px;width:auto;border:0;" /></td>'
        if logo_image_url
        else (
            f'<td style="width:36px;height:36px;vertical-align:middle;border-radius:4px;background:{_ESTATE_INK};color:#ffffff;text-align:center;font:800 13px {_ESTATE_SANS};letter-spacing:-0.02em;">{logo_text}</td>'
            f'<td style="vertical-align:middle;padding-left:11px;"><span style="font:800 13px {_ESTATE_SANS};letter-spacing:-0.01em;color:{_ESTATE_INK};">{brand_name}</span></td>'
        )
    )
    return f"""
    <html>
      <body style="margin:0;padding:0;background:{_ESTATE_PAPER};font-family:{_ESTATE_SANS};color:{_ESTATE_INK};">
        <div style="max-width:620px;margin:0 auto;padding:32px 16px;">
          <div style="background:#ffffff;border:1px solid {_ESTATE_LINE};border-radius:4px;overflow:hidden;">
            <div style="height:3px;background:{_ESTATE_INK};font-size:0;line-height:0;">&nbsp;</div>
            <div style="padding:30px 34px 24px;border-bottom:1px solid {_ESTATE_LINE_SOFT};">
              <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  {logo_html}
                </tr>
              </table>
              <div style="margin-top:22px;font:800 11px {_ESTATE_SANS};letter-spacing:0.16em;text-transform:uppercase;color:{_ESTATE_TEAL};">{kicker}</div>
              <div style="margin-top:9px;font-family:{_ESTATE_SERIF};font-size:25px;font-weight:500;letter-spacing:-0.02em;line-height:1.24;color:{_ESTATE_INK};">{title}</div>
              <div style="margin-top:10px;font:400 14px/1.7 {_ESTATE_SANS};color:{_ESTATE_SLATE};">{subtitle}</div>
            </div>
            <div style="padding:26px 34px 10px;font:400 14.5px/1.7 {_ESTATE_SANS};color:{_ESTATE_INK};">
              {body_html}
            </div>
            <div style="padding:22px 34px 28px;border-top:1px solid {_ESTATE_LINE_SOFT};margin-top:14px;">
              {footer_html}
            </div>
          </div>
          <p style="text-align:center;font:400 11px/1.6 {_ESTATE_SANS};color:{_ESTATE_FAINT};margin:18px 0 0;">{footer_note}</p>
        </div>
      </body>
    </html>
    """


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

    reply_to = str(os.getenv("SMTP_REPLY_TO") or "support@landcheck.online").strip()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_display_name} <{smtp_from_email}>" if from_display_name else smtp_from_email
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to
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
      <a href="{html.escape(plot_link)}" style="display:inline-block;padding:13px 22px;background:{_ESTATE_INK};color:#ffffff;font:700 13px {_ESTATE_SANS};text-decoration:none;border-radius:2px;">{html.escape(label)}</a>
    </div>
    """


def _wrap_html(*, org_name: str, heading: str, message_html: str, financial_html: str, plot_link_html: str = "") -> str:
    safe_org_name = html.escape(org_name)
    return _estate_shell(
        brand_name="LandCheck Estates",
        logo_text="LC",
        logo_image_url=_ESTATE_LOGO_URL,
        kicker=safe_org_name,
        title=html.escape(heading),
        subtitle="A secure update about your Estate property.",
        body_html=f"{message_html}{plot_link_html}{financial_html}",
        footer_html=_estate_footer_html(context_line=f"Sent on behalf of <strong style=\"color:{_ESTATE_INK};\">{safe_org_name}</strong>"),
        footer_note=f"This is an automated update from {safe_org_name} about your property. If anything here looks wrong, please contact {safe_org_name} directly.",
    )


def _public_customer_wrap_html(*, organization_name: str, heading: str, message_html: str, plot_link_html: str = "") -> str:
    """Customer-facing shell for a public reservation confirmation.

    The internal lead alert deliberately uses the LandCheck account shell above. A buyer should
    instead receive a message that reads as a direct welcome from the Estate company, without
    internal workspace language or LandCheck support details.
    """
    safe_org_name = html.escape(organization_name)
    return _estate_shell(
        brand_name=safe_org_name,
        logo_text=_estate_initials(organization_name),
        kicker="Estate reservation",
        title=html.escape(heading),
        subtitle="Your reservation enquiry has been received by the Estate team.",
        body_html=f"{message_html}{plot_link_html}",
        footer_html=_estate_footer_html(context_line=f"<strong style=\"color:{_ESTATE_INK};\">{safe_org_name}</strong><br/>Estate enquiries", show_landcheck_branding=False),
        footer_note=f"This message confirms your enquiry with {safe_org_name}. Please contact the Estate team directly if you need any help.",
    )


def _financial_block_html(agreed_price: Decimal, confirmed_paid: Decimal, outstanding: Decimal) -> str:
    row = "display:flex;justify-content:space-between;padding:6px 0;font-size:14px;"
    return f"""
    <div style="margin:22px 0 0;padding:16px 18px;background:{_ESTATE_SURFACE_TINT};border:1px solid {_ESTATE_LINE};border-radius:4px;">
      <div style="{row}"><span style="color:{_ESTATE_SLATE};">Agreed price</span><strong style="color:{_ESTATE_INK};">{html.escape(format_naira(agreed_price))}</strong></div>
      <div style="{row}border-top:1px solid {_ESTATE_LINE_SOFT};"><span style="color:{_ESTATE_SLATE};">Total paid so far</span><strong style="color:{_ESTATE_TEAL};">{html.escape(format_naira(confirmed_paid))}</strong></div>
      <div style="{row}border-top:1px solid {_ESTATE_LINE_SOFT};"><span style="color:{_ESTATE_SLATE};">Outstanding balance</span><strong style="color:{_ESTATE_TEAL if outstanding <= 0 else '#a3690a'};">{html.escape(format_naira(outstanding))}</strong></div>
    </div>
    """


_CREDIBILITY_BLURB = (
    "{org_name} is a verified developer on LandCheck Estates, where every plot boundary, "
    "allocation and payment is recorded against a surveyed, GIS-mapped record - so you always "
    "know exactly what you own and where it stands."
)


def _event_copy(
    event: str,
    *,
    org_name: str,
    estate_name: str,
    plot_number: str,
    customer_name: str,
    amount_just_paid: Decimal | None,
    payment_due_at=None,
) -> tuple[str, str, str]:
    """Returns (subject, heading, message_html) for one lifecycle event."""
    first_name = (customer_name or "there").split(" ")[0]
    credibility = _CREDIBILITY_BLURB.format(org_name=html.escape(org_name))
    if event == "reserved":
        next_payment_line = (
            f"<p>Your first payment has been recorded. Your next scheduled payment is due on "
            f"<strong>{html.escape(payment_due_at.strftime('%d %b %Y'))}</strong>. We will email you "
            "up to 7 days before each scheduled due date.</p>"
            if payment_due_at
            else "<p>Your first payment has been recorded. Your Estate team will confirm any next payment date with you.</p>"
        )
        return (
            f"Welcome to {estate_name} - Plot {plot_number} Reserved",
            f"Welcome to {estate_name}, {html.escape(first_name)}!",
            f"<p>Thank you for choosing {html.escape(org_name)}. {credibility}</p>"
            f"<p>We're delighted to confirm that <strong>Plot {html.escape(plot_number)}</strong> at "
            f"<strong>{html.escape(estate_name)}</strong> has been reserved in your name.</p>"
            f"{next_payment_line}",
        )
    if event == "reservation_expiring":
        return (
            f"Reservation deadline approaching - Plot {plot_number}",
            f"Your reservation deadline is approaching, {html.escape(first_name)}",
            f"<p>Your reservation for <strong>Plot {html.escape(plot_number)}</strong> at <strong>{html.escape(estate_name)}</strong> will expire soon.</p>"
            f"<p>Please contact {html.escape(org_name)} to confirm payment or the next step before the deadline.</p>",
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
    if event == "payment_reminder":
        due_label = payment_due_at.strftime("%d %b %Y") if payment_due_at else "the scheduled date"
        return (
            f"Payment reminder - Plot {plot_number}, {estate_name}",
            f"Your next payment is due on {due_label}",
            f"<p>This is a reminder that your next payment for <strong>Plot {html.escape(plot_number)}</strong> "
            f"at <strong>{html.escape(estate_name)}</strong> is due on <strong>{html.escape(due_label)}</strong>.</p>"
            f"<p>Please use your buyer portal below to review your balance and payment history, or contact {html.escape(org_name)} if you need help.</p>",
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


def _plain_text(
    heading: str,
    message_html_stripped: str,
    financial: tuple[Decimal, Decimal, Decimal] | None,
    plot_link: str | None = None,
    portal_link: str | None = None,
) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", message_html_stripped)
    text = re.sub(r"\s+", " ", text).strip()
    lines = [heading, "", text]
    if plot_link:
        lines += ["", f"View your plot on satellite map: {plot_link}"]
    if portal_link:
        lines += ["", f"Open your buyer portal: {portal_link}"]
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
    return _estate_shell(
        brand_name="LandCheck Estates",
        logo_text="LC",
        logo_image_url=_ESTATE_LOGO_URL,
        kicker="Estate workspace",
        title=html.escape(heading),
        subtitle="Your LandCheck Estates workspace update is ready.",
        body_html=f"{message_html}{button_html}",
        footer_html=_estate_footer_html(),
        footer_note="If you didn't expect this email, you can safely ignore it, or contact us at support@landcheck.online.",
    )


def _account_button_html(*, label: str, url: str) -> str:
    return f"""
    <div style="margin:24px 0 0;text-align:center;">
      <a href="{html.escape(url)}" style="display:inline-block;padding:13px 22px;background:{_ESTATE_INK};color:#ffffff;font:700 13px {_ESTATE_SANS};text-decoration:none;border-radius:2px;">{html.escape(label)}</a>
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


def notify_geotech_survey_request(
    *,
    organization_name: str,
    estate_name: str,
    plot_number: str | None = None,
    requested_by_name: str | None = None,
    contact_name: str | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    note: str | None = None,
) -> bool:
    """Notify LandCheck's own team that an Estate staff member requested a geotechnical survey -
    the honest handoff from Soil Analysis, which gives indicative bearing-capacity and water-table
    readings from satellite data but is explicitly not a soil test and can't determine soil
    density/consistency or subsurface layers. Unlike notify_reservation_request (a public buyer
    lead sent to the Estate's own team), this goes to LandCheck's own admin address, since
    LandCheck - not the Estate - is the one who would need to source/vet a geotechnical partner
    for the customer.
    """
    scope_line = f"Plot {html.escape(plot_number)}" if plot_number else "the whole Estate boundary"
    contact_bits = []
    if contact_name:
        contact_bits.append(f"<p><strong>Contact name:</strong> {html.escape(contact_name)}</p>")
    if contact_phone:
        contact_bits.append(f"<p><strong>Phone:</strong> {html.escape(contact_phone)}</p>")
    if contact_email:
        contact_bits.append(f"<p><strong>Email:</strong> {html.escape(contact_email)}</p>")
    if note:
        contact_bits.append(f"<p><strong>Note:</strong><br/>{html.escape(str(note).strip())}</p>")
    contact_html = "".join(contact_bits)
    message_html = (
        f"<p><strong>{html.escape(organization_name)}</strong> requested a geotechnical survey for "
        f"{scope_line} at <strong>{html.escape(estate_name)}</strong>, from the Estate dashboard's "
        "Soil Analysis tool."
        + (f" Requested by {html.escape(requested_by_name)}." if requested_by_name else "")
        + "</p>"
        f"{contact_html}"
        "<p>Soil Analysis is satellite-based and gives indicative readings only - it does not "
        "determine soil density/consistency or subsurface soil layers - follow up to connect this "
        "customer with a licensed geotechnical investigation.</p>"
    )
    body_html = _account_wrap_html(heading="Geotechnical survey requested", message_html=message_html)
    body_text = _account_plain_text("Geotechnical survey requested", message_html)
    try:
        _send_email(
            to_email="admin@landcheck.online",
            from_display_name="LandCheck Estates",
            subject=f"Geotechnical survey requested - {estate_name}",
            body_text=body_text,
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Geotech survey request notification failed (estate=%s, plot=%s)", estate_name, plot_number)
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


def send_welcome_email(*, organization, account, subscription=None) -> bool:
    """Sent right after a new company registers - the first email a customer ever gets from us,
    so it doubles as a mini product tour and pricing reference rather than a bare "you're in"."""
    to_email = str(getattr(account, "email", "") or "").strip()
    if not to_email:
        return False
    first_name = str(getattr(account, "full_name", "") or "there").split(" ")[0]
    web_url = os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online"
    trial_context = (
        f"<p>Your {html.escape(str(subscription.plan_key).title())} plan trial is active until "
        f"<strong>{html.escape(subscription.trial_ends_at.strftime('%d %b %Y') if subscription.trial_ends_at else 'the trial end date')}</strong>. "
        "Your workspace access is ready now.</p>"
        if subscription
        else "<p>Choose a plan to activate your workspace trial.</p>"
    )
    message_html = f"""
    <p>Welcome to LandCheck Estates, {html.escape(first_name)} - your workspace for
    <strong>{html.escape(str(getattr(organization, "name", "") or "your company"))}</strong> is ready.</p>
    {trial_context}
    <ul style="padding-left:18px;line-height:1.9;">
      <li>Bring in a layout (survey coordinates, CAD, CSV, or a scanned plan) and manage every plot from one map</li>
      <li>Track customers, reservations, allocations, payments and receipts</li>
      <li>Run a tiered sales-agent commission ladder with payout tracking</li>
      <li>Produce survey plans, staking coordinates and DGPS exports</li>
      <li>Screen flood and erosion risk for a whole layout (Plus plan)</li>
    </ul>
    <p><strong>Basic</strong> is &#8358;19,500/month (or &#8358;220,000/year) - everything except flood and erosion hazard analysis.<br/>
    <strong>Plus</strong> is &#8358;24,500/month (or &#8358;285,000/year) - everything, including hazard analysis.</p>
    <p>{"Your subscription will use scheduled bank-transfer reminders." if subscription and getattr(subscription, "payment_method", "card") == "bank_transfer" else "Both plans start with a 3-day free trial, and you can cancel anytime."}</p>
    """
    action_label = "Open your workspace" if subscription else "Choose your plan"
    action_path = "estates/workspace" if subscription else "estates/choose-plan"
    body_html = _account_wrap_html(heading=f"Welcome to LandCheck Estates, {first_name}!", message_html=message_html, button_html=_account_button_html(label=action_label, url=f"{web_url.rstrip('/')}/{action_path}"))
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject="Welcome to LandCheck Estates", body_text=_account_plain_text("Welcome to LandCheck Estates", message_html, f"{web_url.rstrip('/')}/{action_path}"), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate welcome email failed (to=%s)", to_email)
        return False


def send_email_verification_email(*, organization, account, verification_url: str) -> bool:
    to_email = str(getattr(account, "email", "") or "").strip()
    if not to_email or not verification_url:
        return False
    first_name = str(getattr(account, "full_name", "") or "there").split(" ")[0]
    organization_name = str(getattr(organization, "name", "your company") or "your company")
    message_html = (
        f"<p>Hello {html.escape(first_name)},</p>"
        f"<p>Confirm this email address to activate the LandCheck Estates workspace for "
        f"<strong>{html.escape(organization_name)}</strong>.</p>"
        f"<p>This verification link expires in {EMAIL_VERIFICATION_TTL_HOURS} hours.</p>"
    )
    body_html = _account_wrap_html(
        heading="Verify your company email",
        message_html=message_html,
        button_html=_account_button_html(label="Verify email address", url=verification_url),
    )
    try:
        _send_email(
            to_email=to_email,
            from_display_name="LandCheck Estates",
            subject="Verify your LandCheck Estates email",
            body_text=_account_plain_text("Verify your company email", message_html, verification_url),
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate email verification failed")
        return False


def send_agent_workspace_invite(*, organization, member, portal_url: str) -> bool:
    """Send a lightweight, revocable workspace link to an agent or marketer."""
    to_email = str(getattr(member, "contact_email", "") or "").strip()
    if not to_email:
        return False
    name = str(getattr(member, "subject_id", "there") or "there")
    role = str(getattr(member, "role_key", "agent") or "agent").replace("_", " ").title()
    organization_name = str(getattr(organization, "name", "your company") or "your company")
    message_html = (
        f"<p>Hello {html.escape(name)},</p>"
        f"<p><strong>{html.escape(organization_name)}</strong> has invited you to its LandCheck Estates "
        f"{html.escape(role)} workspace.</p>"
        "<p>Use the secure link below to view estate layouts, your QR leads, buyer payment progress, "
        "and commission records. The link can be revoked by the company at any time.</p>"
    )
    body_html = _account_wrap_html(
        heading=f"Your {role} workspace is ready",
        message_html=message_html,
        button_html=_account_button_html(label="Open agent workspace", url=portal_url),
    )
    try:
        _send_email(
            to_email=to_email,
            from_display_name="LandCheck Estates",
            subject=f"Your LandCheck Estates workspace - {organization_name}",
            body_text=_account_plain_text(f"Your {role} workspace is ready", message_html, portal_url),
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate agent invite email failed (to=%s)", to_email)
        return False


def send_staff_invite_email(
    *,
    organization,
    account,
    role_label: str,
    permission_labels: list[str],
    temp_password: str,
    login_url: str,
) -> bool:
    """Sent when the organization owner adds a full dashboard staff account via "Add Access" -
    the one email that ever carries a plaintext password, since it's the only copy that will
    ever exist (only the hash is stored). first login forces the recipient to set their own."""
    to_email = str(getattr(account, "email", "") or "").strip()
    if not to_email:
        return False
    first_name = str(getattr(account, "full_name", "") or "there").split(" ")[0]
    organization_name = str(getattr(organization, "name", "your company") or "your company")
    access_list_html = (
        "<ul style=\"padding-left:18px;line-height:1.9;\">"
        + "".join(f"<li>{html.escape(item)}</li>" for item in permission_labels)
        + "</ul>"
        if permission_labels
        else "<p>No dashboard sections are enabled yet - ask an administrator to grant access.</p>"
    )
    message_html = (
        f"<p>Hello {html.escape(first_name)},</p>"
        f"<p><strong>{html.escape(organization_name)}</strong> has added you to its LandCheck Estates "
        f"dashboard as <strong>{html.escape(role_label)}</strong>.</p>"
        f"<p>Your access covers:</p>"
        f"{access_list_html}"
        "<p>Sign in with the temporary password below - you'll be asked to set your own password "
        "the first time you log in.</p>"
        f"<p style=\"margin:18px 0;padding:14px 16px;background:{_ESTATE_SURFACE_TINT};border:1px solid {_ESTATE_LINE};"
        f"border-radius:4px;font-size:15px;\">Email: <strong>{html.escape(to_email)}</strong><br/>"
        f"Temporary password: <strong style=\"font-family:monospace;font-size:16px;letter-spacing:0.5px;\">"
        f"{html.escape(temp_password)}</strong></p>"
    )
    body_html = _account_wrap_html(
        heading=f"You've been added to {organization_name}",
        message_html=message_html,
        button_html=_account_button_html(label="Log in to LandCheck Estates", url=login_url),
    )
    try:
        _send_email(
            to_email=to_email,
            from_display_name="LandCheck Estates",
            subject=f"You've been added to {organization_name} on LandCheck Estates",
            body_text=_account_plain_text(
                f"You've been added to {organization_name}",
                message_html,
                login_url,
            )
            + f"\n\nEmail: {to_email}\nTemporary password: {temp_password}",
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate staff invite email failed (to=%s)", to_email)
        return False


def _subscription_org_email(organization) -> str | None:
    value = str(getattr(organization, "contact_email", "") or "").strip()
    return value or None


def send_trial_started_email(*, organization, subscription) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    plan_label = str(ESTATE_PLANS.get(subscription.plan_key, {}).get("label") or "Basic")
    trial_ends = subscription.trial_ends_at.strftime("%d %b %Y") if subscription.trial_ends_at else "in 3 days"
    payment_message = (
        f"Your next subscription payment will be {html.escape(format_naira(subscription.amount))}. "
        "Because you selected bank transfer, we will send you a payment reminder and secure payment link before access is renewed."
        if getattr(subscription, "payment_method", "card") == "bank_transfer"
        else f"We'll automatically charge {html.escape(format_naira(subscription.amount))} to your card on file when the trial ends, unless you cancel first."
    )
    message_html = f"<p>Your {html.escape(plan_label)} plan trial has started - you have full access until <strong>{html.escape(trial_ends)}</strong>.</p><p>{payment_message} You can cancel anytime from Settings &rarr; Billing.</p>"
    body_html = _account_wrap_html(heading="Your free trial has started", message_html=message_html)
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject=f"Your {plan_label} plan trial has started", body_text=_account_plain_text("Your free trial has started", message_html), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate trial-started email failed (org=%s)", organization.id)
        return False


def send_trial_expiry_reminder_email(*, organization, subscription) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    web_url = str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").rstrip("/")
    trial_ends = subscription.trial_ends_at.strftime("%d %b %Y") if subscription.trial_ends_at else "soon"
    payment_line = (
        f"Your next payment is {html.escape(format_naira(subscription.amount))}. We will email you a bank-transfer payment link when it is due."
        if getattr(subscription, "payment_method", "card") == "bank_transfer"
        else f"Your saved card will be charged {html.escape(format_naira(subscription.amount))} when the trial ends."
    )
    message_html = f"<p>Your {html.escape(subscription.plan_key.title())} trial ends on <strong>{html.escape(trial_ends)}</strong>.</p><p>{payment_line} Review your billing details before the trial ends.</p>"
    body_html = _account_wrap_html(
        heading="Your Estate trial ends soon",
        message_html=message_html,
        button_html=_account_button_html(label="Review billing", url=f"{web_url}/estates/billing"),
    )
    try:
        _send_email(
            to_email=to_email,
            from_display_name="LandCheck Estates",
            subject="Your LandCheck Estates trial ends soon",
            body_text=_account_plain_text("Your Estate trial ends soon", message_html, f"{web_url}/estates/billing"),
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate trial reminder email failed (org=%s)", organization.id)
        return False


def send_subscription_renewal_reminder_email(*, organization, subscription) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    web_url = str(os.getenv("LANDCHECK_WEB_URL") or "https://landcheck.online").rstrip("/")
    due_at = subscription.next_charge_at.strftime("%d %b %Y") if subscription.next_charge_at else "soon"
    payment_line = (
        f"Because your subscription uses bank transfer, open Billing to complete the payment when the transfer account is issued."
        if getattr(subscription, "payment_method", "card") == "bank_transfer"
        else "Your saved card will be charged automatically."
    )
    message_html = f"<p>Your {html.escape(subscription.plan_key.title())} subscription renews on <strong>{html.escape(due_at)}</strong> for <strong>{html.escape(format_naira(subscription.amount))}</strong>.</p><p>{payment_line} You can review your plan and billing details at any time.</p>"
    body_html = _account_wrap_html(
        heading="Subscription payment reminder",
        message_html=message_html,
        button_html=_account_button_html(label="Open billing", url=f"{web_url}/estates/billing"),
    )
    try:
        _send_email(
            to_email=to_email,
            from_display_name="LandCheck Estates",
            subject="LandCheck Estates subscription payment reminder",
            body_text=_account_plain_text("Subscription payment reminder", message_html, f"{web_url}/estates/billing"),
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate renewal reminder email failed (org=%s)", organization.id)
        return False


def send_subscription_payment_due_email(*, organization, subscription, checkout_url: str | None = None) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    message_html = f"<p>Your {html.escape(subscription.plan_key.title())} subscription payment of <strong>{html.escape(format_naira(subscription.amount))}</strong> is due.</p><p>Complete the bank transfer using the secure payment page below. Your Estate workspace will be restored automatically after Flutterwave confirms the transfer.</p>"
    button_html = _account_button_html(label="Pay subscription by bank transfer", url=checkout_url) if checkout_url else ""
    body_html = _account_wrap_html(heading="Subscription payment due", message_html=message_html, button_html=button_html)
    try:
        _send_email(
            to_email=to_email,
            from_display_name="LandCheck Estates",
            subject="Action needed - Estate subscription payment due",
            body_text=_account_plain_text("Subscription payment due", message_html, checkout_url),
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate subscription payment-due email failed (org=%s)", organization.id)
        return False


def send_payment_receipt_email(*, organization, subscription) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    plan_label = str(ESTATE_PLANS.get(subscription.plan_key, {}).get("label") or "Basic")
    period_end = subscription.current_period_end.strftime("%d %b %Y") if subscription.current_period_end else ""
    message_html = f"<p>We've charged {html.escape(format_naira(subscription.amount))} for your {html.escape(plan_label)} plan ({html.escape(subscription.billing_cycle)}).</p><p>Your subscription is active through <strong>{html.escape(period_end)}</strong>.</p>"
    body_html = _account_wrap_html(heading="Payment received", message_html=message_html)
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject="LandCheck Estates - payment receipt", body_text=_account_plain_text("Payment received", message_html), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate payment receipt email failed (org=%s)", organization.id)
        return False


def send_plan_change_receipt_email(*, organization, subscription, charged_amount) -> bool:
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    plan_label = str(ESTATE_PLANS.get(subscription.plan_key, {}).get("label") or "Basic")
    period_end = subscription.current_period_end.strftime("%d %b %Y") if subscription.current_period_end else ""
    message_html = (
        f"<p>We've charged {html.escape(format_naira(charged_amount))} for your upgrade to the "
        f"{html.escape(plan_label)} plan ({html.escape(subscription.billing_cycle)}).</p>"
        f"<p>Your subscription is active through <strong>{html.escape(period_end)}</strong>. "
        f"Your next renewal will use the {html.escape(plan_label)} plan price.</p>"
    )
    body_html = _account_wrap_html(heading="Plan changed successfully", message_html=message_html)
    try:
        _send_email(
            to_email=to_email,
            from_display_name="LandCheck Estates",
            subject="LandCheck Estates - plan changed",
            body_text=_account_plain_text("Plan changed successfully", message_html),
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate plan-change receipt email failed (org=%s)", organization.id)
        return False


def send_payout_account_changed_email(*, organization, account_name: str, bank_name: str | None, masked_account_number: str, changed_by_display_name: str | None) -> bool:
    """Security alert sent to the org's own contact email every time the buyer-payment payout
    account is created or changed - so an owner finds out immediately if a team member (or an
    attacker) redirected where buyer money settles, even though the change itself is already
    restricted to owner-only (see app/services/estates/authorization.py::require_estate_owner)."""
    to_email = _subscription_org_email(organization)
    if not to_email:
        return False
    actor_line = f" by {html.escape(changed_by_display_name)}" if changed_by_display_name else ""
    message_html = (
        f"<p>Your buyer-payment payout account was just set{actor_line}:</p>"
        f"<p><strong>{html.escape(account_name)}</strong><br/>{html.escape(bank_name or '')} - {html.escape(masked_account_number)}</p>"
        "<p>From now on, buyer online plot payments settle directly into this account. If this wasn't you, "
        "contact support@landcheck.online immediately.</p>"
    )
    body_html = _account_wrap_html(heading="Payout account updated", message_html=message_html)
    try:
        _send_email(to_email=to_email, from_display_name="LandCheck Estates", subject="Security alert - your LandCheck Estates payout account changed", body_text=_account_plain_text("Payout account updated", message_html), body_html=body_html)
        return True
    except Exception:
        logger.exception("Estate payout-account-changed email failed (org=%s)", organization.id)
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
    portal_url: str | None = None,
    payment_due_at=None,
) -> bool:
    """Returns True once the email has actually been sent (not merely queued/attempted) - False if
    there was no address to send to, or delivery failed. Callers use this to tell the person acting
    in the dashboard whether a customer really was notified, rather than assuming it silently."""
    to_email = str(to_email or "").strip()
    if not to_email:
        return False
    try:
        subject, heading, message_html = _event_copy(
            event,
            org_name=org_name,
            estate_name=estate_name,
            plot_number=plot_number,
            customer_name=customer_name,
            amount_just_paid=amount_just_paid,
            payment_due_at=payment_due_at,
        )
        has_financials = agreed_price is not None and confirmed_paid is not None and outstanding is not None
        financial_html = _financial_block_html(agreed_price, confirmed_paid, outstanding) if has_financials else ""
        plot_link = public_plot_url(share_token)
        links_html = (
            _plot_link_html(portal_url, label="Open your buyer portal")
            + _plot_link_html(plot_link)
        )
        body_html = _wrap_html(org_name=org_name, heading=heading, message_html=message_html, financial_html=financial_html, plot_link_html=links_html)
        body_text = _plain_text(
            heading,
            message_html,
            (agreed_price, confirmed_paid, outstanding) if has_financials else None,
            plot_link,
            portal_url,
        )
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


def send_customer_portal_link(
    *,
    to_email: str | None,
    customer_name: str,
    org_name: str,
    portal_url: str,
) -> bool:
    """Send a standalone portal link when staff explicitly creates one."""
    to_email = str(to_email or "").strip()
    if not to_email:
        return False
    first_name = html.escape((customer_name or "there").split(" ")[0])
    message_html = (
        f"<p>Hello {first_name},</p>"
        f"<p>{html.escape(org_name)} has shared your secure buyer portal with you. "
        "Use it to review your plots, payment progress, receipts, documents and next steps.</p>"
    )
    subject = f"Your buyer portal - {org_name}"
    body_html = _wrap_html(
        org_name=org_name,
        heading="Your buyer portal is ready",
        message_html=message_html,
        financial_html="",
        plot_link_html=_plot_link_html(portal_url, label="Open your buyer portal"),
    )
    try:
        _send_email(
            to_email=to_email,
            from_display_name=f"{org_name} (via LandCheck Estates)",
            subject=subject,
            body_text=_plain_text("Your buyer portal is ready", message_html, None, portal_link=portal_url),
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate buyer portal email failed (to=%s)", to_email)
        return False


def send_payment_reminder_email(
    *,
    to_email: str | None,
    customer_name: str,
    org_name: str,
    estate_name: str,
    plot_number: str,
    outstanding,
    due_at,
    method: str,
    portal_url: str | None = None,
    payout_account=None,
) -> bool:
    """A staff-triggered reminder from the Due Payments board - distinct from the automatic
    reminder job's notify_customer(event="payment_reminder"), since this one needs to carry
    either a pay-now link or bank-transfer details, which that fixed-template function has no
    parameters for."""
    to_email = str(to_email or "").strip()
    if not to_email:
        return False
    first_name = html.escape((customer_name or "there").split(" ")[0])
    due_str = due_at.strftime("%d %B %Y") if due_at else "soon"
    message_html = (
        f"<p>Hello {first_name},</p>"
        f"<p>This is a reminder that your next payment of <strong>{html.escape(format_naira(outstanding))}</strong> "
        f"for Plot {html.escape(plot_number)} at {html.escape(estate_name)} is due on <strong>{html.escape(due_str)}</strong>.</p>"
    )
    plot_link_html = ""
    bank_html = ""
    if method in ("online_link", "both") and portal_url:
        plot_link_html = _plot_link_html(portal_url, label="Pay online now")
    if method in ("bank_details", "both") and payout_account is not None:
        bank_html = f"""
        <div style="margin:22px 0 0;padding:16px 18px;background:{_ESTATE_SURFACE_TINT};border:1px solid {_ESTATE_LINE};border-radius:4px;">
          <p style="margin:0 0 8px;color:{_ESTATE_SLATE};font-size:13px;">Pay by bank transfer to:</p>
          <p style="margin:0;color:{_ESTATE_INK};font-weight:700;">{html.escape(payout_account.bank_name or "")}</p>
          <p style="margin:0;color:{_ESTATE_INK};">{html.escape(payout_account.account_name or "")} - {html.escape(payout_account.account_number or "")}</p>
        </div>
        """
    subject = f"Payment reminder - {org_name}"
    body_html = _wrap_html(org_name=org_name, heading="Payment reminder", message_html=message_html + bank_html, financial_html="", plot_link_html=plot_link_html)
    try:
        _send_email(
            to_email=to_email,
            from_display_name=f"{org_name} (via LandCheck Estates)",
            subject=subject,
            body_text=_plain_text("Payment reminder", message_html, None, portal_link=portal_url if method in ("online_link", "both") else None),
            body_html=body_html,
        )
        return True
    except Exception:
        logger.exception("Estate payment reminder email failed (to=%s)", to_email)
        return False
