"""Shared HTML email shell used by LandCheck transactional messages.

Email clients have limited CSS support, so the visual system intentionally uses inline styles and
system font fallbacks instead of relying on external stylesheets or web fonts.
"""

from __future__ import annotations


_DEFAULT_HEADER_GRADIENT = "linear-gradient(145deg,#0c5f2e 0%,#1d8a49 55%,#2aa852 100%)"
_DEFAULT_FOOTER_HTML = """
<div style="font-size:12.5px;color:#7c9186;line-height:1.7;">
  Powered by <strong style="color:#1f8c58;">LandCheck</strong> Geospatial Technologies Limited<br/>
  <a href="mailto:landchecktech@gmail.com" style="color:#1f8c58;text-decoration:none;">landchecktech@gmail.com</a>
  &nbsp;&middot;&nbsp;
  <a href="https://landcheck.online" style="color:#1f8c58;text-decoration:none;">landcheck.online</a>
</div>
<div style="margin-top:10px;font-size:11.5px;color:#a9bdb0;">
  <a href="https://www.instagram.com/land.check/" style="color:#a9bdb0;text-decoration:none;">Instagram</a> &middot;
  <a href="https://www.facebook.com/landcheck/" style="color:#a9bdb0;text-decoration:none;">Facebook</a> &middot;
  <a href="https://www.youtube.com/@LandCheckGreen" style="color:#a9bdb0;text-decoration:none;">YouTube</a> &middot;
  <a href="https://www.tiktok.com/@landcheckgeo" style="color:#a9bdb0;text-decoration:none;">TikTok</a> &middot;
  <a href="https://www.linkedin.com/company/landcheck-geospatial/" style="color:#a9bdb0;text-decoration:none;">LinkedIn</a>
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
    header_gradient: str = _DEFAULT_HEADER_GRADIENT,
) -> str:
    """Render the shared green email shell.

    Header values are HTML fragments so callers can escape dynamic values once before passing
    them in. Body content is also intentionally accepted as an HTML fragment because each email
    needs its own tables, buttons, and detail blocks.
    """
    return f"""
    <html>
      <body style="margin:0;padding:0;background:#eef4f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#173624;">
        <div style="max-width:640px;margin:0 auto;padding:32px 16px;">
          <div style="background:#ffffff;border-radius:22px;overflow:hidden;box-shadow:0 18px 46px rgba(14,46,28,0.14);border:1px solid #dceee0;">
            <div style="padding:30px 32px 28px;background:{header_gradient};">
              <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  <td style="vertical-align:middle;">
                    <img src="https://landcheck.online/landcheck-email-logo.png" width="36" height="36" alt="LandCheck"
                         style="display:block;border-radius:9px;background:#ffffff;padding:3px;" />
                  </td>
                  <td style="vertical-align:middle;padding-left:10px;">
                    <span style="font-size:13px;font-weight:800;letter-spacing:0.04em;color:#ffffff;">{brand_name}</span>
                  </td>
                </tr>
              </table>
              <div style="margin-top:20px;font-size:11.5px;font-weight:800;letter-spacing:0.16em;text-transform:uppercase;color:#d6f5df;">{kicker}</div>
              <div style="margin-top:8px;font-size:25px;font-weight:800;line-height:1.2;color:#ffffff;">{title}</div>
              <div style="margin-top:10px;font-size:14.5px;line-height:1.7;color:#e9fbee;">{subtitle}</div>
            </div>
            <div style="padding:28px 32px 8px;">
              {body_html}
            </div>
            <div style="padding:20px 32px 26px;border-top:1px solid #ecf4ee;margin-top:16px;">
              {footer_html or _DEFAULT_FOOTER_HTML}
            </div>
          </div>
          <p style="text-align:center;font-size:11px;color:#9fb2a6;margin:18px 0 0;">{footer_note}</p>
        </div>
      </body>
    </html>
    """
