from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def render_green_report_pdf(
    output_path: str,
    project: dict,
    rows: list[dict],
    map_png: bytes | None = None,
    map_rows: list[dict] | None = None,
    map_view: dict | None = None,
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
    c.drawString(40, height - 220, "Tree Records (latest 200)")

    y = height - 240
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "ID")
    c.drawString(70, y, "Lng")
    c.drawString(140, y, "Lat")
    c.drawString(210, y, "Species")
    c.drawString(320, y, "Status")
    c.drawString(390, y, "Planting Date")
    y -= 12
    c.setFont("Helvetica", 8)

    for r in rows:
        if y < 60:
            c.showPage()
            y = height - 60
            c.setFont("Helvetica-Bold", 9)
            c.drawString(40, y, "ID")
            c.drawString(70, y, "Lng")
            c.drawString(140, y, "Lat")
            c.drawString(210, y, "Species")
            c.drawString(320, y, "Status")
            c.drawString(390, y, "Planting Date")
            y -= 12
            c.setFont("Helvetica", 8)

        c.drawString(40, y, str(r.get("id", "")))
        c.drawString(70, y, f"{r.get('lng', ''):.6f}" if r.get("lng") is not None else "")
        c.drawString(140, y, f"{r.get('lat', ''):.6f}" if r.get("lat") is not None else "")
        c.drawString(210, y, (r.get("species") or "")[:16])
        c.drawString(320, y, str(r.get("status", "")))
        c.drawString(390, y, str(r.get("planting_date", "")))
        y -= 12

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

    c.showPage()
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

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, height - 150, "Assignee Summary")

    y = height - 170
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Assignee")
    c.drawString(160, y, "Orders")
    c.drawString(220, y, "Target")
    c.drawString(280, y, "Planted")
    y -= 12
    c.setFont("Helvetica", 9)

    for r in stats.get("orders", []):
        if y < 60:
            c.showPage()
            y = height - 60
        c.drawString(40, y, str(r.get("assignee_name", "")))
        c.drawString(160, y, str(r.get("orders", 0)))
        c.drawString(220, y, str(r.get("target_trees", 0)))
        c.drawString(280, y, str(r.get("planted_count", 0)))
        y -= 12

    c.showPage()
    c.save()
