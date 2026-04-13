"""
scheduler.py – Real-time trade notifications.

  poll_trades() runs every POLL_INTERVAL_MINUTES:
    - Fetches & stores all new trades
    - Immediately notifies pertinent ones (insider score >= MIN_INSIDER_SCORE)
    - Silently marks the rest as notified (no accumulation of noise)
"""
import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import POLL_INTERVAL_MINUTES
from database import insert_trade, mark_notified
from fetcher import fetch_all_trades
from notifier import send_trade_notification
from performance import compute_trade_filing_performance
from insider_score import compute_insider_score
from enricher import enrich_trade

logger = logging.getLogger(__name__)

# Only notify trades with this insider score or above.
# 20 = at least one meaningful signal (large amount, conflict, notable performance…)
MIN_INSIDER_SCORE = 20

# Max notifications per poll to avoid flooding during recess-end dumps
MAX_PER_POLL = 10


async def poll_trades():
    """
    Fetch new trades, score them, and notify pertinent ones immediately.
    Trades below MIN_INSIDER_SCORE are silently marked as notified.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    logger.info("[%s] Polling for new trades…", ts)

    trades = await fetch_all_trades()

    new_trades = []
    for trade in trades:
        is_new = await insert_trade(trade)
        if is_new:
            new_trades.append(trade)
            logger.info("  NEW  %-25s  %-6s  %s  %s",
                trade.get("politician_name", "?"),
                trade.get("ticker", "?"),
                trade.get("trade_type", "?"),
                trade.get("trade_date", "?"),
            )

    logger.info("[%s] %d new trade(s) stored", ts, len(new_trades))

    if not new_trades:
        return

    # Score & notify
    notified = 0
    for trade in new_trades:
        if notified >= MAX_PER_POLL:
            # Cap reached – mark remaining silently
            await mark_notified(trade["id"])
            continue

        filing_perf = await compute_trade_filing_performance(trade)
        enriched    = await enrich_trade(trade)
        insider     = compute_insider_score(enriched, filing_perf)

        if insider["score"] >= MIN_INSIDER_SCORE:
            ok = await send_trade_notification(trade, filing_perf=filing_perf, insider=insider)
            if ok:
                notified += 1
                await mark_notified(trade["id"])
                if insider["flagged"]:
                    logger.info(
                        "SUSPECT TRADE  %s | %s | score=%d | %s",
                        trade.get("politician_name"), trade.get("ticker"),
                        insider["score"], "; ".join(insider["reasons"][:2]),
                    )
                else:
                    logger.info(
                        "Notified  %s | %s | score=%d",
                        trade.get("politician_name"), trade.get("ticker"),
                        insider["score"],
                    )
        else:
            # Not pertinent enough – discard silently
            logger.debug(
                "Skipped (score=%d)  %s | %s",
                insider["score"], trade.get("politician_name"), trade.get("ticker"),
            )
            await mark_notified(trade["id"])

        await asyncio.sleep(0.5)

    if notified:
        logger.info("[%s] %d trade(s) notified (score >= %d)", ts, notified, MIN_INSIDER_SCORE)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        poll_trades,
        trigger="interval",
        minutes=POLL_INTERVAL_MINUTES,
        id="poll_trades",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    logger.info(
        "Scheduler configured: poll every %d min, notify trades with score >= %d",
        POLL_INTERVAL_MINUTES, MIN_INSIDER_SCORE,
    )
    return scheduler
