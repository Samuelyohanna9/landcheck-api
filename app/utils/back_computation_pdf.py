#back_computation_pdf.py
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib.colors import red, black
from datetime import datetime


def render_back_computation_pdf(rows, sum_de, sum_dn, area_m2, plot_id, output_path):

    c = canvas.Canvas(output_path, pagesize=landscape(A4))
    width, height = landscape(A4)

    margin_left = 40
    margin_right = 30
    top_y = height - 40

    # ================= HEADER =================
    c.setFont("Helvetica-Bold", 16)
    c.setFillColor(black)
    c.drawCentredString(width / 2, top_y, "BACK COMPUTATION")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin_left, top_y, f"PLOT {plot_id}")

    # ================= TABLE SETUP =================
    # Changed to single ±ΔE and ±ΔN columns
    headers = [
        "FROM", "TO", "E", "N", "±ΔE", "±ΔN",
        "DIST (m)", "FB (DMS)", "BB (DMS)"
    ]

    # Adjusted column positions for new layout
    col_x = [40, 85, 135, 220, 305, 390, 475, 570, 680]

    row_y = top_y - 60
    row_h = 22

    # ================= AREA (safe position) =================
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(width - margin_right, row_y + 30, f"AREA = {area_m2:,.2f} m²")

    # ================= HEADERS =================
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(black)

    for x, h in zip(col_x, headers):
        c.drawString(x, row_y, h)

    # top horizontal line
    c.setLineWidth(1)
    c.line(margin_left - 5, row_y - 6, width - margin_right, row_y - 6)

    # ================= ROWS =================
    y = row_y - row_h
    c.setFont("Helvetica", 9)

    table_top = row_y + 8
    table_bottom_limit = 60

    def draw_vertical_lines(bottom_y):
        c.setStrokeColor(black)
        c.setLineWidth(0.8)
        for x in col_x:
            c.line(x - 8, table_top, x - 8, bottom_y)
        c.line(width - margin_right, table_top, width - margin_right, bottom_y)

    for r in rows:
        de = r["dE"]
        dn = r["dN"]

        # Single column with ± sign included in value
        values = [
            r["from"], r["to"],
            f"{r['E']:.3f}", f"{r['N']:.3f}",
            f"{de:+.3f}",  # Shows + or - sign
            f"{dn:+.3f}",  # Shows + or - sign
            f"{r['distance']:.3f}",
            str(r["fb"]),
            str(r["bb"]),
        ]

        for i, (x, v) in enumerate(zip(col_x, values)):
            # Color numeric values in red
            if i >= 2 and i <= 6:  # E, N, ±ΔE, ±ΔN, DIST columns
                c.setFillColor(red)
            else:
                c.setFillColor(black)

            c.drawString(x, y, v)

        y -= row_h

        # New page handling
        if y < table_bottom_limit:
            draw_vertical_lines(y + row_h)
            c.showPage()

            # redraw header
            c.setFont("Helvetica-Bold", 16)
            c.drawCentredString(width / 2, top_y, "BACK COMPUTATION")
            c.setFont("Helvetica-Bold", 12)
            c.drawString(margin_left, top_y, f"PLOT {plot_id}")

            c.setFont("Helvetica-Bold", 9)
            for x, h in zip(col_x, headers):
                c.drawString(x, row_y, h)

            c.line(margin_left - 5, row_y - 6, width - margin_right, row_y - 6)

            y = row_y - row_h
            c.setFont("Helvetica", 9)

    # ================= TOTAL ROW =================
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(black)

    c.line(margin_left - 5, y + 8, width - margin_right, y + 8)

    c.drawString(col_x[0], y, "TOTAL")

    # Show totals in the ±ΔE and ±ΔN columns (should be ~0.000 for closed polygon)
    c.setFillColor(red)
    c.drawString(col_x[4], y, f"{sum_de:+.3f}")
    c.drawString(col_x[5], y, f"{sum_dn:+.3f}")

    # bottom line
    c.setFillColor(black)
    c.line(margin_left - 5, y - 6, width - margin_right, y - 6)

    draw_vertical_lines(y - 6)

    # ================= FOOTER =================
    c.setFont("Helvetica", 8)
    c.drawString(
        margin_left,
        30,
        f"Printed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    c.save()
