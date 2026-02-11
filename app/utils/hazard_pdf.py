import io
from typing import Dict

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth


def render_flood_report_pdf(
    output_path: str,
    overlay_png: bytes,
    summary: Dict[str, float | str],
):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, height - 50, "Flood Risk Report")

    c.setFont("Helvetica", 11)
    c.drawString(40, height - 80, f"Risk Score: {summary['risk_score']}")
    c.drawString(40, height - 100, f"Risk Class: {summary['risk_class']}")
    if summary.get("note"):
        c.setFont("Helvetica", 9)
        _draw_wrapped(c, str(summary.get("note")), 40, height - 115, width - 80, 10)

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 140, "Components")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 160, f"Mean Depth (m): {summary['mean_depth_m']}")
    c.drawString(40, height - 175, f"Max Depth (m): {summary['max_depth_m']}")
    c.drawString(40, height - 190, f"Inundation (%): {summary['inundation_percent']}")
    c.drawString(40, height - 205, f"Distance to River (m): {summary.get('distance_to_river_m', 'N/A')}")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 220, "Method")
    c.setFont("Helvetica", 9)
    method = str(summary.get("method", ""))
    _draw_wrapped(c, method, 40, height - 238, width - 240, 10)
    rp = summary.get("return_period", "100")
    c.drawString(40, height - 282, f"Return period: {rp} years.")
    c.drawString(40, height - 296, f"Analysis buffer: {summary.get('buffer_m', '1000')} m around plot.")
    c.drawString(40, height - 310, "Map is for screening only, not a legal flood determination.")

    # Legend box
    legend = summary.get("legend", [])
    legend_x = width - 180
    legend_y = height - 250
    c.setFont("Helvetica-Bold", 10)
    c.drawString(legend_x, legend_y, "Legend")
    y = legend_y - 14
    for item in legend:
        try:
            color = colors.HexColor(item["color"])
        except Exception:
            color = colors.black
        c.setFillColor(color)
        c.rect(legend_x, y - 8, 10, 10, fill=1, stroke=0)
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 9)
        c.drawString(legend_x + 14, y - 6, str(item.get("label", "")))
        y -= 14

    img = ImageReader(io.BytesIO(overlay_png))
    img_width = width - 80
    img_height = height - 390
    img_x = 40
    img_y = 40
    c.drawImage(img, img_x, img_y, img_width, img_height, preserveAspectRatio=True, anchor="sw")

    # Simple north arrow (map overlay reference)
    arrow_x = img_x + img_width - 24
    arrow_y = img_y + img_height - 24
    c.setFillColor(colors.black)
    c.setStrokeColor(colors.black)
    path = c.beginPath()
    path.moveTo(arrow_x, arrow_y)
    path.lineTo(arrow_x - 6, arrow_y - 12)
    path.lineTo(arrow_x + 6, arrow_y - 12)
    path.close()
    c.drawPath(path, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(arrow_x, arrow_y - 24, "N")

    c.showPage()
    c.save()


def _draw_wrapped(c: canvas.Canvas, text: str, x: float, y: float, max_width: float, line_height: float):
    words = text.replace("\n", " ").split()
    line = ""
    cursor_y = y
    for word in words:
        test_line = f"{line} {word}".strip()
        if stringWidth(test_line, c._fontname, c._fontsize) <= max_width:
            line = test_line
        else:
            c.drawString(x, cursor_y, line)
            cursor_y -= line_height
            line = word
    if line:
        c.drawString(x, cursor_y, line)
