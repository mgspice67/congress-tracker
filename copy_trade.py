"""
copy_trade.py – Copy trading recommendation for congressional trades.

Évalue s'il est pertinent de copier un trade congressionnel MAINTENANT,
en tenant compte de :
  - La fraîcheur du trade (jours depuis trade_date)
  - Le délai de déclaration (combien de temps avant la divulgation)
  - L'évolution du prix depuis le trade jusqu'à aujourd'hui
  - La présence d'un conflit d'intérêt
  - La taille du trade (proxy de conviction)

Uniquement applicable aux ACHATS (copier une vente = short, hors scope).
"""
import logging
from datetime import date, datetime

from performance import get_price, get_current_price

logger = logging.getLogger(__name__)

_AMOUNT_MIDPOINTS = {
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
    for key, val in _AMOUNT_MIDPOINTS.items():
        if key.lower() in s.lower():
            return val
    return 0


def _days_since(date_str: str) -> int | None:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (date.today() - d).days
    except Exception:
        return None


def _days_between(a: str, b: str) -> int | None:
    try:
        return abs((datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days)
    except Exception:
        return None


async def compute_copy_recommendation(trade: dict, filing_perf: dict | None) -> dict:
    """
    Calcule une recommandation de copy trading.

    Retourne :
      - score         : int (0–100)
      - level         : "strong" | "moderate" | "skip"
      - label         : str (libellé affiché dans Telegram)
      - reasons       : list[str] (justifications détaillées)
      - price_now     : float | None
      - pct_since_trade : float | None (variation depuis le trade jusqu'à aujourd'hui)
    """
    ticker     = (trade.get("ticker") or "").strip()
    trade_type = (trade.get("trade_type") or "").lower()
    trade_date = (trade.get("trade_date") or "").strip()
    filed_date = (trade.get("filed_date") or "").strip()
    amount_str = trade.get("amount_range", "")
    conflict   = trade.get("conflict_committee")

    is_buy = "purchase" in trade_type or "buy" in trade_type

    # Ventes et tickers inconnus : non applicable
    if not is_buy or not ticker or ticker == "N/A":
        action = "Vente" if not is_buy else "Ticker inconnu"
        return {
            "score": 0, "level": "skip",
            "label": "⚪ Copy trading non applicable",
            "reasons": [f"{action} — le copy trading ne s'applique qu'aux achats"],
            "price_now": None,
            "pct_since_trade": None,
        }

    score   = 0
    reasons = []

    # ── 1. Prix actuel vs prix au trade ──────────────────────────────────────
    price_at_trade = (filing_perf or {}).get("price_at_trade")
    if not price_at_trade:
        price_at_trade = await get_price(ticker, trade_date)

    price_now       = await get_current_price(ticker)
    pct_since_trade = None

    if price_at_trade and price_now and price_at_trade > 0:
        pct_since_trade = (price_now - price_at_trade) / price_at_trade * 100

    if pct_since_trade is not None:
        p0 = f"${price_at_trade:.2f}"
        p1 = f"${price_now:.2f}"
        if pct_since_trade < 0:
            score += 20
            reasons.append(
                f"Action en baisse de {pct_since_trade:.1f}% depuis le trade "
                f"({p0} → {p1}) — entrée actuellement plus favorable que l'élu"
            )
        elif pct_since_trade <= 5:
            score += 15
            reasons.append(
                f"Hausse faible depuis le trade (+{pct_since_trade:.1f}%, {p0} → {p1}) "
                f"— fenêtre d'achat encore ouverte"
            )
        elif pct_since_trade <= 15:
            score += 5
            reasons.append(
                f"Hausse modérée depuis le trade (+{pct_since_trade:.1f}%, {p0} → {p1}) "
                f"— une partie du potentiel déjà consommée"
            )
        elif pct_since_trade <= 30:
            score -= 10
            reasons.append(
                f"Action déjà en hausse de +{pct_since_trade:.1f}% depuis le trade "
                f"({p0} → {p1}) — entrée tardive, risque de correction"
            )
        else:
            score -= 25
            reasons.append(
                f"Forte hausse depuis le trade (+{pct_since_trade:.1f}%, {p0} → {p1}) "
                f"— trop tard, le prix est peu attractif"
            )

    # ── 2. Fraîcheur du trade ─────────────────────────────────────────────────
    days_since = _days_since(trade_date)
    if days_since is not None:
        if days_since <= 7:
            score += 25
            reasons.append(
                f"Trade très récent ({days_since}j) — information fraîche, "
                f"catalyseur potentiel pas encore intégré"
            )
        elif days_since <= 14:
            score += 15
            reasons.append(f"Trade récent ({days_since}j) — fenêtre d'opportunité encore active")
        elif days_since <= 30:
            score += 5
            reasons.append(f"Trade de {days_since}j — signal encore potentiellement valide")
        else:
            score -= 10
            reasons.append(
                f"Trade ancien ({days_since}j) — signal possiblement dépassé, "
                f"le marché a eu le temps de s'ajuster"
            )

    # ── 3. Délai de déclaration ───────────────────────────────────────────────
    filing_delay = _days_between(trade_date, filed_date)
    if filing_delay is not None:
        if filing_delay > 45:
            score -= 15
            reasons.append(
                f"Déclaration très tardive ({filing_delay}j après le trade) — "
                f"l'information circule depuis longtemps, effet potentiellement déjà pricé"
            )
        elif filing_delay > 30:
            score -= 5
            reasons.append(
                f"Déclaration tardive ({filing_delay}j après le trade) — "
                f"information divulguée avec retard"
            )
        elif filing_delay <= 10:
            score += 10
            reasons.append(
                f"Déclaration rapide ({filing_delay}j après le trade) — "
                f"information divulguée promptement, signal plus exploitable"
            )

    # ── 4. Conflit d'intérêt ──────────────────────────────────────────────────
    if conflict:
        score += 15
        reasons.append(
            f"Conflit d'intérêt détecté (commission {conflict}) — "
            f"suggère un accès potentiel à une information non publique"
        )

    # ── 5. Taille du trade (conviction) ──────────────────────────────────────
    amount = _parse_amount(amount_str)
    if amount >= 100_000:
        score += 10
        reasons.append(f"Montant élevé ({amount_str}) — forte conviction de l'élu")
    elif amount >= 50_000:
        score += 5
        reasons.append(f"Montant notable ({amount_str}) — conviction modérée")

    # ── Niveau final ──────────────────────────────────────────────────────────
    score = max(0, min(100, score))

    if score >= 60:
        level = "strong"
        label = "🟢 COPY TRADING RECOMMANDÉ"
    elif score >= 35:
        level = "moderate"
        label = "🟡 COPY TRADING POSSIBLE"
    else:
        level = "skip"
        label = "🔴 COPY TRADING DÉCONSEILLÉ"

    return {
        "score":            score,
        "level":            level,
        "label":            label,
        "reasons":          reasons,
        "price_now":        round(price_now, 2) if price_now else None,
        "pct_since_trade":  round(pct_since_trade, 2) if pct_since_trade is not None else None,
    }
