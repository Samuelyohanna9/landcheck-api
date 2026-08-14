from __future__ import annotations

import html
import os
import smtplib
from email.message import EmailMessage


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def send_magic_link_email(*, to_email: str, link_url: str, otp_code: str) -> None:
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
    <html>
      <body style="margin:0;padding:0;background:#eef4f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#173624;">
        <div style="max-width:520px;margin:0 auto;padding:32px 16px;">
          <div style="background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 18px 46px rgba(14,46,28,0.14);border:1px solid #dceee0;padding:28px;">
            <div style="font-size:12.5px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:#5c7a68;margin:0 0 10px;">Sign in</div>
            <h1 style="margin:0 0 14px;font-size:20px;color:#173624;">Sign in to LandCheck Survey</h1>
            <p style="margin:0 0 20px;font-size:14.5px;line-height:1.7;color:#345542;">Click the button below to sign in, or enter the code where you requested it.</p>
            <a href="{html.escape(link_url)}" style="display:inline-block;background:#1d8a49;color:#ffffff;text-decoration:none;font-weight:700;font-size:14.5px;padding:12px 22px;border-radius:10px;">Sign in</a>
            <div style="margin:22px 0 0;padding:16px;background:#f4f9f5;border:1px solid #dceee0;border-radius:12px;text-align:center;">
              <div style="font-size:11.5px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#5c7a68;margin:0 0 8px;">Your sign-in code</div>
              <div style="font-size:28px;font-weight:800;letter-spacing:0.35em;color:#173624;">{html.escape(otp_code)}</div>
            </div>
            <p style="margin:22px 0 0;font-size:12.5px;line-height:1.6;color:#8199a5;">Either one expires in 15 minutes and can only be used once. If you didn't request this, you can safely ignore this email.</p>
          </div>
        </div>
      </body>
    </html>
    """

    msg = EmailMessage()
    msg["Subject"] = "Sign in to LandCheck Survey"
    msg["From"] = f"{smtp_from_name} <{smtp_from_email}>" if smtp_from_name else smtp_from_email
    msg["To"] = to_email
    msg.set_content(body)
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
