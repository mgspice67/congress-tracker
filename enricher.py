"""
enricher.py – Enrich trade data with sector info (yfinance) and conflict-of-interest detection.

Design principles:
  - Sector lookups are cached in SQLite → yfinance is called AT MOST ONCE per ticker ever
  - All enrichment is async-safe (runs in a thread pool for the sync yfinance calls)
  - Never raises: returns empty/None on any failure so notifications always go out
"""
import asyncio
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Sector → committee keywords mapping ──────────────────────────────────────
# If a politician sits on a committee whose name contains any of these keywords,
# AND the traded stock is in the matching sector, we flag a conflict of interest.

SECTOR_COMMITTEE_MAP: dict[str, list[str]] = {
    "Technology":            ["intelligence", "commerce", "science", "technology", "energy"],
    "Communication Services":["commerce", "intelligence", "judiciary"],
    "Healthcare":            ["health", "labor", "aging", "veterans", "appropriations"],
    "Pharmaceuticals":       ["health", "labor", "aging", "finance", "appropriations"],
    "Biotechnology":         ["health", "labor", "science", "appropriations"],
    "Defense":               ["armed services", "defense", "intelligence", "foreign affairs", "homeland"],
    "Aerospace":             ["armed services", "defense", "intelligence", "transportation"],
    "Energy":                ["energy", "environment", "natural resources", "appropriations"],
    "Utilities":             ["energy", "environment", "commerce"],
    "Financial Services":    ["finance", "banking", "financial services", "budget"],
    "Banks":                 ["finance", "banking", "financial services", "budget"],
    "Real Estate":           ["banking", "financial services", "appropriations"],
    "Industrials":           ["armed services", "transportation", "commerce"],
    "Agriculture":           ["agriculture", "appropriations", "environment"],
    "Consumer Staples":      ["agriculture", "commerce", "appropriations"],
    "Consumer Discretionary":["commerce", "judiciary"],
    "Materials":             ["environment", "energy", "natural resources"],
    "Transportation":        ["transportation", "commerce", "infrastructure"],
    "Semiconductors":        ["intelligence", "commerce", "science", "technology", "armed services"],
}

# Friendly sector display names
SECTOR_EMOJI: dict[str, str] = {
    "Technology":             "💻 Tech",
    "Communication Services": "📡 Télécoms",
    "Healthcare":             "🏥 Santé",
    "Pharmaceuticals":        "💊 Pharma",
    "Biotechnology":          "🧬 Biotech",
    "Defense":                "🛡️ Défense",
    "Aerospace":              "🚀 Aérospatial",
    "Energy":                 "⚡ Énergie",
    "Utilities":              "🔌 Services publics",
    "Financial Services":     "🏦 Finance",
    "Banks":                  "🏦 Banque",
    "Real Estate":            "🏢 Immobilier",
    "Industrials":            "🏭 Industrie",
    "Agriculture":            "🌾 Agriculture",
    "Consumer Staples":       "🛒 Conso. courante",
    "Consumer Discretionary": "🛍️ Conso. discrétionnaire",
    "Materials":              "⛏️ Matériaux",
    "Transportation":         "✈️ Transport",
    "Semiconductors":         "🔬 Semi-conducteurs",
}


# ── In-memory ticker cache (process-scoped) ───────────────────────────────────
# Replaced the SQLite cache: in cron.py mode each run is a fresh process, so
# persistence across runs is not needed. Within a single run, the dict avoids
# refetching yfinance for repeated tickers.

_TICKER_CACHE: dict[str, dict] = {}


async def ensure_cache_table():
    """No-op kept for backwards compatibility with old callers."""
    return


async def _get_cached_sector(ticker: str) -> dict | None:
    return _TICKER_CACHE.get(ticker)


async def _save_sector_cache(ticker: str, sector: str, industry: str, long_name: str):
    _TICKER_CACHE[ticker] = {
        "ticker":    ticker,
        "sector":    sector,
        "industry":  industry,
        "long_name": long_name,
    }


# ── yfinance lookup (sync → run in thread) ────────────────────────────────────

