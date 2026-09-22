from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from app.banner.router import router as banner_router
from app.db.database import init_db, db
from app.queue import redis_conn
from app.routers import jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ec-imagegen", version="0.2.0", lifespan=lifespan)
app.include_router(jobs.router)
app.include_router(banner_router)

@app.get("/health")
def health():
    db_ok = db.check_connection()
    try:
        redis_conn.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    status_code = 200 if db_ok and redis_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if db_ok and redis_ok else "degraded", "db": db_ok, "redis": redis_ok},
    )