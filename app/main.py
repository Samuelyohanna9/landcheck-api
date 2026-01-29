#main.py
from fastapi import FastAPI
from app.routers import health, plots

app = FastAPI(title="LandCheck API")

app.include_router(health.router)
app.include_router(plots.router)

@app.get("/")
def root():
    return {"status": "ok"}