def _yfinance_lookup(ticker: str) -> dict:
    """Synchronous yfinance call. Returns dict with sector/industry/long_name."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return {
            "sector":    info.get("sector", ""),
            "industry":  info.get("industry", ""),
            "long_name": info.get("longName", ""),
        }
    except Exception as e:
        logger.debug("yfinance error for %s: %s", ticker, e)
        return {"sector": "", "industry": "", "long_name": ""}


async def get_ticker_info(ticker: str) -> dict:
    """Get sector info for a ticker, using cache first."""
    if not ticker or ticker == "N/A":
        return {"sector": "", "industry": "", "long_name": ""}

    await ensure_cache_table()

    # Try cache first
    cached = await _get_cached_sector(ticker)
    if cached:
        logger.debug("Cache hit for %s → %s", ticker, cached.get("sector"))
        return cached

    # Fetch from yfinance in thread pool (non-blocking)
    logger.info("Fetching sector for %s via yfinance…", ticker)
    result = await asyncio.to_thread(_yfinance_lookup, ticker)

    # Save to cache
    await _save_sector_cache(
        ticker,
        result.get("sector", ""),
        result.get("industry", ""),
        result.get("long_name", ""),
    )
    return result


# ── Committees loader ─────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_committees() -> dict:
    path = Path(__file__).parent / "committees.json"
    try:
        data = json.loads(path.read_text())
        # Remove the doc key
        data.pop("_doc", None)
        return data
    except Exception as e:
        logger.warning("Could not load committees.json: %s", e)
        return {}


def _find_entry(politician_id: str, name: str = "") -> dict:
    """
    Look up a politician in committees.json.
    Tries multiple key formats to handle first_last vs last_first mismatches.
    """
    data = load_committees()

    def clean(s: str) -> str:
        import re as _re
        return _re.sub(r"[^\w\s]", "", s).strip().lower().replace(" ", "_")

    # 1. Direct match on politician_id (first_last)
    key = clean(politician_id)
    if key in data:
        return data[key]

    # 2. Reverse to last_first from full name  e.g. "Byron Donalds" → "donalds_byron"
    if name:
        parts = name.strip().split()
        if len(parts) >= 2:
            last_first = clean(f"{parts[-1]} {' '.join(parts[:-1])}")
            if last_first in data:
                return data[last_first]

    # 3. Partial match on last name
    if name:
        last = clean(name.strip().split()[-1])
        for k, v in data.items():
            if k.startswith(last + "_") or k.endswith("_" + last):
                return v

    return {}


def get_politician_committees(politician_id: str, name: str = "") -> list[str]:
    """Return list of committee names for a politician, empty list if unknown."""
    return _find_entry(politician_id, name).get("committees", [])


def get_politician_party(politician_id: str, name: str = "") -> str:
    """Return the party for a politician from committees.json, empty string if unknown."""
    party = _find_entry(politician_id, name).get("party", "")
    # Normalize: congress.gov returns "Democratic", we want "Democrat"
    if party == "Democratic":
        return "Democrat"
    return party


# ── Conflict of interest detection ────────────────────────────────────────────

def detect_conflict(sector: str, committees: list[str]) -> str | None:
    """
    Returns a conflict description string if the sector matches a committee keyword,
    or None if no conflict detected.
    """
    if not sector or not committees:
        return None

    keywords = SECTOR_COMMITTEE_MAP.get(sector, [])
    if not keywords:
        return None

    committees_lower = [c.lower() for c in committees]
    for kw in keywords:
        for committee in committees_lower:
            if kw in committee:
                # Find the matching committee (original case)
                matching = next(
                    (c for c in committees if kw in c.lower()), committees[0]
                )
                return matching

    return None


# ── Wikipedia URL builder ─────────────────────────────────────────────────────

def wikipedia_url(name: str) -> str:
    """Build a best-guess Wikipedia URL from a politician's name."""
    # Clean and format: "Nancy Pelosi" → "Nancy_Pelosi"
    clean = re.sub(r"[^\w\s]", "", name).strip()
    slug  = "_".join(clean.split())
    return f"https://en.wikipedia.org/wiki/{slug}"


# ── Owner label ───────────────────────────────────────────────────────────────

OWNER_LABELS = {
    "self":   "👤 Élu(e) lui/elle-même",
    "filer":  "👤 Élu(e) lui/elle-même",
    "spouse": "💑 Conjoint(e)",
    "child":  "👶 Enfant",
    "joint":  "👥 Compte joint",
    "dependent": "👨‍👩‍👧 Dépendant",
}

def format_owner(owner_raw: str) -> str:
    key = (owner_raw or "").lower().strip()
    return OWNER_LABELS.get(key, f"👤 {owner_raw}" if owner_raw else "")


# ── Main enrichment function ──────────────────────────────────────────────────

async def enrich_trade(trade: dict) -> dict:
    """
    Add enrichment fields to a trade dict:
      - sector, sector_display
      - wikipedia_url
      - owner_label
      - conflict_committee (or None)
    Returns a new dict (original not mutated).
    """
    enriched = dict(trade)

    # 1. Sector via yfinance (cached)
    ticker_info = await get_ticker_info(trade.get("ticker", ""))
    sector = ticker_info.get("sector", "")
    enriched["sector"]         = sector
    enriched["sector_display"] = SECTOR_EMOJI.get(sector, f"📊 {sector}") if sector else ""

    # 2. Wikipedia URL
    enriched["wikipedia_url"] = wikipedia_url(trade.get("politician_name", ""))

    # 3. Owner (from raw data)
    raw   = trade.get("raw") or {}
    owner = raw.get("owner") or raw.get("Owner") or ""
    enriched["owner_label"] = format_owner(owner)

    # 4. Party enrichment (House XML doesn't include party)
    pol_id   = trade.get("politician_id", "")
    pol_name = trade.get("politician_name", "")
    if not enriched.get("politician_party"):
        enriched["politician_party"] = get_politician_party(pol_id, pol_name)

    # 5. Conflict of interest
    committees = get_politician_committees(pol_id, pol_name)
    conflict   = detect_conflict(sector, committees)
    enriched["conflict_committee"] = conflict
    enriched["politician_committees"] = committees

    return enriched
