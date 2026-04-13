"""
notifier.py – Telegram notification logic (HTML format).

Each notification includes:
  - Buy/sell operation
  - Politician name, party, state, chamber
  - Stock ticker + company
  - Trade amount range
  - Transaction date + filing date
  - Approximate price at trade time
  - Performance % between trade date and filing date
  - Wikipedia link
  - Conflict of interest alert if detected
"""
import html
import logging

from telegram import Bot
from telegram.constants import ParseMode

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from enricher import enrich_trade
from performance import compute_trade_filing_performance

logger = logging.getLogger(__name__)

_PARTY_EMOJI = {"Democrat": "\U0001f535", "Republican": "\U0001f534", "Independent": "\U0001f7e1"}
_TRADE_LABEL = {
    "purchase":     "\U0001f7e2 ACHAT",
    "buy":          "\U0001f7e2 ACHAT",
    "sale":         "\U0001f534 VENTE",
    "sell":         "\U0001f534 VENTE",
    "sale_full":    "\U0001f534 VENTE TOTALE",
    "sale_partial": "\U0001f7e0 VENTE PARTIELLE",
    "exchange":     "\U0001f504 ECHANGE",
    "disclosure":   "\U0001f4cb DECLARATION",
}
_CHAMBER_LABEL = {"senate": "Senateur", "house": "Representant"}


