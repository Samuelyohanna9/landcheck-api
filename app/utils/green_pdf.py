from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def render_green_report_pdf(
    output_path: str,
    project: dict,
    rows: list[dict],
    map_png: bytes | None = None,
    map_rows: list[dict] | None = None,
    map_view: dict | None = None,
    maintenance_rows: list[dict] | None = None,
):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, height - 50, "LandCheck Green Report")

    c.setFont("Helvetica", 11)
    c.drawString(40, height - 80, f"Project: {project.get('name', '')}")
    c.drawString(40, height - 98, f"Location: {project.get('location_text', '')}")
    c.drawString(40, height - 116, f"Sponsor: {project.get('sponsor', '')}")
    c.drawString(40, height - 134, f"Created: {project.get('created_at', '')}")

    stats = project.get("stats", {})
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 170, "Summary")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 188, f"Total: {stats.get('total', 0)}")
    c.drawString(120, height - 188, f"Alive: {stats.get('alive', 0)}")
    c.drawString(200, height - 188, f"Dead: {stats.get('dead', 0)}")
    c.drawString(280, height - 188, f"Needs Attention: {stats.get('needs_attention', 0)}")
    c.drawString(420, height - 188, f"Survival: {stats.get('survival_rate', 0)}%")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 220, "Tree Records + Maintenance (latest 200)")

    def draw_tree_header(y_pos: float):
        c.setFont("Helvetica-Bold", 8)
        c.drawString(40, y_pos, "Tree ID")
        c.drawString(84, y_pos, "Species")
        c.drawString(160, y_pos, "Status")
        c.drawString(222, y_pos, "Planting Date")
        c.drawString(300, y_pos, "Maint #")
        c.drawString(344, y_pos, "Maint Type(s)")
        c.drawString(474, y_pos, "Last Maint Date")

    y = height - 240
    draw_tree_header(y)
    y -= 12
    c.setFont("Helvetica", 7.5)

    for r in rows:
        if y < 60:
            c.showPage()
            y = height - 60
            draw_tree_header(y)
            y -= 12
            c.setFont("Helvetica", 7.5)

        c.drawString(40, y, str(r.get("id", "")))
        c.drawString(84, y, (str(r.get("species", "")) or "-")[:16])
        c.drawString(160, y, (str(r.get("status", "")) or "-")[:13])
        c.drawString(222, y, str(r.get("planting_date", "") or "-")[:14])
        c.drawString(300, y, str(r.get("maintenance_count", 0) or 0))
        c.drawString(344, y, (str(r.get("maintenance_types", "")) or "-")[:30])
        c.drawString(474, y, str(r.get("last_maintenance_date", "") or "-")[:16])
        y -= 11

    # Always render a map page so reports include a visual snapshot.
    c.showPage()
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 50, "Tree Map Snapshot")

    # Legend (compact row)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, height - 72, "Legend")
    c.setFont("Helvetica", 9)
    legend_items = [
        ("Alive", (0.13, 0.77, 0.37)),
        ("Needs Attention", (0.96, 0.62, 0.04)),
        ("Dead", (0.94, 0.27, 0.27)),
        ("Pending Planting", (0.23, 0.51, 0.96)),
    ]
    legend_x = 90
    legend_y = height - 74
    for label, color in legend_items:
        c.setFillColorRGB(*color)
        c.rect(legend_x, legend_y - 6, 10, 10, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(legend_x + 14, legend_y - 4, label)
        legend_x += 95

    map_x = 40
    map_top = height - 92
    map_y = 60
    map_w = width - 80
    map_h = max(map_top - map_y, 200)
    c.setStrokeColorRGB(0, 0, 0)

    map_rows = map_rows or rows

    img_x = map_x
    img_y = map_y
    img_w = map_w
    img_h = map_h
    if map_png:
        from reportlab.lib.utils import ImageReader
        import io
        img = ImageReader(io.BytesIO(map_png))
        src_w, src_h = img.getSize()
        if src_w and src_h:
            scale = min(map_w / src_w, map_h / src_h)
            img_w = src_w * scale
            img_h = src_h * scale
            img_x = map_x
            img_y = map_y + (map_h - img_h)
        c.drawImage(img, img_x, img_y, img_w, img_h, preserveAspectRatio=True, anchor="sw")

    # Draw border around the actual map image area
    c.setStrokeColorRGB(0, 0, 0)
    c.rect(img_x, img_y, img_w, img_h, stroke=1, fill=0)

    # Overlay points only when no static map with markers is available.
    lats = [r.get("lat") for r in map_rows if r.get("lat") is not None]
    lngs = [r.get("lng") for r in map_rows if r.get("lng") is not None]
    if not map_png and lats and lngs:
        status_colors = {
            "alive": (0.13, 0.77, 0.37),
            "needs_attention": (0.96, 0.62, 0.04),
            "dead": (0.94, 0.27, 0.27),
            "pending_planting": (0.23, 0.51, 0.96),
        }

        def mercator_px(lng: float, lat: float, world_size: float):
            import math
            x = (lng + 180.0) / 360.0 * world_size
            siny = math.sin(math.radians(lat))
            siny = min(max(siny, -0.9999), 0.9999)
            y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * world_size
            return x, y

        if map_png and map_view and map_view.get("zoom") is not None:
            try:
                import math
                zoom = float(map_view.get("zoom"))
                center_lng = float(map_view.get("lng"))
                center_lat = float(map_view.get("lat"))
                world_size = 512 * (2 ** zoom)
                cx, cy = mercator_px(center_lng, center_lat, world_size)
                src_w, src_h = img.getSize()
                scale = img_w / src_w if src_w else 1
                for r in map_rows:
                    if r.get("lat") is None or r.get("lng") is None:
                        continue
                    px_w, py_w = mercator_px(float(r["lng"]), float(r["lat"]), world_size)
                    dx = px_w - cx
                    dy = py_w - cy
                    x_px = (src_w / 2) + dx
                    y_px = (src_h / 2) + dy
                    px = img_x + x_px * scale
                    py = img_y + (src_h - y_px) * scale
                    color = status_colors.get(str(r.get("status", "")).lower(), (0.13, 0.77, 0.37))
                    c.setFillColorRGB(*color)
                    c.setStrokeColorRGB(0, 0, 0)
                    c.circle(px, py, 3.4, stroke=1, fill=1)
                # Done with accurate overlay; skip fallback mapping
                lats = []
            except Exception:
                pass

        if lats and lngs:
            min_lat, max_lat = min(lats), max(lats)
            min_lng, max_lng = min(lngs), max(lngs)
            lat_span = max(max_lat - min_lat, 1e-6)
            lng_span = max(max_lng - min_lng, 1e-6)
            pad_lat = lat_span * 0.05
            pad_lng = lng_span * 0.05
            min_lat -= pad_lat
            max_lat += pad_lat
            min_lng -= pad_lng
            max_lng += pad_lng
            lat_span = max(max_lat - min_lat, 1e-6)
            lng_span = max(max_lng - min_lng, 1e-6)

            for r in map_rows:
                if r.get("lat") is None or r.get("lng") is None:
                    continue
                px = img_x + ((r["lng"] - min_lng) / lng_span) * img_w
                py = img_y + ((r["lat"] - min_lat) / lat_span) * img_h
                color = status_colors.get(str(r.get("status", "")).lower(), (0.13, 0.77, 0.37))
                c.setFillColorRGB(*color)
                c.setStrokeColorRGB(0, 0, 0)
                c.circle(px, py, 3.4, stroke=1, fill=1)

    if maintenance_rows:
        c.showPage()
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, height - 50, "Maintenance Summary By Tree")
        c.setFont("Helvetica", 9)
        c.drawString(40, height - 68, "Columns: maintenance type, last date, and number of times.")

        y = height - 92
        c.setFont("Helvetica-Bold", 8)
        c.drawString(40, y, "Tree ID")
        c.drawString(92, y, "Times")
        c.drawString(134, y, "Done")
        c.drawString(172, y, "Pending")
        c.drawString(226, y, "Overdue")
        c.drawString(282, y, "Last Type")
        c.drawString(376, y, "Type(s)")
        c.drawString(494, y, "Last Date")
        y -= 12

        c.setFont("Helvetica", 7.5)
        for row in maintenance_rows:
            if y < 52:
                c.showPage()
                y = height - 60
                c.setFont("Helvetica-Bold", 8)
                c.drawString(40, y, "Tree ID")
                c.drawString(92, y, "Times")
                c.drawString(134, y, "Done")
                c.drawString(172, y, "Pending")
                c.drawString(226, y, "Overdue")
                c.drawString(282, y, "Last Type")
                c.drawString(376, y, "Type(s)")
                c.drawString(494, y, "Last Date")
                y -= 12
                c.setFont("Helvetica", 7.5)

            c.drawString(40, y, str(row.get("tree_id", "")))
            c.drawString(92, y, str(row.get("maintenance_count", 0) or 0))
            c.drawString(134, y, str(row.get("maintenance_done", 0) or 0))
            c.drawString(172, y, str(row.get("maintenance_pending", 0) or 0))
            c.drawString(226, y, str(row.get("maintenance_overdue", 0) or 0))
            c.drawString(282, y, (str(row.get("last_maintenance_type", "")) or "-")[:16])
            c.drawString(376, y, (str(row.get("maintenance_types", "")) or "-")[:24])
            c.drawString(494, y, str(row.get("last_maintenance_date", "") or "-")[:16])
            y -= 11

    c.save()


