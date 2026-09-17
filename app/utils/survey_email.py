from __future__ import annotations

import html
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from app.utils.email_branding import render_branded_email_shell


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def _send_email(*, to_email: str, subject: str, body_text: str, body_html: str, reply_to: str | None = None) -> None:
    smtp_host = str(os.getenv("SMTP_HOST") or "").strip()
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")
    smtp_port = int(str(os.getenv("SMTP_PORT") or "587").strip() or "587")
    smtp_user = str(os.getenv("SMTP_USERNAME") or "").strip()
    smtp_pass = str(os.getenv("SMTP_PASSWORD") or "").strip()
    smtp_from_email = str(os.getenv("SMTP_FROM_EMAIL") or smtp_user or "").strip()
    smtp_from_name = str(os.getenv("SMTP_FROM_NAME") or "LandCheck Survey").strip()
    if not smtp_from_email:
        raise RuntimeError("SMTP_FROM_EMAIL (or SMTP_USERNAME) is not configured")
    use_ssl = _env_bool("SMTP_USE_SSL", False)
    use_tls = _env_bool("SMTP_USE_TLS", not use_ssl)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{smtp_from_name} <{smtp_from_email}>" if smtp_from_name else smtp_from_email
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to
    # Strict inbox providers (Yahoo/Outlook in particular) weigh RFC 5322 hygiene more heavily
    # than Gmail does when deciding whether to deliver vs. silently drop/spam-bin - neither header
    # was being set before, which left it up to whatever the SMTP relay chose to backfill (some
    # do, some don't). Explicit Message-ID (domain-scoped, not the relay's) and Date cost nothing
    # and rule this out as a contributing factor either way.
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


def send_magic_link_email(*, to_email: str, link_url: str, otp_code: str) -> None:
    body = (
        "Hello,\n\n"
        "Click the link below to sign in to LandCheck Survey:\n\n"
        f"{link_url}\n\n"
        f"Or enter this code where you requested it: {otp_code}\n\n"
        "Either one expires in 15 minutes and can only be used once. If you didn't request this, "
        "you can safely ignore this email.\n\n"
        "Regards,\nLandCheck Survey"
    )
    body_html = f"""
    <p style="margin:0 0 20px;font-size:15px;line-height:1.7;color:#173624;">Click the button below to sign in, or enter the code where you requested it.</p>
    <a href="{html.escape(link_url)}" style="display:inline-block;background:linear-gradient(135deg,#1f8c58,#0f6f39);color:#ffffff;text-decoration:none;font-weight:800;font-size:15px;padding:14px 22px;border-radius:999px;box-shadow:0 10px 22px rgba(15,111,57,0.28);">Sign in</a>
    <div style="margin:22px 0 0;padding:16px;background:#f4f9f5;border:1px solid #dceee0;border-radius:12px;text-align:center;">
      <div style="font-size:11.5px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#5c7a68;margin:0 0 8px;">Your sign-in code</div>
      <div style="font-size:28px;font-weight:800;letter-spacing:0.35em;color:#173624;">{html.escape(otp_code)}</div>
    </div>
    <p style="margin:22px 0 0;font-size:12.5px;line-height:1.6;color:#8199a5;">Either one expires in 15 minutes and can only be used once. If you didn't request this, you can safely ignore this email.</p>
    """
    body_html = render_branded_email_shell(
        brand_name="LandCheck Survey",
        kicker="Secure sign in",
        title="Sign in to LandCheck Survey",
        subtitle="Use the secure link or one-time code below to access your workspace.",
        body_html=body_html,
        footer_note="You are receiving this email because a LandCheck Survey sign-in was requested.",
    )

    _send_email(to_email=to_email, subject="Sign in to LandCheck Survey", body_text=body, body_html=body_html)


def send_support_message_email(*, subject: str, message: str, from_email: str, from_name: str, page_context: str) -> None:
    """Notifies the support inbox of a new dashboard support/help request. Best-effort - callers
    should treat a raised exception here as non-fatal (the message is already persisted to the DB
    by the time this is called), same as every other SMTP call in this module."""
    notify_email = str(os.getenv("SUPPORT_NOTIFY_EMAIL") or os.getenv("SMTP_FROM_EMAIL") or "").strip()
    if not notify_email:
        raise RuntimeError("SUPPORT_NOTIFY_EMAIL (or SMTP_FROM_EMAIL) is not configured")

    safe_subject = subject.strip() or "(no subject)"
    body_text = (
        f"New support request from {from_name} <{from_email}>\n"
        f"Page: {page_context or 'dashboard'}\n\n"
        f"Subject: {safe_subject}\n\n"
        f"{message}\n"
    )
    body_html = f"""
    <p style="margin:0 0 6px;font-size:13px;color:#5c7a68;">From: {html.escape(from_name)} &lt;{html.escape(from_email)}&gt;</p>
    <p style="margin:0 0 18px;font-size:13px;color:#5c7a68;">Page: {html.escape(page_context or "dashboard")}</p>
    <div style="padding:16px;background:#f4f9f5;border:1px solid #dceee0;border-radius:12px;font-size:14.5px;line-height:1.7;color:#173624;white-space:pre-wrap;">{html.escape(message)}</div>
    """
    body_html = render_branded_email_shell(
        brand_name="LandCheck Survey",
        kicker="Support request",
        title=html.escape(safe_subject),
        subtitle="A support request has been submitted from the LandCheck Survey workspace.",
        body_html=body_html,
        footer_note="You are receiving this email because a LandCheck Survey support request was submitted.",
    )
    _send_email(
        to_email=notify_email,
        subject=f"[LandCheck Support] {safe_subject}",
        body_text=body_text,
        body_html=body_html,
        reply_to=from_email or None,
    )
