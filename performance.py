"""
performance.py – Calculate politician trading performance over the last 365 days.

Logic:
  1. For each trade in the last 365 days, fetch the stock price on the trade date
     and the current price (both via yfinance, cached in SQLite).
  2. Compute a weighted return for each trade based on amount range midpoint.
  3. Aggregate into a single % return for the politician.
  4. Rank all politicians → compute percentile.

All price fetches are cached in ticker_price_cache to avoid redundant API calls.
"""
import asyncio
import logging
from datetime import date, timedelta

import aiosqlite

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

# ── Amount range → midpoint (USD) ─────────────────────────────────────────────
AMOUNT_MIDPOINTS = {
    "$1,001 - $15,000":         8_000,
    "$15,001 - $50,000":       32_500,
    "$50,001 - $100,000":      75_000,
    "$100,001 - $250,000":    175_000,
    "$250,001 - $500,000":    375_000,
    "$500,001 - $1,000,000":  750_000,
    "$1,000,001 - $5,000,000":3_000_000,
    "$5,000,001 - $25,000,000":15_000_000,
    "$25,000,001 - $50,000,000":37_500_000,
}

def parse_amount(amount_str: str) -> float:
    """Return the midpoint dollar value for an amount range string."""
    if not amount_str:
        return 50_000  # default guess
    for key, val in AMOUNT_MIDPOINTS.items():
        if key.lower() in amount_str.lower():
            return val
    # Try to extract numbers from unknown formats
    import re
    nums = re.findall(r"[\d,]+", amount_str.replace(",", ""))
    if len(nums) >= 2:
        return (int(nums[0]) + int(nums[1])) / 2
    if len(nums) == 1:
        return int(nums[0])
    return 50_000


# ── In-memory price cache (process-scoped) ────────────────────────────────────
# Replaced the SQLite cache: in cron.py mode each run is a fresh process, so
# persistence across runs is not needed. Within a single run, the dict avoids
# refetching the same (ticker, date) twice (e.g. when two trades share a ticker).

_PRICE_CACHE: dict[tuple[str, str], float] = {}


async def ensure_price_cache():
    """No-op kept for backwards compatibility with old callers."""
    return


async def _get_cached_price(ticker: str, price_date: str) -> float | None:
    return _PRICE_CACHE.get((ticker, price_date))


async def _save_price(ticker: str, price_date: str, price: float):
    _PRICE_CACHE[(ticker, price_date)] = price


def _fetch_price_sync(ticker: str, on_date: str) -> float | None:
    """Fetch historical close price for ticker on a specific date (sync, for thread pool)."""
    try:
        import yfinance as yf
        from datetime import datetime, timedelta as td
        dt    = datetime.strptime(on_date, "%Y-%m-%d")
        start = (dt - td(days=5)).strftime("%Y-%m-%d")
        end   = (dt + td(days=1)).strftime("%Y-%m-%d")
        hist  = yf.Ticker(ticker).history(start=start, end=end)
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug("Price fetch error %s @ %s: %s", ticker, on_date, e)
        return None


def _fetch_current_price_sync(ticker: str) -> float | None:
    """Fetch the most recent closing price."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug("Current price fetch error %s: %s", ticker, e)
        return None


async def get_price(ticker: str, on_date: str) -> float | None:
    """Get price with cache."""
    cached = await _get_cached_price(ticker, on_date)
    if cached is not None:
        return cached
    price  = await asyncio.to_thread(_fetch_price_sync, ticker, on_date)
    if price:
        await _save_price(ticker, on_date, price)
    return price


async def get_current_price(ticker: str) -> float | None:
    """Get today's price with cache (refreshed daily)."""
    today = str(date.today())
    cached = await _get_cached_price(ticker, today)
    if cached is not None:
        return cached
    price = await asyncio.to_thread(_fetch_current_price_sync, ticker)
    if price:
        await _save_price(ticker, today, price)
    return price


async def compute_trade_filing_performance(trade: dict) -> dict | None:
    """
    Compute the stock performance between trade_date and filed_date.
    Returns dict with: price_at_trade, price_at_filing, pct_change
    or None if prices can't be fetched.
    """
    ticker     = (trade.get("ticker") or "").strip()
    trade_date = (trade.get("trade_date") or "").strip()
    filed_date = (trade.get("filed_date") or "").strip()

    if not ticker or ticker == "N/A" or not trade_date or not filed_date:
        return None

    price_at_trade  = await get_price(ticker, trade_date)
    price_at_filing = await get_price(ticker, filed_date)

    if not price_at_trade or not price_at_filing or price_at_trade <= 0:
        return None

    pct_change = (price_at_filing - price_at_trade) / price_at_trade * 100

    return {
        "price_at_trade":  round(price_at_trade, 2),
        "price_at_filing": round(price_at_filing, 2),
        "pct_change":      round(pct_change, 2),
    }


# ── Trade return calculator ───────────────────────────────────────────────────

