"""Shared HTML email shell used by LandCheck transactional messages.

Email clients have limited CSS support, so the visual system intentionally uses inline styles and
system font fallbacks instead of relying on external stylesheets or web fonts.
"""

from __future__ import annotations


_DEFAULT_FOOTER_HTML = """
<div style="font-size:12.5px;color:#6b7685;line-height:1.7;">
  Powered by <strong style="color:#0f6e44;">LandCheck</strong> Geospatial Technologies Limited<br/>
  <a href="mailto:admin@landcheck.online" style="color:#0f6e44;text-decoration:none;">admin@landcheck.online</a>
  &nbsp;&middot;&nbsp;
  <a href="https://landcheck.online" style="color:#0f6e44;text-decoration:none;">landcheck.online</a>
</div>
<div style="margin-top:10px;font-size:11.5px;color:#98a2b0;">
  <a href="https://www.instagram.com/land.check/" style="color:#98a2b0;text-decoration:none;">Instagram</a> &middot;
  <a href="https://www.facebook.com/landcheck/" style="color:#98a2b0;text-decoration:none;">Facebook</a> &middot;
  <a href="https://www.youtube.com/@LandCheckGreen" style="color:#98a2b0;text-decoration:none;">YouTube</a> &middot;
  <a href="https://www.tiktok.com/@landcheckgeo" style="color:#98a2b0;text-decoration:none;">TikTok</a> &middot;
  <a href="https://www.linkedin.com/company/landcheck-geospatial/" style="color:#98a2b0;text-decoration:none;">LinkedIn</a>
</div>
"""


def render_branded_email_shell(
    *,
    brand_name: str,
    kicker: str,
    title: str,
    subtitle: str,
    body_html: str,
    footer_html: str | None = None,
    footer_note: str = "You are receiving this email because of an action taken on LandCheck.",
) -> str:
    """Render the shared Estate-style email shell.

    Header values are HTML fragments so callers can escape dynamic values once before passing
    them in. Body content is also intentionally accepted as an HTML fragment because each email
    needs its own detail blocks. The shell uses inline styles for broad email-client compatibility
    and avoids remote images so a blocked asset cannot create a broken header mark.
    """
    return f"""
    <html>
      <body style="margin:0;padding:0;background:#f3f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#101827;">
        <div style="max-width:680px;margin:0 auto;padding:24px 12px;">
          <div style="height:4px;background:#1a8f5a;font-size:0;line-height:0;">&nbsp;</div>
          <div style="background:#ffffff;border:1px solid #e4e8ec;border-top:0;border-radius:0 0 18px 18px;overflow:hidden;box-shadow:0 12px 32px rgba(16,24,39,0.08);">
            <div style="padding:24px 32px 22px;border-bottom:1px solid #edf0f3;">
              <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  <td style="width:42px;height:42px;vertical-align:middle;border-radius:10px;background:#0f6e44;color:#ffffff;text-align:center;font-size:16px;font-weight:850;letter-spacing:-0.04em;">
                    LC
                  </td>
                  <td style="vertical-align:middle;padding-left:12px;">
                    <span style="font-size:14px;font-weight:800;letter-spacing:-0.01em;color:#101827;">{brand_name}</span>
                  </td>
                </tr>
              </table>
              <div style="margin-top:24px;font-size:11.5px;font-weight:800;letter-spacing:0.14em;text-transform:uppercase;color:#1a8f5a;">{kicker}</div>
              <div style="margin-top:8px;font-size:26px;font-weight:800;letter-spacing:-0.04em;line-height:1.18;color:#101827;">{title}</div>
              <div style="margin-top:10px;font-size:14.5px;line-height:1.7;color:#6b7685;">{subtitle}</div>
            </div>
            <div style="padding:28px 32px 12px;">
              {body_html}
            </div>
            <div style="padding:20px 32px 26px;border-top:1px solid #edf0f3;margin-top:16px;">
              {footer_html or _DEFAULT_FOOTER_HTML}
            </div>
          </div>
          <p style="text-align:center;font-size:11px;line-height:1.5;color:#98a2b0;margin:16px 0 0;">{footer_note}</p>
        </div>
      </body>
    </html>
    """
