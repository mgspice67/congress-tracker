"""
dashboard.py – FastAPI application exposing the web UI + JSON API.
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import (
    get_leaderboard,
    get_politician,
    get_politician_trades,
    get_recent_trades,
    get_stats,
    get_trades_by_day,
)

app = FastAPI(title="Congress Stock Tracker", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")


# ── HTML ──────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── JSON API ──────────────────────────────────────────────────────────────────
@app.get("/api/trades")
async def api_trades(
    limit: int = 50,
    offset: int = 0,
    politician: str | None = None,
    ticker: str | None = None,
    trade_type: str | None = None,
):
    trades = await get_recent_trades(
        limit=limit, offset=offset,
        politician=politician, ticker=ticker, trade_type=trade_type,
    )
    return {"trades": trades, "count": len(trades)}


@app.get("/api/leaderboard")
async def api_leaderboard(limit: int = 25):
    return {"leaders": await get_leaderboard(limit)}


@app.get("/api/stats")
async def api_stats():
    return await get_stats()


@app.get("/api/chart/daily")
async def api_chart_daily(days: int = 30):
    return {"data": await get_trades_by_day(days)}


@app.get("/api/politician/{politician_id}")
async def api_politician(politician_id: str):
    profile = await get_politician(politician_id)
    trades  = await get_politician_trades(politician_id, limit=30)
    return {"profile": profile, "trades": trades}


# Health-check (for Render / Railway)
@app.get("/health")
async def health():
    return {"status": "ok"}
