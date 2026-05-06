"""
fetcher.py – Fetch congressional trade disclosures.

Sources (free, official US government data):
  - House PTR XML  : disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.xml
  - House PTR PDFs : parsed per filing to extract individual stock trades with tickers
  - Senate eFD     : (mirror via GitHub – fallback if unavailable)

Test mode: MOCK_DATA=true → synthetic trades, no HTTP calls
"""
import asyncio
import hashlib
import io
import logging
import random
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import httpx

from config import MOCK_DATA

logger = logging.getLogger(__name__)

_BASE = "https://disclosures-clerk.house.gov"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CongressTracker/1.0)",
    "Accept": "*/*",
}

# Max concurrent PDF downloads
_PDF_SEMAPHORE = asyncio.Semaphore(5)

# How many of the most recent PTR filings to parse (each PDF = one filing with N trades)
_MAX_PDFS = 20


# ── PDF parser ────────────────────────────────────────────────────────────────

# Ticker on same line as asset code: "Common Stock (NFLX) [ST]"
_TICKER_LINE_RE = re.compile(r'\(([A-Z]{1,6})\)\s*\[([A-Z]+)\]\s*$', re.IGNORECASE)
# Ticker at end of line, asset code on next line: "Common Stock (NFLX)" then "[ST]"
_TICKER_ONLY_RE = re.compile(r'\(([A-Z]{1,6})\)\s*$', re.IGNORECASE)
_ASSET_CODE_RE  = re.compile(r'^\[([A-Z]+)\]\s*$', re.IGNORECASE)
# Transaction line: starts with P/S/Exchange + two concatenated dates + amount
_TX_LINE_RE = re.compile(
    r'^(P|S\s*\(partial\)|S\s*\(full\)|S|Exchange)\s+'
    r'(\d{2}/\d{2}/\d{4})(\d{2}/\d{2}/\d{4})'
    r'(\$[\d,]+\s*-\s*\$[\d,]+|\$[\d,]+\+?)',
    re.IGNORECASE,
)
# Owner prefix: exactly 2 uppercase letters at start of line
_OWNER_LINE_RE = re.compile(r'^([A-Z]{2})\s+(.+)', re.IGNORECASE)
# Metadata lines to skip when walking backward.
# After null-byte stripping these become e.g. "F S: New" or "S O: Morgan Stanley"
_META_LINE_RE = re.compile(r'^[FS]\s*[A-Z]\s*:', re.IGNORECASE)


def _normalize_trade_type(raw: str) -> str:
    r = raw.strip().lower()
    if r.startswith("p"):         return "purchase"
    if "partial" in r:            return "sale_partial"
    if "full" in r or r.startswith("s"): return "sale"
    if "exchange" in r:           return "exchange"
    return r


