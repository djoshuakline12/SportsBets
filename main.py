import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from models.database import Base, engine
from routes.api import router as api_router
from services.betting_scheduler import run_betting_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="SportsBets API",
    description="Automated sports betting prediction and execution engine",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

scheduler = AsyncIOScheduler()


@app.on_event("startup")
async def startup():
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created")

    scheduler.add_job(
        run_betting_cycle,
        "interval",
        minutes=settings.odds_refresh_minutes,
        id="betting_cycle",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Betting scheduler started (every {settings.odds_refresh_minutes} min)")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
    logger.info("Shutdown complete")


@app.get("/")
def root():
    return {
        "name": "SportsBets API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "ok"}
