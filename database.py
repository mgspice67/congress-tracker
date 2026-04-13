"""
database.py – All SQLite operations via aiosqlite.
"""
import json
import aiosqlite
from config import DATABASE_PATH

# ── Schema ────────────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id                  TEXT PRIMARY KEY,
    politician_id       TEXT,
    politician_name     TEXT,
    politician_party    TEXT,
    politician_state    TEXT,
    politician_chamber  TEXT,
    ticker              TEXT,
    company             TEXT,
    trade_type          TEXT,
    amount_range        TEXT,
    trade_date          TEXT,
    filed_date          TEXT,
    price               REAL,
    asset_type          TEXT,
    raw_data            TEXT,
    notified            INTEGER DEFAULT 0,
    created_at          TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS politicians (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    party       TEXT,
    state       TEXT,
    chamber     TEXT,
    net_worth   TEXT,
    committees  TEXT,
    image_url   TEXT,
    bio         TEXT,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trades_date       ON trades(trade_date);
CREATE INDEX IF NOT EXISTS idx_trades_politician ON trades(politician_id);
CREATE INDEX IF NOT EXISTS idx_trades_notified   ON trades(notified);
CREATE INDEX IF NOT EXISTS idx_trades_ticker     ON trades(ticker);
"""


async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript(_DDL)
        await db.commit()


# ── Write ─────────────────────────────────────────────────────────────────────
async def insert_trade(trade: dict) -> bool:
    """Returns True if the trade is NEW (inserted), False if already known."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO trades (
                    id, politician_id, politician_name, politician_party,
                    politician_state, politician_chamber, ticker, company,
                    trade_type, amount_range, trade_date, filed_date,
                    price, asset_type, raw_data, notified
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
            """, (
                trade["id"],
                trade.get("politician_id", ""),
                trade.get("politician_name", ""),
                trade.get("politician_party", ""),
                trade.get("politician_state", ""),
                trade.get("politician_chamber", ""),
                trade.get("ticker", ""),
                trade.get("company", ""),
                trade.get("trade_type", ""),
                trade.get("amount_range", ""),
                trade.get("trade_date", ""),
                trade.get("filed_date", ""),
                trade.get("price"),
                trade.get("asset_type", "stock"),
                json.dumps(trade.get("raw", {})),
            ))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def upsert_politician(politician: dict):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO politicians (id, name, party, state, chamber, net_worth, committees, image_url, bio)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                net_worth  = excluded.net_worth,
                committees = excluded.committees,
                image_url  = excluded.image_url,
                bio        = excluded.bio,
                updated_at = CURRENT_TIMESTAMP
        """, (
            politician.get("id", ""),
            politician.get("name", ""),
            politician.get("party", ""),
            politician.get("state", ""),
            politician.get("chamber", ""),
            politician.get("net_worth", ""),
            politician.get("committees", ""),
            politician.get("image_url", ""),
            politician.get("bio", ""),
        ))
        await db.commit()


async def mark_notified(trade_id: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE trades SET notified=1 WHERE id=?", (trade_id,))
        await db.commit()


# ── Read ──────────────────────────────────────────────────────────────────────
async def get_unnotified_trades() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades WHERE notified=0 ORDER BY trade_date DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_recent_trades(
    limit: int = 50,
    offset: int = 0,
    politician: str | None = None,
    ticker: str | None = None,
    trade_type: str | None = None,
) -> list[dict]:
    conds, params = [], []
    if politician:
        conds.append("politician_name LIKE ?")
        params.append(f"%{politician}%")
    if ticker:
        conds.append("ticker = ?")
        params.append(ticker.upper())
    if trade_type:
        conds.append("trade_type LIKE ?")
        params.append(f"%{trade_type}%")

    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params += [limit, offset]

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM trades {where} ORDER BY trade_date DESC, created_at DESC LIMIT ? OFFSET ?",
            params,
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_leaderboard(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                politician_id,
                politician_name,
                politician_party,
                politician_state,
                politician_chamber,
                COUNT(*) AS total_trades,
                SUM(CASE WHEN trade_type LIKE '%purchase%' OR trade_type LIKE '%buy%' THEN 1 ELSE 0 END) AS buys,
                SUM(CASE WHEN trade_type LIKE '%sale%'     OR trade_type LIKE '%sell%' THEN 1 ELSE 0 END) AS sells,
                MAX(trade_date) AS last_trade
            FROM trades
            GROUP BY politician_id
            ORDER BY total_trades DESC
            LIMIT ?
        """, (limit,)) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_stats() -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN trade_type LIKE '%purchase%' OR trade_type LIKE '%buy%'  THEN 1 ELSE 0 END) AS buys,
                SUM(CASE WHEN trade_type LIKE '%sale%'     OR trade_type LIKE '%sell%' THEN 1 ELSE 0 END) AS sells,
                COUNT(DISTINCT politician_id) AS politicians,
                COUNT(DISTINCT ticker)        AS tickers
            FROM trades
        """) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def get_trades_by_day(days: int = 30) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                trade_date,
                COUNT(*) AS count,
                SUM(CASE WHEN trade_type LIKE '%purchase%' OR trade_type LIKE '%buy%'  THEN 1 ELSE 0 END) AS buys,
                SUM(CASE WHEN trade_type LIKE '%sale%'     OR trade_type LIKE '%sell%' THEN 1 ELSE 0 END) AS sells
            FROM trades
            WHERE trade_date >= date('now', '-' || ? || ' days')
            GROUP BY trade_date
            ORDER BY trade_date
        """, (days,)) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_politician(politician_id: str) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM politicians WHERE id=?", (politician_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_politician_trades(politician_id: str, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades WHERE politician_id=? ORDER BY trade_date DESC LIMIT ?",
            (politician_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
