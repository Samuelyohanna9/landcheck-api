# pdf.py
from reportlab.lib.pagesizes import A4, A3, A2, A1, A0
from reportlab.pdfgen import canvas

# Paper size mapping
PAPER_SIZES = {
    "A4": A4,
    "A3": A3,
    "A2": A2,
    "A1": A1,
    "A0": A0,
}


def generate_plot_report_pdf(report_data, filepath, map_image_path, paper_size="A4"):
    """Generate the survey plan PDF - just the rendered map page, no leading text-summary page."""

    # Get page size (default to A4 if invalid)
    page_size = PAPER_SIZES.get(paper_size.upper(), A4)

    c = canvas.Canvas(filepath, pagesize=page_size)
    page_width, page_height = page_size

    # Draw the rendered survey page image full-page (same approach used by orthophoto export)
    # so the page frame occupies the sheet without extra outer margins.
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
