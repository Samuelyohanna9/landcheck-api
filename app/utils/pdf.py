# pdf.py
import glob
import os

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
    """Generate the survey plan PDF, appending deferred boundary schedules when present."""

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

    # Dense boundaries are rendered as companion schedule images by the map renderer. Keeping
    # them on subsequent sheets preserves every bearing/distance without covering the plan.
    schedule_paths = sorted(
        glob.glob(f"{map_image_path}.boundary-schedule-*.png"),
        key=lambda path: int(path.rsplit("-", 1)[-1].rsplit(".", 1)[0]),
    )
    for schedule_path in schedule_paths:
        c.drawImage(
            schedule_path,
            0,
            0,
            width=page_width,
            height=page_height,
            preserveAspectRatio=False,
            mask='auto',
        )
        c.showPage()
    c.save()

    for schedule_path in schedule_paths:
        try:
            os.remove(schedule_path)
        except OSError:
            pass