def render_green_work_report_pdf(output_path: str, project: dict, stats: dict):
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, height - 50, "LandCheck Work Report")

    c.setFont("Helvetica", 11)
    c.drawString(40, height - 80, f"Project: {project.get('name', '')}")
    c.drawString(40, height - 98, f"Location: {project.get('location_text', '')}")
    c.drawString(40, height - 116, f"Sponsor: {project.get('sponsor', '')}")

    maintenance_by_assignee = {
        str(r.get("assignee_name", "")): dict(r)
        for r in stats.get("maintenance_by_assignee", [])
    }
    assignee_rows = [dict(r) for r in stats.get("orders", [])]

    # Include assignees who only have maintenance data (no planting order rows).
    seen_names = {str(r.get("assignee_name", "")) for r in assignee_rows}
    for assignee_name, maintenance_row in maintenance_by_assignee.items():
        if assignee_name in seen_names:
            continue
        assignee_rows.append({
            "assignee_name": assignee_name,
            "orders": 0,
            "target_trees": 0,
            "planted_count": 0,
            "maintenance_total": maintenance_row.get("maintenance_total", 0),
            "maintenance_done": maintenance_row.get("maintenance_done", 0),
            "maintenance_pending": maintenance_row.get("maintenance_pending", 0),
            "maintenance_overdue": maintenance_row.get("maintenance_overdue", 0),
            "maintenance_types": maintenance_row.get("maintenance_types", ""),
            "last_maintenance_date": maintenance_row.get("last_maintenance_date"),
        })

    for row in assignee_rows:
        extra = maintenance_by_assignee.get(str(row.get("assignee_name", "")), {})
        row["maintenance_total"] = int(extra.get("maintenance_total", row.get("maintenance_total", 0)) or 0)
        row["maintenance_done"] = int(extra.get("maintenance_done", row.get("maintenance_done", 0)) or 0)
        row["maintenance_pending"] = int(extra.get("maintenance_pending", row.get("maintenance_pending", 0)) or 0)
        row["maintenance_overdue"] = int(extra.get("maintenance_overdue", row.get("maintenance_overdue", 0)) or 0)
        row["maintenance_types"] = extra.get("maintenance_types", row.get("maintenance_types", "")) or ""
        row["last_maintenance_date"] = extra.get("last_maintenance_date", row.get("last_maintenance_date"))

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 150, "Assignee Summary + Maintenance")

    y = height - 170
    c.setFont("Helvetica-Bold", 8)
    c.drawString(40, y, "Assignee")
    c.drawString(130, y, "Orders")
    c.drawString(168, y, "Target")
    c.drawString(206, y, "Planted")
    c.drawString(252, y, "Maint #")
    c.drawString(296, y, "Done/Pend/Over")
    c.drawString(396, y, "Type(s)")
    c.drawString(502, y, "Last Date")
    y -= 12
    c.setFont("Helvetica", 7.3)

    for r in assignee_rows:
        if y < 60:
            c.showPage()
            y = height - 60
            c.setFont("Helvetica-Bold", 8)
            c.drawString(40, y, "Assignee")
            c.drawString(130, y, "Orders")
            c.drawString(168, y, "Target")
            c.drawString(206, y, "Planted")
            c.drawString(252, y, "Maint #")
            c.drawString(296, y, "Done/Pend/Over")
            c.drawString(396, y, "Type(s)")
            c.drawString(502, y, "Last Date")
            y -= 12
            c.setFont("Helvetica", 7.3)

        c.drawString(40, y, str(r.get("assignee_name", ""))[:15])
        c.drawString(130, y, str(r.get("orders", 0)))
        c.drawString(168, y, str(r.get("target_trees", 0)))
        c.drawString(206, y, str(r.get("planted_count", 0)))
        c.drawString(252, y, str(r.get("maintenance_total", 0)))
        c.drawString(
            296,
            y,
            f"{r.get('maintenance_done', 0)}/{r.get('maintenance_pending', 0)}/{r.get('maintenance_overdue', 0)}",
        )
        c.drawString(396, y, str(r.get("maintenance_types", "") or "-")[:23])
        c.drawString(502, y, str(r.get("last_maintenance_date", "") or "-")[:12])
        y -= 11

    maintenance_by_type = stats.get("maintenance_by_type", [])
    if maintenance_by_type:
        c.showPage()
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, height - 50, "Maintenance Type Activity")
        c.setFont("Helvetica", 9)
        c.drawString(40, height - 68, "Columns: type, date, and number of times by assignee.")

        y = height - 90
        c.setFont("Helvetica-Bold", 8)
        c.drawString(40, y, "Assignee")
        c.drawString(170, y, "Maintenance Type")
        c.drawString(330, y, "Number of Times")
        c.drawString(450, y, "Last Date")
        y -= 12
        c.setFont("Helvetica", 8)

        for row in maintenance_by_type:
            if y < 55:
                c.showPage()
                y = height - 60
                c.setFont("Helvetica-Bold", 8)
                c.drawString(40, y, "Assignee")
                c.drawString(170, y, "Maintenance Type")
                c.drawString(330, y, "Number of Times")
                c.drawString(450, y, "Last Date")
                y -= 12
                c.setFont("Helvetica", 8)

            c.drawString(40, y, str(row.get("assignee_name", ""))[:22])
            c.drawString(170, y, str(row.get("task_type", ""))[:24])
            c.drawString(330, y, str(row.get("maintenance_times", 0)))
            c.drawString(450, y, str(row.get("last_maintenance_date", "") or "-")[:16])
            y -= 11

    c.save()