def _parse_ptr_pdf(pdf_bytes: bytes, politician_name: str, politician_state: str,
                   filing_date_str: str, doc_id: str) -> list[dict]:
    """
    Parse a House PTR PDF line by line and extract individual stock transactions.

    PDF line structure per transaction:
      [OWNER_ID] [Company name — may wrap to next line]
      [Asset class] (TICKER) [ASSET_CODE]          ← ticker line
      [P|S|...] MM/DD/YYYYMM/DD/YYYY$AMOUNT        ← tx line (dates concatenated)
      F      S     : New
      S          O : [Account name]
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return []

    try:
        reader   = PdfReader(io.BytesIO(pdf_bytes))
        all_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        logger.debug("PDF parse error for %s: %s", doc_id, e)
        return []

    # Strip null bytes — PDF extraction sometimes embeds \x00 in metadata lines
    # which breaks pattern matching (e.g. "F\x00\x00 S\x00: New" becomes "F S: New")
    lines = [l.replace("\x00", "") for l in all_text.splitlines()]
    pol_id   = politician_name.lower().replace(" ", "_").replace(",", "").replace(".", "")
    trades   = []
    seen_ids = set()

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Detect ticker — two formats:
        # 1. Same line: "Common Stock (NFLX) [ST]"
        # 2. Two lines: "Common Stock (NFLX)" then "[ST]"
        m_ticker = _TICKER_LINE_RE.search(stripped)
        ticker_line_idx = i   # index of the line that contains (TICKER)
        asset_line_idx  = i   # index of the line that contains [ASSET_CODE]

        if not m_ticker:
            # Check two-line format
            m_only = _TICKER_ONLY_RE.search(stripped)
            if m_only and i + 1 < len(lines):
                next_stripped = lines[i + 1].strip()
                if _ASSET_CODE_RE.match(next_stripped):
                    m_ticker        = m_only
                    asset_line_idx  = i + 1
            if not m_ticker:
                continue

        ticker = m_ticker.group(1).upper()

        # Transaction line: the first non-empty line after the asset code line
        m_tx = None
        for j in range(asset_line_idx + 1, min(asset_line_idx + 4, len(lines))):
            m_tx = _TX_LINE_RE.match(lines[j].strip())
            if m_tx:
                break
        if not m_tx:
            continue

        tx_raw     = m_tx.group(1)
        tx_date_s  = m_tx.group(2)
        fil_date_s = m_tx.group(3)
        amount     = m_tx.group(4).strip()

        # Build company name:
        # The text before (TICKER) on the ticker line is always part of the name.
        # If the line starts with an owner prefix (JT, SP...), use that directly.
        # Otherwise walk back 1-2 lines to collect the beginning of multi-line names.
        ticker_line_stripped = stripped
        before_ticker = ticker_line_stripped[:ticker_line_stripped.rfind("(")].strip()

        company_parts = []
        owner_raw     = ""

        m_own_current = _OWNER_LINE_RE.match(before_ticker)
        if m_own_current:
            # Owner prefix on the ticker line itself → company fully on this line
            owner_raw     = m_own_current.group(1)
            company_parts = [m_own_current.group(2).strip()]
        else:
            # No owner prefix → may be a continuation line; walk backward
            if before_ticker:
                company_parts = [before_ticker]
            for k in range(i - 1, max(i - 4, -1), -1):
                prev = lines[k].strip()
                # Stop at blank or metadata
                if not prev or _META_LINE_RE.match(prev):
                    break
                # Stop at transaction lines
                if _TX_LINE_RE.match(prev):
                    break
                # Stop at lines that look like table headers (contain $ or no letters)
                if re.match(r'^[\$\d>]', prev) or not re.search(r'[A-Za-z]', prev):
                    break
                m_own = _OWNER_LINE_RE.match(prev)
                if m_own:
                    owner_raw = m_own.group(1)
                    rest = m_own.group(2).strip()
                    if rest:
                        company_parts.insert(0, rest)
                    break
                company_parts.insert(0, prev)

        company = re.sub(r'\s+', ' ', " ".join(company_parts)).strip()

        trade_type        = _normalize_trade_type(tx_raw)
        tx_date           = _fmt_date(tx_date_s)
        notification_date = _fmt_date(fil_date_s)
        # filed_date = official PTR submission date (from XML FilingDate),
        # NOT the bank-notification date inside the PDF. Falls back to
        # notification_date if the XML value is missing/unparseable.
        filed_date = _fmt_date(filing_date_str) if filing_date_str else notification_date

        uid_str  = f"{pol_id}-{ticker}-{tx_date}-{trade_type}-{amount}"
        uid      = hashlib.md5(uid_str.encode()).hexdigest()[:12]
        trade_id = f"house_{uid}"

        if trade_id in seen_ids:
            continue
        seen_ids.add(trade_id)

        trades.append({
            "id":                 trade_id,
            "politician_id":      pol_id,
            "politician_name":    politician_name,
            "politician_party":   "",
            "politician_state":   politician_state,
            "politician_chamber": "house",
            "ticker":             ticker,
            "company":            (company or ticker)[:120],
            "trade_type":         trade_type,
            "amount_range":       amount,
            "trade_date":         tx_date,
            "filed_date":         filed_date,
            "notification_date":  notification_date,
            "price":              None,
            "asset_type":         "stock",
            "raw":                {"owner": owner_raw, "doc_id": doc_id},
        })

    logger.debug("PDF %s → %d trades parsed for %s", doc_id, len(trades), politician_name)
    return trades


def _fmt_date(dt_str: str) -> str:
    """Convert M/D/YYYY or MM/DD/YYYY → YYYY-MM-DD."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(dt_str.strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return dt_str


# ── House PTR XML + PDF parsing ───────────────────────────────────────────────

