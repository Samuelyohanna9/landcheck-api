from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime
import os
import tempfile
import csv

from app.db import SessionLocal
from app.utils.green_pdf import render_green_report_pdf, render_green_work_report_pdf

router = APIRouter(prefix="/green", tags=["green"])

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports", "green")


def get_db():
    db = SessionLocal()
    try:
        ensure_green_tables(db)
        yield db
    finally:
        db.close()


def ensure_green_tables(db: Session):
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS tree_projects (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            location_text TEXT,
            sponsor TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS trees (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            geom GEOMETRY(POINT, 4326),
            species TEXT,
            planting_date DATE,
            status TEXT NOT NULL DEFAULT 'alive',
            notes TEXT,
            photo_url TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS tree_visits (
            id SERIAL PRIMARY KEY,
            tree_id INTEGER NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
            visit_date DATE NOT NULL,
            status TEXT NOT NULL,
            notes TEXT,
            photo_url TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_users (
            id SERIAL PRIMARY KEY,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'field_officer',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS tree_tasks (
            id SERIAL PRIMARY KEY,
            tree_id INTEGER NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            assignee_name TEXT NOT NULL,
            due_date DATE,
            priority TEXT DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'pending',
            notes TEXT,
            photo_url TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP
        )
    """))
    try:
        db.execute(text("ALTER TABLE tree_tasks ADD COLUMN IF NOT EXISTS priority TEXT DEFAULT 'normal'"))
    except Exception:
        db.rollback()
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS green_work_orders (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES tree_projects(id) ON DELETE CASCADE,
            assignee_name TEXT NOT NULL,
            work_type TEXT NOT NULL,
            target_trees INTEGER DEFAULT 0,
            maintenance_schedule TEXT,
            due_date DATE,
            status TEXT NOT NULL DEFAULT 'assigned',
            planted_count INTEGER DEFAULT 0,
            visits_done INTEGER DEFAULT 0,
            last_update TIMESTAMP DEFAULT NOW(),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_trees_project_id ON trees(project_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_trees_geom ON trees USING GIST (geom)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_tree_visits_tree_id ON tree_visits(tree_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_tree_tasks_tree_id ON tree_tasks(tree_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_green_users_name ON green_users(full_name)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_work_orders_project_id ON green_work_orders(project_id)"))
    db.commit()


def _load_env_token() -> str | None:
    token = os.environ.get("MAPBOX_TOKEN") or os.environ.get("VITE_MAPBOX_TOKEN")
    if token:
        return token.strip().strip('"').strip("'")
    env_path = os.path.join(BASE_DIR, "..", ".env")
    if not os.path.exists(env_path):
        return None
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("#") or "=" not in line:
                    continue
                key, val = line.strip().split("=", 1)
                if key in {"MAPBOX_TOKEN", "VITE_MAPBOX_TOKEN"}:
                    return val.strip().strip('"').strip("'")
    except Exception:
        return None
    return None


@router.post("/projects")
def create_project(
    db: Session = Depends(get_db),
    name: str = Body(...),
    location_text: str = Body(default=""),
    sponsor: str = Body(default=""),
):
    row = db.execute(
        text("""
            INSERT INTO tree_projects (name, location_text, sponsor)
            VALUES (:name, :location_text, :sponsor)
            RETURNING id, name, location_text, sponsor, created_at
        """),
        {"name": name, "location_text": location_text, "sponsor": sponsor},
    ).mappings().first()
    db.commit()
    return dict(row)


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, name, location_text, sponsor, created_at
        FROM tree_projects
        ORDER BY created_at DESC
    """)).mappings().all()
    return [dict(r) for r in rows]


@router.get("/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = db.execute(text("""
        SELECT id, name, location_text, sponsor, created_at
        FROM tree_projects
        WHERE id = :project_id
    """), {"project_id": project_id}).mappings().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    stats = db.execute(text("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'alive' THEN 1 ELSE 0 END) AS alive,
            SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END) AS dead,
            SUM(CASE WHEN status = 'needs_attention' THEN 1 ELSE 0 END) AS needs_attention
        FROM trees
        WHERE project_id = :project_id
    """), {"project_id": project_id}).mappings().first()

    total = stats["total"] or 0
    alive = stats["alive"] or 0
    dead = stats["dead"] or 0
    needs_attention = stats["needs_attention"] or 0
    survival_rate = round((alive / total) * 100, 1) if total else 0.0

    return {
        **dict(project),
        "stats": {
            "total": total,
            "alive": alive,
            "dead": dead,
            "needs_attention": needs_attention,
            "survival_rate": survival_rate,
        },
    }


@router.get("/projects/{project_id}/trees")
def list_trees(project_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, project_id, species, planting_date, status, notes, photo_url, created_by, created_at,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM trees
        WHERE project_id = :project_id
        ORDER BY created_at DESC
    """), {"project_id": project_id}).mappings().all()
    return [dict(r) for r in rows]


@router.post("/trees")
def add_tree(
    db: Session = Depends(get_db),
    project_id: int = Body(...),
    lng: float = Body(...),
    lat: float = Body(...),
    species: str = Body(default=""),
    planting_date: str | None = Body(default=None),
    status: str = Body(default="alive"),
    notes: str = Body(default=""),
    photo_url: str = Body(default=""),
    created_by: str = Body(default=""),
):
    if status not in {"alive", "dead", "needs_attention", "pending_planting"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    row = db.execute(text("""
        INSERT INTO trees (project_id, geom, species, planting_date, status, notes, photo_url, created_by)
        VALUES (
            :project_id,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
            :species,
            :planting_date,
            :status,
            :notes,
            :photo_url,
            :created_by
        )
        RETURNING id
    """), {
        "project_id": project_id,
        "lng": lng,
        "lat": lat,
        "species": species or None,
        "planting_date": planting_date,
        "status": status,
        "notes": notes or None,
        "photo_url": photo_url or None,
        "created_by": created_by or None,
    }).scalar()
    db.commit()
    return {"id": row}


@router.patch("/trees/{tree_id}")
def update_tree(
    tree_id: int,
    db: Session = Depends(get_db),
    species: str | None = Body(default=None),
    planting_date: str | None = Body(default=None),
    status: str | None = Body(default=None),
    notes: str | None = Body(default=None),
    photo_url: str | None = Body(default=None),
):
    if status and status not in {"alive", "dead", "needs_attention", "pending_planting"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    db.execute(text("""
        UPDATE trees
        SET species = COALESCE(:species, species),
            planting_date = COALESCE(:planting_date, planting_date),
            status = COALESCE(:status, status),
            notes = COALESCE(:notes, notes),
            photo_url = COALESCE(:photo_url, photo_url)
        WHERE id = :tree_id
    """), {
        "species": species,
        "planting_date": planting_date,
        "status": status,
        "notes": notes,
        "photo_url": photo_url,
        "tree_id": tree_id,
    })
    db.commit()
    return {"status": "ok"}


@router.post("/trees/{tree_id}/visits")
def add_visit(
    tree_id: int,
    db: Session = Depends(get_db),
    visit_date: str = Body(...),
    status: str = Body(...),
    notes: str = Body(default=""),
    photo_url: str = Body(default=""),
    created_by: str = Body(default=""),
):
    if status not in {"alive", "dead", "needs_attention", "pending_planting"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    db.execute(text("""
        INSERT INTO tree_visits (tree_id, visit_date, status, notes, photo_url, created_by)
        VALUES (:tree_id, :visit_date, :status, :notes, :photo_url, :created_by)
    """), {
        "tree_id": tree_id,
        "visit_date": visit_date,
        "status": status,
        "notes": notes or None,
        "photo_url": photo_url or None,
        "created_by": created_by or None,
    })
    db.commit()
    return {"status": "ok"}


@router.post("/trees/{tree_id}/tasks")
def add_task(
    tree_id: int,
    db: Session = Depends(get_db),
    task_type: str = Body(...),
    assignee_name: str = Body(...),
    due_date: str | None = Body(default=None),
    priority: str = Body(default="normal"),
    status: str = Body(default="pending"),
    notes: str = Body(default=""),
    photo_url: str = Body(default=""),
):
    if status not in {"pending", "done", "overdue"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    row = db.execute(text("""
        INSERT INTO tree_tasks (tree_id, task_type, assignee_name, due_date, priority, status, notes, photo_url)
        VALUES (:tree_id, :task_type, :assignee_name, :due_date, :priority, :status, :notes, :photo_url)
        RETURNING id
    """), {
        "tree_id": tree_id,
        "task_type": task_type,
        "assignee_name": assignee_name,
        "due_date": due_date,
        "priority": priority,
        "status": status,
        "notes": notes or None,
        "photo_url": photo_url or None,
    }).scalar()
    db.commit()
    return {"id": row}


@router.get("/trees/{tree_id}/tasks")
def list_tree_tasks(tree_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, tree_id, task_type, assignee_name, due_date, priority,
               status, notes, photo_url, created_at, completed_at
        FROM tree_tasks
        WHERE tree_id = :tree_id
        ORDER BY created_at DESC
    """), {"tree_id": tree_id}).mappings().all()
    return [dict(r) for r in rows]


