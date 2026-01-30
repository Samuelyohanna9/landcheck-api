# app/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import health, plots, analytics, feedback
from app.db_init import init_db

app = FastAPI(title="LandCheck API")

# ✅ Create tables on startup
@app.on_event("startup")
def startup_event():
    init_db()

# CORS (relaxed for MVP)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(health.router)
app.include_router(plots.router)
app.include_router(analytics.router)
app.include_router(feedback.router)

@app.get("/")
def root():
    return {"status": "ok"}