async def fetch_house_trades() -> list[dict]:
    """
    Fetch the House PTR index XML, then parse the most recent PTR PDFs
    to extract individual stock trades with real tickers.
    """
    if MOCK_DATA:
        return []

    year = date.today().year
    url  = f"{_BASE}/public_disc/financial-pdfs/{year}FD.xml"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=_HEADERS)
            r.raise_for_status()
            content = r.content.lstrip(b'\xef\xbb\xbf')
            root = ET.fromstring(content)
    except Exception as e:
        logger.error("House XML fetch error: %s", e)
        return []

    members = root.findall("Member")
    ptrs    = [m for m in members if (m.findtext("FilingType") or "").strip() == "P"]

    def parse_date(m):
        try:
            return datetime.strptime(m.findtext("FilingDate") or "", "%m/%d/%Y")
        except Exception:
            return datetime.min

    ptrs.sort(key=parse_date, reverse=True)
    ptrs = ptrs[:_MAX_PDFS]

    logger.info("House XML: %d PTR filings to parse (top %d)", len(ptrs), _MAX_PDFS)

    # Parse PDFs concurrently
    tasks = [_fetch_and_parse_pdf(m) for m in ptrs]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_trades = []
    for r in results:
        if isinstance(r, list):
            all_trades.extend(r)

    logger.info("House PDFs: %d individual trades extracted", len(all_trades))
    return all_trades


async def _fetch_and_parse_pdf(member_xml) -> list[dict]:
    """Download one PTR PDF and parse its trades."""
    first     = (member_xml.findtext("First") or "").strip()
    last      = (member_xml.findtext("Last")  or "").strip()
    name      = f"{first} {last}".strip()
    state_dst = (member_xml.findtext("StateDst") or "").strip()
    state     = state_dst[:2] if state_dst else ""
    doc_id    = (member_xml.findtext("DocID")      or "").strip()
    filing_dt = (member_xml.findtext("FilingDate") or "").strip()

    if not doc_id:
        return []

    url = f"{_BASE}/public_disc/ptr-pdfs/{date.today().year}/{doc_id}.pdf"

    async with _PDF_SEMAPHORE:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url, headers=_HEADERS)
                r.raise_for_status()
                pdf_bytes = r.content
        except Exception as e:
            logger.debug("PDF fetch error %s: %s", doc_id, e)
            return []

    trades = await asyncio.to_thread(
        _parse_ptr_pdf, pdf_bytes, name, state, filing_dt, doc_id
    )
    return trades


# ── Senate (live scrape of efdsearch.senate.gov via curl_cffi) ────────────────
#
# eFD is protected by Akamai bot manager — plain httpx/requests are blocked
# on TLS fingerprint. curl_cffi impersonates a real Chrome and gets through.

_EFD_BASE = "https://efdsearch.senate.gov"
_EFD_HOME = f"{_EFD_BASE}/search/home/"
_EFD_DATA = f"{_EFD_BASE}/search/report/data/"

# Days back to scan in the eFD listing. The cron's MAX_FILING_AGE_DAYS filter
# narrows this further; we just need enough headroom to never miss a filing.
_EFD_LOOKBACK_DAYS = 14
# Cap PTR detail fetches per run to be polite to the server.
_EFD_MAX_PTRS = 25

_EFD_ROW_RE = re.compile(
    r'<tr>\s*'
    r'<td>\s*(\d+)\s*</td>\s*'                 # row #
    r'<td>\s*(\d{2}/\d{2}/\d{4})\s*</td>\s*'   # tx date
    r'<td>\s*(.+?)\s*</td>\s*'                 # owner
    r'<td>(.*?)</td>\s*'                       # ticker (HTML)
    r'<td>(.*?)</td>\s*'                       # asset name (HTML)
    r'<td>\s*(.+?)\s*</td>\s*'                 # asset type
    r'<td>\s*(.+?)\s*</td>\s*'                 # trade type
    r'<td>\s*(.+?)\s*</td>\s*'                 # amount
    r'<td>(.*?)</td>\s*'                       # comment
    r'</tr>',
    re.DOTALL,
)
_EFD_TICKER_RE = re.compile(r'>([A-Z0-9.\-]+)</a>')
_EFD_TAGS_RE   = re.compile(r'<[^>]+>')


def _ef_normalize_type(raw: str) -> str:
    r = raw.lower()
    if "purchase" in r:            return "purchase"
    if "partial"  in r:            return "sale_partial"
    if "sale" in r or "sell" in r: return "sale"
    if "exchange" in r:            return "exchange"
    return r