@router.get("/tasks")
def list_tasks(
    project_id: int,
    assignee_name: str | None = None,
    db: Session = Depends(get_db),
):
    rows = db.execute(text("""
        SELECT t.id, t.tree_id, t.task_type, t.assignee_name, t.due_date, t.priority,
               t.status, t.notes, t.photo_url, t.created_at, t.completed_at,
               tr.status AS tree_status, ST_X(tr.geom) AS lng, ST_Y(tr.geom) AS lat
        FROM tree_tasks t
        JOIN trees tr ON tr.id = t.tree_id
        WHERE tr.project_id = :project_id
          AND (:assignee_name IS NULL OR t.assignee_name = :assignee_name)
        ORDER BY t.created_at DESC
    """), {"project_id": project_id, "assignee_name": assignee_name}).mappings().all()
    return [dict(r) for r in rows]


@router.post("/users")
def create_user(
    db: Session = Depends(get_db),
    full_name: str = Body(...),
    role: str = Body(default="field_officer"),
):
    row = db.execute(text("""
        INSERT INTO green_users (full_name, role)
        VALUES (:full_name, :role)
        RETURNING id, full_name, role, created_at
    """), {"full_name": full_name, "role": role}).mappings().first()
    db.commit()
    return dict(row)


@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, full_name, role, created_at
        FROM green_users
        ORDER BY created_at DESC
    """)).mappings().all()
    return [dict(r) for r in rows]


@router.patch("/tasks/{task_id}")
def update_task(
    task_id: int,
    db: Session = Depends(get_db),
    status: str | None = Body(default=None),
    notes: str | None = Body(default=None),
    photo_url: str | None = Body(default=None),
):
    if status and status not in {"pending", "done", "overdue"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    db.execute(text("""
        UPDATE tree_tasks
        SET status = COALESCE(:status, status),
            notes = COALESCE(:notes, notes),
            photo_url = COALESCE(:photo_url, photo_url),
            completed_at = CASE WHEN :status = 'done' THEN NOW() ELSE completed_at END
        WHERE id = :task_id
    """), {"status": status, "notes": notes, "photo_url": photo_url, "task_id": task_id})
    db.commit()
    return {"status": "ok"}


@router.get("/trees/{tree_id}/timeline")
def tree_timeline(tree_id: int, db: Session = Depends(get_db)):
    tree = db.execute(text("""
        SELECT id, species, planting_date, status, created_at
        FROM trees
        WHERE id = :tree_id
    """), {"tree_id": tree_id}).mappings().first()
    tasks = list_tree_tasks(tree_id, db)
    visits = db.execute(text("""
        SELECT visit_date, status, notes, photo_url, created_by, created_at
        FROM tree_visits
        WHERE tree_id = :tree_id
        ORDER BY visit_date DESC
    """), {"tree_id": tree_id}).mappings().all()
    return {
        "tree": dict(tree) if tree else None,
        "tasks": tasks,
        "visits": [dict(v) for v in visits],
    }


@router.get("/projects/{project_id}/task-stats")
def task_stats(project_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'overdue' THEN 1 ELSE 0 END) AS overdue
        FROM tree_tasks
        WHERE tree_id IN (SELECT id FROM trees WHERE project_id = :project_id)
    """), {"project_id": project_id}).mappings().first()
    return dict(rows)