def _fmt(trade: dict, filing_perf: dict | None = None, insider: dict | None = None) -> str:
    """Format a trade notification as Telegram HTML."""
    t_type    = (trade.get("trade_type") or "").lower()
    label     = _TRADE_LABEL.get(t_type, f"\U0001f4b1 {t_type.upper()}")
    party_emo = _PARTY_EMOJI.get(trade.get("politician_party", ""), "\u26aa")
    chamber   = _CHAMBER_LABEL.get(trade.get("politician_chamber", ""), "Elu")
    pol_name  = html.escape(trade.get("politician_name", "Inconnu"))
    party     = html.escape(trade.get("politician_party", ""))
    state     = html.escape(trade.get("politician_state", ""))
    ticker    = html.escape(trade.get("ticker", "N/A"))
    company   = html.escape(trade.get("company", "Inconnu"))
    amount    = html.escape(trade.get("amount_range") or "Non divulgue")
    tx_date   = trade.get("trade_date")  or "N/A"
    fil_date  = trade.get("filed_date")  or "N/A"

    sector_display     = trade.get("sector_display", "")
    owner_label        = trade.get("owner_label", "")
    conflict_committee = trade.get("conflict_committee")
    wikipedia          = trade.get("wikipedia_url", "")
    committees         = trade.get("politician_committees", [])

    # Insider score banner
    insider_banner = ""
    if insider and insider.get("flagged"):
        score = insider["score"]
        if insider["level"] == "high":
            insider_banner = f"\U0001f6a8 <b>TRADE SUSPECT — Score {score}/100</b>"
        else:
            insider_banner = f"\u26a0\ufe0f <b>Trade a surveiller — Score {score}/100</b>"

    lines = [
        insider_banner if insider_banner else "\U0001f3db <b>CONGRESS STOCK ALERT</b>",
        "",
        f"{label}",
        "",
        f"\U0001f464 <b>{pol_name}</b>",
        f"{party_emo} {party} | {chamber} | {state}",
    ]

    if committees:
        short = ", ".join(committees[:3])
        suffix = f" +{len(committees)-3}" if len(committees) > 3 else ""
        lines.append(f"\U0001f3db Commissions : <i>{html.escape(short)}{suffix}</i>")

    lines.append("")

    if ticker != "N/A":
        lines.append(f"\U0001f4c8 <b>${ticker}</b>  —  {company}")
    else:
        lines.append(f"\U0001f4c8 <b>{company}</b>")

    if sector_display:
        lines.append(f"\U0001f3f7 Secteur : {html.escape(sector_display)}")

    lines += [
        f"\U0001f4b0 Montant : <code>{amount}</code>",
        f"\U0001f4c5 Transaction : {tx_date}",
        f"\U0001f4cb Declare le  : {fil_date}",
    ]

    # Price at trade time
    if filing_perf and filing_perf.get("price_at_trade"):
        lines.append(f"\U0001f4b2 Prix approx. au trade : <b>${filing_perf['price_at_trade']:.2f}</b>")

    # Performance between trade and filing
    if filing_perf and filing_perf.get("pct_change") is not None:
        pct = filing_perf["pct_change"]
        sign = "+" if pct >= 0 else ""
        emo = "\U0001f4c8" if pct >= 0 else "\U0001f4c9"
        price_now = filing_perf.get("price_at_filing", 0)
        lines += [
            "",
            f"{emo} <b>Performance trade \u2192 declaration : {sign}{pct:.1f}%</b>",
            f"   Prix au trade : ${filing_perf['price_at_trade']:.2f} \u2192 declaration : ${price_now:.2f}",
        ]

    if owner_label:
        lines.append(f"\U0001f511 Operateur : {html.escape(owner_label)}")

    # Conflict of interest
    if conflict_committee:
        lines += [
            "",
            "\u26a0\ufe0f <b>CONFLIT D'INTERET POTENTIEL</b>",
            f"Siege en commission : <i>{html.escape(conflict_committee)}</i>",
        ]
        if sector_display:
            lines.append(f"Secteur du trade : <i>{html.escape(sector_display)}</i>")

    # Insider score detail
    if insider and insider.get("flagged") and insider.get("reasons"):
        score = insider["score"]
        level = insider["level"]
        bar   = "\U0001f7e5" * min(score // 20, 5)  # ■ blocks up to 5
        lines += [
            "",
            f"\U0001f575 <b>ANALYSE DELIT D'INITIE ({score}/100) {bar}</b>",
        ]
        for r in insider["reasons"]:
            lines.append(f"  \u2022 {html.escape(r)}")

    # Links
    lines.append("")
    link_parts = []
    if wikipedia:
        link_parts.append(f'<a href="{wikipedia}">Wikipedia</a>')
    pol_id = trade.get("politician_id", "")
    link_parts.append(
        f'<a href="https://www.capitoltrades.com/politicians/{pol_id}">Capitol Trades</a>'
    )
    lines.append("\U0001f517 " + "  |  ".join(link_parts))

    return "\n".join(lines)


async def send_trade_notification(
    trade: dict,
    filing_perf: dict | None = None,
    insider: dict | None = None,
) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured – notification skipped")
        return False
    try:
        enriched = await enrich_trade(trade)
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=_fmt(enriched, filing_perf=filing_perf, insider=insider),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info("Notification sent  %s – %s – %s",
                    trade.get("politician_name"),
                    trade.get("ticker"),
                    trade.get("trade_type"))
        return True
    except Exception as e:
        logger.error("Telegram send error: %s", e)
        return False


async def send_startup_message():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "\U0001f680 <b>Congress Tracker demarre !</b>\n\n"
                "Vous recevrez un rapport quotidien avec :\n"
                "\u2022 Tous les nouveaux trades detectes\n"
                "\u2022 Prix approx. + performance trade \u2192 declaration\n"
                "\u2022 Detection de conflits d'interet\n"
                "\u2022 Liens Wikipedia + Capitol Trades\n\n"
                "\U0001f4ca Dashboard : http://localhost:8000"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.debug("Startup message error: %s", e)


async def send_daily_header(count: int):
    """Send a header message before the daily batch of trade notifications."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"\U0001f3db\U0001f4ca <b>RAPPORT QUOTIDIEN</b>\n\n"
                f"\U0001f4e5 <b>{count}</b> nouveau(x) trade(s) detecte(s)\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.debug("Daily header error: %s", e)
