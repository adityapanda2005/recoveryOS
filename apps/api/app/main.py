"""
RecoverOS API entrypoint.

This is intentionally minimal for Phase 1 — just health/readiness so the
whole stack (docker-compose: db + migrations + seed + api) can be proven
to boot end-to-end. Domain routes are added in Phase 2.
"""
from fastapi import FastAPI
from sqlalchemy import text

from app.db.session import SessionLocal

app = FastAPI(title="RecoverOS API", version="0.1.0")


@app.get("/health")
def health():
    """Liveness: is the process up."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness: can we actually reach the database."""
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        return {"status": "not_ready", "database": "unreachable", "error": str(e)}