@router.post("/work-orders")
def create_work_order(
    db: Session = Depends(get_db),
    project_id: int = Body(...),
    assignee_name: str = Body(...),
    work_type: str = Body(...),
    target_trees: int = Body(default=0),
    maintenance_schedule: str = Body(default=""),
    due_date: str | None = Body(default=None),
):
    if work_type not in {"planting", "maintenance"}:
        raise HTTPException(status_code=400, detail="Invalid work_type")
    row = db.execute(text("""
        INSERT INTO green_work_orders (
            project_id, assignee_name, work_type, target_trees, maintenance_schedule, due_date
        )
        VALUES (:project_id, :assignee_name, :work_type, :target_trees, :maintenance_schedule, :due_date)
        RETURNING id
    """), {
        "project_id": project_id,
        "assignee_name": assignee_name,
        "work_type": work_type,
        "target_trees": target_trees,
        "maintenance_schedule": maintenance_schedule or None,
        "due_date": due_date,
    }).scalar()
    db.commit()
    return {"id": row}


@router.get("/work-orders")
def list_work_orders(project_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, project_id, assignee_name, work_type, target_trees,
               maintenance_schedule, due_date, status, planted_count, visits_done,
               last_update, created_at
        FROM green_work_orders
        WHERE project_id = :project_id
        ORDER BY created_at DESC
    """), {"project_id": project_id}).mappings().all()
    return [dict(r) for r in rows]


@router.patch("/work-orders/{work_id}")
def update_work_order(
    work_id: int,
    db: Session = Depends(get_db),
    status: str | None = Body(default=None),
    planted_count: int | None = Body(default=None),
    visits_done: int | None = Body(default=None),
):
    db.execute(text("""
        UPDATE green_work_orders
        SET status = COALESCE(:status, status),
            planted_count = COALESCE(:planted_count, planted_count),
            visits_done = COALESCE(:visits_done, visits_done),
            last_update = NOW()
        WHERE id = :work_id
    """), {
        "status": status,
        "planted_count": planted_count,
        "visits_done": visits_done,
        "work_id": work_id,
    })
    db.commit()
    return {"status": "ok"}


@router.get("/work-stats")
def work_stats(project_id: int, db: Session = Depends(get_db)):
    orders = db.execute(text("""
        SELECT assignee_name,
               COUNT(*) AS orders,
               SUM(target_trees) AS target_trees,
               SUM(planted_count) AS planted_count,
               SUM(visits_done) AS visits_done
        FROM green_work_orders
        WHERE project_id = :project_id
        GROUP BY assignee_name
    """), {"project_id": project_id}).mappings().all()

    tree_counts = db.execute(text("""
        SELECT created_by AS assignee_name, COUNT(*) AS trees_logged
        FROM trees
        WHERE project_id = :project_id
        GROUP BY created_by
    """), {"project_id": project_id}).mappings().all()

    visit_counts = db.execute(text("""
        SELECT created_by AS assignee_name, COUNT(*) AS visits_logged
        FROM tree_visits
        WHERE tree_id IN (SELECT id FROM trees WHERE project_id = :project_id)
        GROUP BY created_by
    """), {"project_id": project_id}).mappings().all()

    return {
        "orders": [dict(r) for r in orders],
        "trees_by_user": [dict(r) for r in tree_counts],
        "visits_by_user": [dict(r) for r in visit_counts],
    }


@router.get("/work-stats/export/csv")
def export_work_stats_csv(project_id: int, db: Session = Depends(get_db)):
    stats = work_stats(project_id, db)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_csv = tempfile.NamedTemporaryFile(suffix="_work_stats.csv", delete=False)
    csv_path = tmp_csv.name
    tmp_csv.close()

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["assignee", "orders", "target_trees", "planted_count", "visits_done"])
        for r in stats["orders"]:
            writer.writerow([
                r.get("assignee_name", ""),
                r.get("orders", 0),
                r.get("target_trees", 0),
                r.get("planted_count", 0),
                r.get("visits_done", 0),
            ])

    filename = f"project_{project_id}_work_stats.csv"
    return FileResponse(csv_path, media_type="text/csv", filename=filename)


@router.get("/work-stats/export/pdf")
def export_work_stats_pdf(project_id: int, db: Session = Depends(get_db)):
    project = get_project(project_id, db)
    stats = work_stats(project_id, db)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_work_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()
    render_green_work_report_pdf(pdf_path, project, stats)
    filename = f"project_{project_id}_work_report.pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


@router.get("/projects/{project_id}/tasks/export/csv")
def export_tasks_csv(project_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT t.id, t.task_type, t.assignee_name, t.due_date, t.priority, t.status,
               t.notes, t.photo_url, t.created_at, t.completed_at
        FROM tree_tasks t
        JOIN trees tr ON tr.id = t.tree_id
        WHERE tr.project_id = :project_id
        ORDER BY t.created_at DESC
    """), {"project_id": project_id}).mappings().all()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_csv = tempfile.NamedTemporaryFile(suffix="_tasks.csv", delete=False)
    csv_path = tmp_csv.name
    tmp_csv.close()

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "task_id", "task_type", "assignee_name", "due_date", "priority", "status",
            "notes", "photo_url", "created_at", "completed_at"
        ])
        for r in rows:
            writer.writerow([
                r["id"], r["task_type"], r["assignee_name"], r["due_date"], r["priority"], r["status"],
                r["notes"], r["photo_url"], r["created_at"], r["completed_at"],
            ])

    filename = f"project_{project_id}_tasks.csv"
    return FileResponse(csv_path, media_type="text/csv", filename=filename)


