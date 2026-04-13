"""
main.py – Application entry point.

Startup sequence:
  1. Initialise the SQLite database
  2. Run first poll immediately (so the dashboard isn't empty)
  3. Send Telegram startup message
  4. Launch APScheduler:
     - poll every POLL_INTERVAL_MINUTES (fetch + store, no notifications)
     - daily_notify at NOTIFY_HOUR:NOTIFY_MINUTE (send Telegram batch)
  5. Serve the FastAPI dashboard on PORT
"""
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from config import MOCK_DATA, NOTIFY_HOUR, NOTIFY_MINUTE, POLL_INTERVAL_MINUTES, PORT
from database import init_db
from notifier import send_startup_message
from scheduler import create_scheduler, poll_trades
from dashboard import app as dashboard_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_scheduler = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _scheduler

    if MOCK_DATA:
        logger.info("MOCK_DATA=true – using synthetic trades (no real API calls)")

    # 1. DB
    await init_db()
    logger.info("Database ready")

    # 2. First poll (data only, no notifications)
    logger.info("Running initial poll…")
    await poll_trades()

    # 3. Telegram
    await send_startup_message()

    # 4. Scheduler
    _scheduler = create_scheduler()
    _scheduler.start()
    logger.info(
        "Scheduler started – polling every %d min, notifications daily at %02d:%02d",
        POLL_INTERVAL_MINUTES, NOTIFY_HOUR, NOTIFY_MINUTE,
    )

    yield

    # Cleanup
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


# Attach the lifespan to the dashboard FastAPI app
dashboard_app.router.lifespan_context = _lifespan


if __name__ == "__main__":
    uvicorn.run(
        "main:dashboard_app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
