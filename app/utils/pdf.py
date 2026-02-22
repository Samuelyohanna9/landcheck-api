# pdf.py
from reportlab.lib.pagesizes import A4, A3, A2, A1, A0
from reportlab.pdfgen import canvas
from datetime import datetime
from PIL import Image

# Paper size mapping
PAPER_SIZES = {
    "A4": A4,
    "A3": A3,
    "A2": A2,
    "A1": A1,
    "A0": A0,
}


def generate_plot_report_pdf(report_data, filepath, map_image_path, paper_size="A4"):
    """Generate PDF report with specified paper size"""

    # Get page size (default to A4 if invalid)
    page_size = PAPER_SIZES.get(paper_size.upper(), A4)

    c = canvas.Canvas(filepath, pagesize=page_size)
    page_width, page_height = page_size

    # Scale font size based on paper size
    font_scale = 1.0
    if paper_size.upper() == "A3":
        font_scale = 1.2
    elif paper_size.upper() == "A2":
        font_scale = 1.4
    elif paper_size.upper() == "A1":
        font_scale = 1.6
    elif paper_size.upper() == "A0":
        font_scale = 1.8

    base_font_size = int(12 * font_scale)
    margin = int(50 * font_scale)
    line_height = int(18 * font_scale)

    # ---------- PAGE 1 : TEXT SUMMARY ----------

    y = page_height - margin

    def line(text, font_size=base_font_size):
        nonlocal y
        c.setFontSize(font_size)
        c.drawString(margin, y, text)
        y -= line_height

    c.setFont("Helvetica-Bold", int(16 * font_scale))
    line("Land Verification Report", int(16 * font_scale))
    c.setFont("Helvetica", base_font_size)
    line("=" * 50)
    line(f"Plot ID: {report_data['plot_id']}")
    line(f"Generated: {datetime.utcnow()} UTC")
    line("")
    line(f"Area (sqm): {report_data['area_m2']}")
    line("")

    line("Features INSIDE plot:")
    if report_data["features"]["inside"]:
        for k, v in report_data["features"]["inside"].items():
            line(f"  - {k}: {v}")
    else:
        line("  None")

    line("")
    line("Features within 50m buffer:")
    if report_data["features"]["buffer"]:
        for k, v in report_data["features"]["buffer"].items():
            line(f"  - {k}: {v}")
    else:
        line("  None")

    c.showPage()

    # ---------- PAGE 2 : MAP ----------

    # Draw the rendered survey page image full-page (same approach used by orthophoto export)
    # so the page frame occupies the sheet without extra outer margins.
    img = Image.open(map_image_path)
    img_width_px, img_height_px = img.size
    c.drawImage(
        map_image_path,
        0,
        0,
        width=page_width,
        height=page_height,
        preserveAspectRatio=False,
        mask='auto'
    )

    c.showPage()
    c.save()