def _fetch_senate_sync() -> list[dict]:
    """Synchronous scrape of efdsearch.senate.gov. Wrapped in a thread by
    fetch_senate_trades(). Uses curl_cffi to bypass Akamai TLS fingerprinting."""
    from curl_cffi import requests as crequests

    s = crequests.Session(impersonate="chrome")

    # 1. Consent flow
    r = s.get(_EFD_HOME, timeout=30)
    r.raise_for_status()
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r.text)
    if not m:
        logger.error("eFD: no CSRF token on home page")
        return []
    token = m.group(1)
    r = s.post(
        _EFD_HOME,
        data={"csrfmiddlewaretoken": token, "prohibition_agreement": "1"},
        headers={"Referer": _EFD_HOME},
        timeout=30,
    )
    r.raise_for_status()

    # 2. List recent PTRs (report_types=[11] is the PTR code)
    end   = date.today()
    start = end - timedelta(days=_EFD_LOOKBACK_DAYS)
    payload = {
        "draw": "1",
        "start": "0",
        "length": "100",
        "report_types": "[11]",
        "filer_types": "[]",
        "submitted_start_date": start.strftime("%m/%d/%Y") + " 00:00:00",
        "submitted_end_date":   end.strftime("%m/%d/%Y")   + " 23:59:59",
        "candidate_state": "",
        "senator_state":   "",
        "office_id":       "",
        "first_name":      "",
        "last_name":       "",
        "csrfmiddlewaretoken": token,
    }
    r = s.post(
        _EFD_DATA,
        data=payload,
        headers={"Referer": f"{_EFD_BASE}/search/", "X-Requested-With": "XMLHttpRequest"},
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json().get("data", []) or []

    ptrs = []
    for row in rows:
        if len(row) < 5:
            continue
        first, last, _, link_html, filed_str = row[0], row[1], row[2], row[3], row[4]
        href_m = re.search(r'href="([^"]+)"', link_html)
        if not href_m:
            continue
        href = href_m.group(1)
        # Skip paper (scanned PDF) filings — only electronic PTRs are HTML-parseable
        if "/search/view/ptr/" not in href:
            continue
        ptrs.append({
            "name":  f"{first} {last}".strip(),
            "url":   _EFD_BASE + href,
            "filed": _fmt_date(filed_str),
        })

    logger.info("eFD: %d electronic PTRs in last %dd", len(ptrs), _EFD_LOOKBACK_DAYS)

    # 3. Parse each PTR detail page
    all_trades = []
    for p in ptrs[:_EFD_MAX_PTRS]:
        try:
            r = s.get(p["url"], timeout=30)
            r.raise_for_status()
        except Exception as e:
            logger.debug("eFD detail fetch error %s: %s", p["url"], e)
            continue

        pol_id = p["name"].lower().replace(" ", "_").replace(",", "").replace(".", "")
        for m in _EFD_ROW_RE.finditer(r.text):
            _, tx_raw, owner, tk_html, nm_html, atype, ttype, amount, _ = m.groups()
            tk_m   = _EFD_TICKER_RE.search(tk_html)
            ticker = (tk_m.group(1) if tk_m else _EFD_TAGS_RE.sub("", tk_html).strip()) or "N/A"
            company = re.sub(r"\s+", " ", _EFD_TAGS_RE.sub("", nm_html)).strip() or "Unknown"
            tx_date    = _fmt_date(tx_raw)
            trade_type = _ef_normalize_type(ttype)

            uid_str  = f"{pol_id}-{ticker}-{tx_date}-{trade_type}-{amount}"
            uid      = hashlib.md5(uid_str.encode()).hexdigest()[:12]
            all_trades.append({
                "id":                 f"senate_{uid}",
                "politician_id":      pol_id,
                "politician_name":    p["name"],
                "politician_party":   "",
                "politician_state":   "",
                "politician_chamber": "senate",
                "ticker":             ticker,
                "company":            company[:120],
                "trade_type":         trade_type,
                "amount_range":       amount.strip(),
                "trade_date":         tx_date,
                "filed_date":         p["filed"],
                "price":              None,
                "asset_type":         atype.strip().lower() or "stock",
                "raw":                {"owner": owner, "ptr_url": p["url"]},
            })

    logger.info("eFD: %d Senate trades extracted", len(all_trades))
    return all_trades


async def fetch_senate_trades() -> list[dict]:
    """Fetch Senate trades from efdsearch.senate.gov. Runs the (synchronous)
    curl_cffi scraper in a thread to keep the async signature."""
    if MOCK_DATA:
        return []
    try:
        trades = await asyncio.to_thread(_fetch_senate_sync)
    except Exception as e:
        logger.error("Senate eFD fetch error: %s", e)
        return []
    trades.sort(key=lambda x: x.get("trade_date", ""), reverse=True)
    return trades


# ── Combined ──────────────────────────────────────────────────────────────────

async def fetch_all_trades() -> list[dict]:
    if MOCK_DATA:
        return _mock_trades(50)

    house, senate = await asyncio.gather(
        fetch_house_trades(),
        fetch_senate_trades(),
    )
    all_trades = house + senate
    all_trades.sort(key=lambda x: x.get("trade_date", ""), reverse=True)
    logger.info("Total: %d house + %d senate trades", len(house), len(senate))
    return all_trades


# Backwards-compat aliases
async def fetch_capitol_trades(page: int = 1, page_size: int = 50) -> list[dict]:
    return await fetch_all_trades()

async def fetch_quiver_trades(limit: int = 50) -> list[dict]:
    return []

async def fetch_politician_detail(politician_id: str):
    return None


# ── Mock data ─────────────────────────────────────────────────────────────────

_MOCK_POLITICIANS = [
    ("nancy_pelosi",      "Nancy Pelosi",             "Democrat",   "CA", "house"),
    ("paul_ryan",         "Paul Ryan",                "Republican", "WI", "house"),
    ("mitch_mcconnell",   "Mitch McConnell",          "Republican", "KY", "senate"),
    ("bernie_sanders",    "Bernie Sanders",           "Democrat",   "VT", "senate"),
    ("dan_crenshaw",      "Dan Crenshaw",             "Republican", "TX", "house"),
    ("alexandria_ocasio", "Alexandria Ocasio-Cortez", "Democrat",   "NY", "house"),
    ("tommy_tuberville",  "Tommy Tuberville",         "Republican", "AL", "senate"),
    ("mark_kelly",        "Mark Kelly",               "Democrat",   "AZ", "senate"),
]
_MOCK_TICKERS = [
    ("AAPL","Apple Inc."),     ("MSFT","Microsoft Corp."),
    ("NVDA","NVIDIA Corp."),   ("GOOGL","Alphabet Inc."),
    ("AMZN","Amazon.com"),     ("TSLA","Tesla Inc."),
    ("META","Meta Platforms"), ("JPM","JPMorgan Chase"),
    ("LMT","Lockheed Martin"), ("RTX","Raytheon Technologies"),
    ("BA","Boeing Co."),       ("PFE","Pfizer Inc."),
]
_MOCK_AMOUNTS = [
    "$1,001 - $15,000", "$15,001 - $50,000",
    "$50,001 - $100,000", "$100,001 - $250,000",
    "$250,001 - $500,000", "$500,001 - $1,000,000",
]
_mock_counter = 0

def _mock_trades(n: int = 10) -> list[dict]:
    global _mock_counter
    trades, today = [], date.today()
    for _ in range(n):
        _mock_counter += 1
        pol_id, pol_name, party, state, chamber = random.choice(_MOCK_POLITICIANS)
        ticker, company = random.choice(_MOCK_TICKERS)
        trade_type      = random.choice(["purchase", "sale", "sale_partial"])
        tx_date         = today - timedelta(days=random.randint(0, 45))
        filed_date      = tx_date + timedelta(days=random.randint(3, 45))
        trades.append({
            "id":                 f"mock_{_mock_counter:06d}_{tx_date}",
            "politician_id":      pol_id,
            "politician_name":    pol_name,
            "politician_party":   party,
            "politician_state":   state,
            "politician_chamber": chamber,
            "ticker":             ticker,
            "company":            company,
            "trade_type":         trade_type,
            "amount_range":       random.choice(_MOCK_AMOUNTS),
            "trade_date":         str(tx_date),
            "filed_date":         str(filed_date),
            "price":              round(random.uniform(10, 800), 2),
            "asset_type":         "stock",
            "raw":                {},
        })
    return trades
