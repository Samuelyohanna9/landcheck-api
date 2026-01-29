# pdf.py
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from datetime import datetime
from PIL import Image


def generate_plot_report_pdf(report_data, filepath, map_image_path):

    c = canvas.Canvas(filepath, pagesize=A4)
    page_width, page_height = A4

    # ---------- PAGE 1 : TEXT SUMMARY ----------

    y = page_height - 50

    def line(text):
        nonlocal y
        c.drawString(50, y, text)
        y -= 18

    line("Land Verification Report")
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

    # Load image size
    img = Image.open(map_image_path)
    img_width_px, img_height_px = img.size

    # Desired width on PDF (points)
    target_width = 500

    aspect_ratio = img_height_px / img_width_px
    target_height = target_width * aspect_ratio

    # Center position
    x = (page_width - target_width) / 2
    y = (page_height - target_height) / 2

    c.drawImage(
        map_image_path,
        x,
        y,
        width=target_width,
        height=target_height,
        preserveAspectRatio=True,
        mask='auto'
    )

    c.showPage()
    c.save()
