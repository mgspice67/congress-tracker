"""
insider_score.py – Suspicion scoring for congressional trades.

Scores a trade on multiple signals to estimate the likelihood of
legally-permitted insider trading (members of Congress are exempt
from the Securities Exchange Act's insider trading provisions).

Score >= 50 → flagged as a "suspect trade" worth notifying.
Score >= 75 → flagged as high suspicion.
"""
import re
from datetime import date, datetime

# ── Sector weight ──────────────────────────────────────────────────────────────
# Sectors where information asymmetry via committee is most valuable
HIGH_OVERSIGHT_SECTORS = {
    "Defense", "Aerospace", "Semiconductors",
    "Pharmaceuticals", "Biotechnology", "Healthcare",
    "Technology", "Communication Services", "Financial Services",
}

# ── Amount midpoints ───────────────────────────────────────────────────────────
AMOUNT_MIDPOINTS = {
    "$1,001 - $15,000":          8_000,
    "$15,001 - $50,000":        32_500,
    "$50,001 - $100,000":       75_000,
    "$100,001 - $250,000":     175_000,
    "$250,001 - $500,000":     375_000,
    "$500,001 - $1,000,000":   750_000,
    "$1,000,001 - $5,000,000": 3_000_000,
}

def _parse_amount(s: str) -> float:
    if not s:
        return 0
    for key, val in AMOUNT_MIDPOINTS.items():
        if key.lower() in (s or "").lower():
            return val
    nums = re.findall(r"[\d]+", (s or "").replace(",", ""))
    if len(nums) >= 2:
        return (int(nums[0]) + int(nums[1])) / 2
    if len(nums) == 1:
        return int(nums[0])
    return 0


def _days_between(date_a: str, date_b: str) -> int | None:
    """Return number of days between two YYYY-MM-DD strings."""
    try:
        a = datetime.strptime(date_a, "%Y-%m-%d")
        b = datetime.strptime(date_b, "%Y-%m-%d")
        return abs((b - a).days)
    except Exception:
        return None


# ── Main scoring function ──────────────────────────────────────────────────────

def compute_insider_score(trade: dict, filing_perf: dict | None) -> dict:
    """
    Score a trade for insider trading suspicion.

    Returns a dict:
      - score       : int (0-100+)
      - level       : "high" | "medium" | "low"
      - reasons     : list[str]  human-readable explanations
      - flagged     : bool (score >= 50)
    """
    score   = 0
    reasons = []

    trade_type = (trade.get("trade_type") or "").lower()
    is_buy     = "purchase" in trade_type or "buy" in trade_type
    is_sell    = "sale" in trade_type or "sell" in trade_type

    sector             = trade.get("sector", "")
    conflict_committee = trade.get("conflict_committee")
    amount_str         = trade.get("amount_range", "")
    trade_date         = trade.get("trade_date", "")
    filed_date         = trade.get("filed_date", "")
    ticker             = trade.get("ticker", "")
    company            = trade.get("company", ticker)

    # ── 1. Conflict of interest ───────────────────────────────────────────────
    if conflict_committee:
        score += 40
        reasons.append(
            f"Siege en commission \"{conflict_committee}\" en lien direct avec "
            f"le secteur {sector} de {ticker}"
        )

    # ── 2. Performance entre trade et declaration ─────────────────────────────
    if filing_perf and filing_perf.get("pct_change") is not None:
        pct = filing_perf["pct_change"]
        p_at_trade  = filing_perf.get("price_at_trade", 0)
        p_at_filing = filing_perf.get("price_at_filing", 0)

        if is_buy and pct >= 15:
            score += 30
            reasons.append(
                f"Achat avant une hausse de +{pct:.1f}% "
                f"(${p_at_trade:.2f} → ${p_at_filing:.2f} entre trade et declaration)"
            )
        elif is_buy and pct >= 10:
            score += 20
            reasons.append(
                f"Achat avant une hausse de +{pct:.1f}% "
                f"(${p_at_trade:.2f} → ${p_at_filing:.2f})"
            )
        elif is_sell and pct <= -15:
            score += 30
            reasons.append(
                f"Vente avant une chute de {pct:.1f}% "
                f"(${p_at_trade:.2f} → ${p_at_filing:.2f} — perte evitee)"
            )
        elif is_sell and pct <= -10:
            score += 20
            reasons.append(
                f"Vente avant une chute de {pct:.1f}% "
                f"(${p_at_trade:.2f} → ${p_at_filing:.2f} — perte evitee)"
            )

    # ── 3. Montant du trade ───────────────────────────────────────────────────
    amount = _parse_amount(amount_str)
    if amount >= 100_000:
        score += 15
        reasons.append(f"Montant significatif : {amount_str}")
    elif amount >= 50_000:
        score += 10
        reasons.append(f"Montant notable : {amount_str}")
    elif amount >= 15_000:
        score += 5

    # ── 4. Delai de declaration ───────────────────────────────────────────────
    delay = _days_between(trade_date, filed_date)
    if delay is not None:
        if delay <= 10:
            score += 10
            reasons.append(
                f"Declaration tres rapide : {delay} jours apres le trade "
                f"(signe de conscience de la sensibilite de l'info)"
            )
        elif delay <= 20:
            score += 5

    # ── 5. Secteur haute surveillance ─────────────────────────────────────────
    if sector in HIGH_OVERSIGHT_SECTORS:
        score += 5
        if not any("secteur" in r.lower() for r in reasons):
            reasons.append(f"Secteur sous haute surveillance reglementaire : {sector}")

    # ── Niveau de suspicion ───────────────────────────────────────────────────
    if score >= 75:
        level = "high"
    elif score >= 50:
        level = "medium"
    else:
        level = "low"

    return {
        "score":   min(score, 100),
        "level":   level,
        "reasons": reasons,
        "flagged": score >= 50,
    }
