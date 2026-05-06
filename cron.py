"""
cron.py – Single-shot polling script for GitHub Actions.

Runs ONE poll cycle then exits. Designed to be invoked by .github/workflows/poll.yml
on a cron schedule.

Persistence model (no SQLite):
  notified_trades.json holds the set of trade IDs already processed.
  After each run, the file is committed back to the repo by the workflow.

Per-run flow:
  1. Load notified_trades.json
  2. Fetch all trades from House XML + Senate mirror
  3. For each new trade (id not in set):
     - Skip if filed > MAX_FILING_AGE_DAYS ago → mark seen
     - Enrich, score, compute copy reco
     - If insider score >= MIN_INSIDER_SCORE → Telegram notif
     - Mark as seen (whether notified or below threshold)
  4. Save notified_trades.json (capped at NOTIFIED_KEEP entries)
"""
import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from copy_trade import compute_copy_recommendation
from enricher import enrich_trade
from fetcher import fetch_all_trades
from insider_score import compute_insider_score
from notifier import send_trade_notification
from performance import compute_trade_filing_performance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

NOTIFIED_FILE       = Path(__file__).parent / "notified_trades.json"
MIN_INSIDER_SCORE   = 20
MAX_PER_RUN         = 10
MAX_FILING_AGE_DAYS = 3   # only notify trades whose PTR was filed within this many days
MAX_TRADE_AGE_DAYS  = 30  # only notify trades whose transaction date is within this many days
NOTIFIED_KEEP       = 5000  # cap to avoid the JSON file growing unbounded


def load_notified() -> set[str]:
    if not NOTIFIED_FILE.exists():
        return set()
    try:
        return set(json.loads(NOTIFIED_FILE.read_text()))
    except Exception as e:
        logger.warning("notified_trades.json unreadable (%s) — starting fresh", e)
        return set()


def save_notified(ids: set[str]):
    keep = sorted(ids)[-NOTIFIED_KEEP:]
    NOTIFIED_FILE.write_text(json.dumps(keep, indent=0))


async def main():
    notified = load_notified()
    logger.info("Loaded %d previously seen trade IDs", len(notified))

    trades = await fetch_all_trades()
    logger.info("Fetched %d trades total", len(trades))

    new_trades = [t for t in trades if t["id"] not in notified]
    logger.info("%d trades are new (not yet seen)", len(new_trades))

    if not new_trades:
        save_notified(notified)
        return

    filing_cutoff = date.today() - timedelta(days=MAX_FILING_AGE_DAYS)
    trade_cutoff  = date.today() - timedelta(days=MAX_TRADE_AGE_DAYS)
    sent = 0
    skipped_filing = 0
    skipped_stale  = 0
    skipped_score  = 0

    for trade in new_trades:
        if sent >= MAX_PER_RUN:
            notified.add(trade["id"])
            continue

        filed_date = trade.get("filed_date", "")
        try:
            if filed_date and datetime.strptime(filed_date, "%Y-%m-%d").date() < filing_cutoff:
                logger.info("Skipped (filing too old, filed %s) %s | %s",
                            filed_date, trade.get("politician_name"), trade.get("ticker"))
                notified.add(trade["id"])
                skipped_filing += 1
                continue
        except ValueError:
            pass

        trade_date = trade.get("trade_date", "")
        try:
            if trade_date and datetime.strptime(trade_date, "%Y-%m-%d").date() < trade_cutoff:
                logger.info("Skipped (transaction too old, traded %s) %s | %s",
                            trade_date, trade.get("politician_name"), trade.get("ticker"))
                notified.add(trade["id"])
                skipped_stale += 1
                continue
        except ValueError:
            pass

        try:
            filing_perf = await compute_trade_filing_performance(trade)
            enriched    = await enrich_trade(trade)
            insider     = compute_insider_score(enriched, filing_perf)
            copy_reco   = await compute_copy_recommendation(enriched, filing_perf)
        except Exception as e:
            logger.exception("Enrichment failed for %s: %s", trade.get("id"), e)
            notified.add(trade["id"])
            continue

        if insider["score"] >= MIN_INSIDER_SCORE:
            ok = await send_trade_notification(
                trade, filing_perf=filing_perf, insider=insider, copy_reco=copy_reco
            )
            if ok:
                sent += 1
                notified.add(trade["id"])
                tag = "SUSPECT" if insider["flagged"] else "Notified"
                logger.info("%s  %s | %s | score=%d", tag,
                            trade.get("politician_name"), trade.get("ticker"),
                            insider["score"])
            else:
                # Telegram failed — leave the ID out so we retry next run
                logger.warning("Telegram send failed for %s — will retry next run",
                               trade.get("id"))
        else:
            logger.info("Skipped (score=%d) %s | %s | %s",
                        insider["score"], trade.get("politician_name"),
                        trade.get("ticker"), trade.get("trade_type"))
            notified.add(trade["id"])
            skipped_score += 1

        await asyncio.sleep(0.5)

    save_notified(notified)
    logger.info("Run complete: %d notified, %d skipped (old filing), %d skipped (stale trade), %d skipped (low score), %d total seen IDs",
                sent, skipped_filing, skipped_stale, skipped_score, len(notified))


if __name__ == "__main__":
    asyncio.run(main())
