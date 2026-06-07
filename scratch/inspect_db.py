from sqlalchemy import text
from app.db import SessionLocal

db = SessionLocal()
tables = ['tree_projects', 'green_sponsorship_units', 'green_sponsor_accounts']
for table in tables:
    print(f"--- Columns for {table} ---")
    res = db.execute(text(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{table}'")).all()
    for r in res:
        print(f"  {r[0]}: {r[1]}")
db.close()
