from __future__ import annotations

import html
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

# Survey's own email palette/typography - mirrors landcheck-web's SurveyPlanLanding.tsx
# (survey-plan-landing.css's .spl-page scope) rather than the generic LandCheck shell in
# app/utils/email_branding.py. Deliberately not shared with that util (Estate and Green each have
# their own near-identical pattern in their own files too) so each product's emails can diverge
# from the others without touching code the other products depend on.
_SURVEY_PAPER = "#f5f3eb"
_SURVEY_INK = "#102a27"
_SURVEY_MUTED = "#61736e"
_SURVEY_ACCENT = "#1f8c58"
_SURVEY_LINE = "rgba(16,42,39,0.18)"
_SURVEY_LINE_SOFT = "rgba(16,42,39,0.09)"
_SURVEY_FAINT = "#8d9a96"
_SURVEY_SERIF = "Georgia,'Times New Roman','Noto Serif',serif"
_SURVEY_SANS = "Arial,'Segoe UI',Helvetica,sans-serif"


def _survey_email_shell(*, kicker: str, title: str, subtitle: str, body_html: str, footer_note: str) -> str:
    return f"""
    <html>
      <body style="margin:0;padding:0;background:{_SURVEY_PAPER};font-family:{_SURVEY_SANS};color:{_SURVEY_INK};">
        <div style="max-width:600px;margin:0 auto;padding:32px 16px;">
          <div style="background:#ffffff;border:1px solid {_SURVEY_LINE};border-radius:4px;overflow:hidden;">
            <div style="height:3px;background:{_SURVEY_ACCENT};font-size:0;line-height:0;">&nbsp;</div>
            <div style="padding:30px 34px 24px;border-bottom:1px solid {_SURVEY_LINE_SOFT};">
              <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  <td style="width:36px;height:36px;vertical-align:middle;border-radius:4px;background:{_SURVEY_INK};color:#ffffff;text-align:center;font:800 13px {_SURVEY_SANS};letter-spacing:-0.02em;">LS</td>
                  <td style="vertical-align:middle;padding-left:11px;">
                    <span style="font:800 13px {_SURVEY_SANS};letter-spacing:-0.01em;color:{_SURVEY_INK};">LandCheck Survey</span>
                  </td>
                </tr>
              </table>
              <div style="margin-top:22px;font:800 11px {_SURVEY_SANS};letter-spacing:0.16em;text-transform:uppercase;color:{_SURVEY_ACCENT};">{kicker}</div>
              <div style="margin-top:9px;font-family:{_SURVEY_SERIF};font-size:24px;font-weight:500;letter-spacing:-0.02em;line-height:1.24;color:{_SURVEY_INK};">{title}</div>
              <div style="margin-top:10px;font:400 14px/1.7 {_SURVEY_SANS};color:{_SURVEY_MUTED};">{subtitle}</div>
            </div>
            <div style="padding:26px 34px 10px;font:400 14.5px/1.7 {_SURVEY_SANS};color:{_SURVEY_INK};">
              {body_html}
            </div>
            <div style="padding:22px 34px 28px;border-top:1px solid {_SURVEY_LINE_SOFT};margin-top:14px;font:400 12.5px/1.75 {_SURVEY_SANS};color:{_SURVEY_MUTED};">
              <div style="font-family:{_SURVEY_SERIF};font-size:14px;font-weight:600;color:{_SURVEY_INK};">LandCheck Survey</div>
              <div style="margin-top:10px;">
                <a href="mailto:admin@landcheck.online" style="color:{_SURVEY_ACCENT};text-decoration:none;">admin@landcheck.online</a>
                &nbsp;&middot;&nbsp;
                <a href="https://landcheck.online/survey" style="color:{_SURVEY_ACCENT};text-decoration:none;">landcheck.online/survey</a>
              </div>
              <div style="margin-top:14px;padding-top:12px;border-top:1px solid {_SURVEY_LINE_SOFT};font-size:11px;color:{_SURVEY_FAINT};">LandCheck Geospatial Technologies Limited</div>
            </div>
          </div>
          <p style="text-align:center;font:400 11px/1.6 {_SURVEY_SANS};color:{_SURVEY_FAINT};margin:18px 0 0;">{footer_note}</p>
        </div>
      </body>
    </html>
    """


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
    <p style="margin:0 0 20px;font-size:15px;line-height:1.7;color:{_SURVEY_INK};">Click the button below to sign in, or enter the code where you requested it.</p>
    <a href="{html.escape(link_url)}" style="display:inline-block;background:{_SURVEY_ACCENT};color:#ffffff;text-decoration:none;font-weight:700;font-size:14px;padding:13px 22px;border-radius:2px;">Sign in</a>
    <div style="margin:22px 0 0;padding:16px;background:#faf8f1;border:1px solid {_SURVEY_LINE};border-radius:4px;text-align:center;">
      <div style="font-size:11.5px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:{_SURVEY_MUTED};margin:0 0 8px;">Your sign-in code</div>
      <div style="font-size:28px;font-weight:800;letter-spacing:0.35em;color:{_SURVEY_INK};">{html.escape(otp_code)}</div>
    </div>
    <p style="margin:22px 0 0;font-size:12.5px;line-height:1.6;color:{_SURVEY_FAINT};">Either one expires in 15 minutes and can only be used once. If you didn't request this, you can safely ignore this email.</p>
    """
    body_html = _survey_email_shell(
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
    <p style="margin:0 0 6px;font-size:13px;color:{_SURVEY_MUTED};">From: {html.escape(from_name)} &lt;{html.escape(from_email)}&gt;</p>
    <p style="margin:0 0 18px;font-size:13px;color:{_SURVEY_MUTED};">Page: {html.escape(page_context or "dashboard")}</p>
    <div style="padding:16px;background:#faf8f1;border:1px solid {_SURVEY_LINE};border-radius:4px;font-size:14.5px;line-height:1.7;color:{_SURVEY_INK};white-space:pre-wrap;">{html.escape(message)}</div>
    """
    body_html = _survey_email_shell(
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
