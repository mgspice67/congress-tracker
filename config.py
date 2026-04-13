import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Data sources ─────────────────────────────────────────────────────────────
# Capitol Trades (primary – public, no key needed)
CAPITOL_TRADES_API = "https://api.capitoltrades.com"

# Quiver Quantitative (backup – free key at quiverquant.com)
QUIVER_API_KEY = os.getenv("QUIVER_API_KEY", "")

# ── App settings ─────────────────────────────────────────────────────────────
DATABASE_PATH          = os.getenv("DATABASE_PATH", "trades.db")
POLL_INTERVAL_MINUTES  = int(os.getenv("POLL_INTERVAL_MINUTES", "60"))
PORT                   = int(os.getenv("PORT", "8000"))

# Daily notification hour (0-23, in server local time)
NOTIFY_HOUR = int(os.getenv("NOTIFY_HOUR", "18"))
NOTIFY_MINUTE = int(os.getenv("NOTIFY_MINUTE", "5"))

# Set to "true" to generate fake trades and test the whole stack locally
MOCK_DATA = os.getenv("MOCK_DATA", "false").lower() == "true"