async def compute_trade_return(trade: dict) -> dict | None:
    """
    Compute the return for a single trade.
    Returns dict with keys: ticker, trade_type, amount, buy_price, current_price, pct_return, weighted_gain
    or None if prices can't be fetched.
    """
    ticker     = trade.get("ticker", "")
    trade_type = (trade.get("trade_type") or "").lower()
    trade_date = trade.get("trade_date", "")

    if not ticker or ticker == "N/A" or not trade_date:
        return None
    # Only consider buy trades for performance (sells close a position)
    if not ("purchase" in trade_type or "buy" in trade_type):
        return None

    buy_price     = await get_price(ticker, trade_date)
    current_price = await get_current_price(ticker)

    if not buy_price or not current_price or buy_price <= 0:
        return None

    pct_return  = (current_price - buy_price) / buy_price * 100
    amount      = parse_amount(trade.get("amount_range", ""))
    weighted    = pct_return * amount   # weight by trade size

    return {
        "ticker":        ticker,
        "trade_type":    trade_type,
        "trade_date":    trade_date,
        "amount":        amount,
        "buy_price":     buy_price,
        "current_price": current_price,
        "pct_return":    pct_return,
        "weighted_gain": weighted,
    }


# ── Politician performance ────────────────────────────────────────────────────

async def get_politician_performance(politician_id: str) -> dict | None:
    """
    Compute the overall performance (%) for a politician over the last 365 days.
    Returns dict with: pct_return, total_invested, best_trade, worst_trade, trade_count
    or None if insufficient data.
    """
    await ensure_price_cache()

    cutoff = str(date.today() - timedelta(days=365))

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM trades
            WHERE politician_id = ?
              AND trade_date >= ?
              AND ticker != 'N/A'
              AND ticker != ''
            ORDER BY trade_date
        """, (politician_id, cutoff)) as cur:
            trades = [dict(r) for r in await cur.fetchall()]

    if not trades:
        return None

    results = []
    for trade in trades:
        r = await compute_trade_return(trade)
        if r:
            results.append(r)

    if not results:
        return None

    total_invested  = sum(r["amount"] for r in results)
    weighted_return = sum(r["weighted_gain"] for r in results)
    avg_return      = weighted_return / total_invested if total_invested > 0 else 0

    best  = max(results, key=lambda x: x["pct_return"])
    worst = min(results, key=lambda x: x["pct_return"])

    return {
        "pct_return":    round(avg_return, 2),
        "total_invested":total_invested,
        "trade_count":   len(results),
        "best_trade":    best,
        "worst_trade":   worst,
    }


# ── Leaderboard & percentile ──────────────────────────────────────────────────

async def compute_all_performances() -> list[dict]:
    """
    Compute performance for every politician with trades in the last 365 days.
    Returns sorted list (best → worst).
    """
    cutoff = str(date.today() - timedelta(days=365))

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT DISTINCT politician_id, politician_name, politician_party,
                            politician_chamber, politician_state
            FROM trades
            WHERE trade_date >= ?
              AND ticker != 'N/A'
              AND ticker != ''
        """, (cutoff,)) as cur:
            politicians = [dict(r) for r in await cur.fetchall()]

    logger.info("Computing performance for %d politicians…", len(politicians))
    results = []
    for pol in politicians:
        perf = await get_politician_performance(pol["politician_id"])
        if perf:
            results.append({**pol, **perf})

    results.sort(key=lambda x: x["pct_return"], reverse=True)
    return results


async def get_percentile(politician_id: str, all_performances: list[dict]) -> int | None:
    """
    Return the percentile rank (0–100) of a politician among all performers.
    100 = best, 1 = worst.
    """
    if not all_performances:
        return None
    total = len(all_performances)
    rank  = next(
        (i + 1 for i, p in enumerate(all_performances) if p["politician_id"] == politician_id),
        None
    )
    if rank is None:
        return None
    # rank=1 is best → percentile = 100 - ((rank-1)/total * 100)
    percentile = round(100 - ((rank - 1) / total * 100))
    return max(1, min(100, percentile))


def format_performance_block(perf: dict | None, percentile: int | None) -> str:
    """
    Format a performance summary for inclusion in a Telegram message.
    Returns empty string if no data.
    """
    if not perf or perf.get("pct_return") is None:
        return ""

    pct  = perf["pct_return"]
    sign = "+" if pct >= 0 else ""
    emoji = "📈" if pct >= 0 else "📉"

    lines = [
        "",
        f"━━━━━━━━━━━━━━━━━━",
        f"{emoji} *Performance 365 jours*",
        f"Rendement : `{sign}{pct:.1f}%`",
    ]

    if perf.get("trade_count"):
        lines.append(f"Basé sur {perf['trade_count']} trade(s)")

    if perf.get("best_trade"):
        b = perf["best_trade"]
        lines.append(f"🏆 Meilleur : ${b['ticker']} `+{b['pct_return']:.1f}%`")

    if percentile is not None:
        if percentile >= 80:
            p_emoji = "🥇"
            p_label = "des meilleurs traders du Congrès"
        elif percentile >= 60:
            p_emoji = "🥈"
            p_label = "au-dessus de la moyenne"
        elif percentile >= 40:
            p_emoji = "📊"
            p_label = "dans la moyenne"
        else:
            p_emoji = "🔻"
            p_label = "en dessous de la moyenne"

        lines.append(
            f"{p_emoji} Top *{100 - percentile + 1}%* des élus — {p_label}"
        )

    return "\n".join(lines)