@router.get("/projects/{project_id}/tasks/export/pdf")
def export_tasks_pdf(project_id: int, db: Session = Depends(get_db)):
    project = get_project(project_id, db)
    stats = task_stats(project_id, db)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_tasks_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()
    render_green_work_report_pdf(pdf_path, project, {"orders": [], **stats})
    filename = f"project_{project_id}_tasks_report.pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


@router.get("/projects/{project_id}/export/csv")
def export_project_csv(project_id: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, project_id, species, planting_date, status, notes, photo_url, created_by, created_at,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM trees
        WHERE project_id = :project_id
        ORDER BY created_at DESC
    """), {"project_id": project_id}).mappings().all()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_csv = tempfile.NamedTemporaryFile(suffix="_trees.csv", delete=False)
    csv_path = tmp_csv.name
    tmp_csv.close()

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "tree_id", "project_id", "lng", "lat", "species", "planting_date",
            "status", "notes", "photo_url", "created_by", "created_at"
        ])
        for r in rows:
            writer.writerow([
                r["id"], r["project_id"], r["lng"], r["lat"], r["species"],
                r["planting_date"], r["status"], r["notes"], r["photo_url"],
                r["created_by"], r["created_at"],
            ])

    filename = f"project_{project_id}_trees.csv"
    return FileResponse(csv_path, media_type="text/csv", filename=filename)


@router.get("/projects/{project_id}/export/pdf")
def export_project_pdf(
    project_id: int,
    lng: float | None = Query(default=None),
    lat: float | None = Query(default=None),
    zoom: float | None = Query(default=None),
    bearing: float | None = Query(default=0.0),
    pitch: float | None = Query(default=0.0),
    db: Session = Depends(get_db),
):
    project = get_project(project_id, db)
    rows = db.execute(text("""
        SELECT id, species, planting_date, status, notes,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM trees
        WHERE project_id = :project_id
        ORDER BY created_at DESC
        LIMIT 200
    """), {"project_id": project_id}).mappings().all()
    map_rows = db.execute(text("""
        SELECT id, species, planting_date, status, notes,
               ST_X(geom) AS lng, ST_Y(geom) AS lat
        FROM trees
        WHERE project_id = :project_id
        ORDER BY created_at DESC
        LIMIT 1000
    """), {"project_id": project_id}).mappings().all()

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_project_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    map_png = None
    token = _load_env_token()
    if token and map_rows:
        lats = [r["lat"] for r in map_rows if r["lat"] is not None]
        lngs = [r["lng"] for r in map_rows if r["lng"] is not None]
        if lats and lngs:
            center_lat = lat if lat is not None else sum(lats) / len(lats)
            center_lng = lng if lng is not None else sum(lngs) / len(lngs)
            z = zoom if zoom is not None else 13
            b = bearing or 0
            p = pitch or 0
            status_colors = {
                "alive": "22c55e",
                "needs_attention": "f59e0b",
                "dead": "ef4444",
                "pending_planting": "3b82f6",
            }
            import urllib.parse
            markers = []
            for r in map_rows[:120]:
                if r["lng"] is None or r["lat"] is None:
                    continue
                color = status_colors.get(str(r.get("status", "")).lower(), "22c55e")
                markers.append(f"pin-s+{color}({r['lng']},{r['lat']})")
            overlay = ",".join(markers) if markers else None
            overlay_part = f"{urllib.parse.quote(overlay, safe='(),:+')}/" if overlay else ""
            url = (
                "https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/static/"
                f"{overlay_part}{center_lng},{center_lat},{z},{b},{p}/800x500@2x?access_token={token}"
            )
            try:
                import requests
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    map_png = resp.content
            except Exception:
                map_png = None

    if map_png is None and map_rows:
        # Fallback to OpenStreetMap static image
        try:
            import requests
            import urllib.parse
            lats = [r["lat"] for r in map_rows if r["lat"] is not None]
            lngs = [r["lng"] for r in map_rows if r["lng"] is not None]
            if lats and lngs:
                center_lat = lat if lat is not None else sum(lats) / len(lats)
                center_lng = lng if lng is not None else sum(lngs) / len(lngs)
                z = int(round(zoom)) if zoom is not None else 13
                markers = "|".join([f"{r['lat']},{r['lng']},lightgreen1" for r in map_rows[:50]])
                marker_qs = urllib.parse.quote(markers, safe="|,")
                osm_url = (
                    "https://staticmap.openstreetmap.de/staticmap.php?"
                    f"center={center_lat},{center_lng}&zoom={z}&size=800x500&markers={marker_qs}"
                )
                resp = requests.get(osm_url, timeout=15)
                if resp.status_code == 200:
                    map_png = resp.content
        except Exception:
            map_png = None

    map_view = None
    if lng is not None and lat is not None and zoom is not None:
        map_view = {"lng": lng, "lat": lat, "zoom": zoom}
    render_green_report_pdf(pdf_path, project, rows, map_png=map_png, map_rows=map_rows, map_view=map_view)
    filename = f"project_{project_id}_report.pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


def _build_tree_stats(rows: list[dict]) -> dict:
    total = len(rows)
    alive = sum(1 for r in rows if r.get("status") == "alive")
    dead = sum(1 for r in rows if r.get("status") == "dead")
    needs_attention = sum(1 for r in rows if r.get("status") == "needs_attention")
    pending = sum(1 for r in rows if r.get("status") == "pending_planting")
    survival_rate = round((alive / total) * 100, 1) if total else 0.0
    return {
        "total": total,
        "alive": alive,
        "dead": dead,
        "needs_attention": needs_attention,
        "pending_planting": pending,
        "survival_rate": survival_rate,
    }


@router.get("/work-report/pdf")
def export_work_report_pdf(
    project_id: int,
    assignee_name: str | None = None,
    lng: float | None = Query(default=None),
    lat: float | None = Query(default=None),
    zoom: float | None = Query(default=None),
    bearing: float | None = Query(default=0.0),
    pitch: float | None = Query(default=0.0),
    db: Session = Depends(get_db),
):
    project = get_project(project_id, db)
    if assignee_name:
        rows = db.execute(text("""
            SELECT id, species, planting_date, status, notes,
                   ST_X(geom) AS lng, ST_Y(geom) AS lat
            FROM trees
            WHERE project_id = :project_id AND created_by = :assignee_name
            ORDER BY created_at DESC
            LIMIT 200
        """), {"project_id": project_id, "assignee_name": assignee_name}).mappings().all()
        map_rows = db.execute(text("""
            SELECT id, species, planting_date, status, notes,
                   ST_X(geom) AS lng, ST_Y(geom) AS lat
            FROM trees
            WHERE project_id = :project_id AND created_by = :assignee_name
            ORDER BY created_at DESC
            LIMIT 1000
        """), {"project_id": project_id, "assignee_name": assignee_name}).mappings().all()
    else:
        rows = db.execute(text("""
            SELECT id, species, planting_date, status, notes,
                   ST_X(geom) AS lng, ST_Y(geom) AS lat
            FROM trees
            WHERE project_id = :project_id
            ORDER BY created_at DESC
            LIMIT 200
        """), {"project_id": project_id}).mappings().all()
        map_rows = db.execute(text("""
            SELECT id, species, planting_date, status, notes,
                   ST_X(geom) AS lng, ST_Y(geom) AS lat
            FROM trees
            WHERE project_id = :project_id
            ORDER BY created_at DESC
            LIMIT 1000
        """), {"project_id": project_id}).mappings().all()

    project_copy = dict(project)
    project_copy["stats"] = _build_tree_stats(map_rows)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_work_map_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    map_png = None
    token = _load_env_token()
    if token and map_rows:
        lats = [r["lat"] for r in map_rows if r["lat"] is not None]
        lngs = [r["lng"] for r in map_rows if r["lng"] is not None]
        if lats and lngs:
            center_lat = lat if lat is not None else sum(lats) / len(lats)
            center_lng = lng if lng is not None else sum(lngs) / len(lngs)
            z = zoom if zoom is not None else 13
            b = bearing or 0
            p = pitch or 0
            status_colors = {
                "alive": "22c55e",
                "needs_attention": "f59e0b",
                "dead": "ef4444",
                "pending_planting": "3b82f6",
            }
            import urllib.parse
            markers = []
            for r in map_rows[:120]:
                if r["lng"] is None or r["lat"] is None:
                    continue
                color = status_colors.get(str(r.get("status", "")).lower(), "22c55e")
                markers.append(f"pin-s+{color}({r['lng']},{r['lat']})")
            overlay = ",".join(markers) if markers else None
            overlay_part = f"{urllib.parse.quote(overlay, safe='(),:+')}/" if overlay else ""
            url = (
                "https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/static/"
                f"{overlay_part}{center_lng},{center_lat},{z},{b},{p}/800x500@2x?access_token={token}"
            )
            try:
                import requests
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    map_png = resp.content
            except Exception:
                map_png = None

    if map_png is None and map_rows:
        try:
            import requests
            import urllib.parse
            lats = [r["lat"] for r in map_rows if r["lat"] is not None]
            lngs = [r["lng"] for r in map_rows if r["lng"] is not None]
            if lats and lngs:
                center_lat = lat if lat is not None else sum(lats) / len(lats)
                center_lng = lng if lng is not None else sum(lngs) / len(lngs)
                z = int(round(zoom)) if zoom is not None else 13
                markers = "|".join([f"{r['lat']},{r['lng']},lightgreen1" for r in map_rows[:50]])
                marker_qs = urllib.parse.quote(markers, safe="|,")
                osm_url = (
                    "https://staticmap.openstreetmap.de/staticmap.php?"
                    f"center={center_lat},{center_lng}&zoom={z}&size=800x500&markers={marker_qs}"
                )
                resp = requests.get(osm_url, timeout=15)
                if resp.status_code == 200:
                    map_png = resp.content
        except Exception:
            map_png = None

    map_view = None
    if lng is not None and lat is not None and zoom is not None:
        map_view = {"lng": lng, "lat": lat, "zoom": zoom}
    render_green_report_pdf(pdf_path, project_copy, rows, map_png=map_png, map_rows=map_rows, map_view=map_view)
    filename = (
        f"project_{project_id}_work_report_{assignee_name}.pdf"
        if assignee_name
        else f"project_{project_id}_work_report_all.pdf"
    )
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)
