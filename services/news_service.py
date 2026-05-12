"""
Aggregated news service.
Fetches stock-specific news from major Indian financial media and
policy/regulatory news from official government RSS feeds.
"""

import asyncio
import logging
import urllib.parse
import feedparser

logger = logging.getLogger(__name__)

# ── REFERENCE SITES (shown in dashboard footer) ───────────────────────────
REFERENCE_SITES = [
    # Market news
    {"name": "Moneycontrol",      "url": "https://www.moneycontrol.com",                       "cat": "news"},
    {"name": "ET Markets",        "url": "https://economictimes.indiatimes.com/markets",        "cat": "news"},
    {"name": "LiveMint",          "url": "https://www.livemint.com",                            "cat": "news"},
    {"name": "Business Standard", "url": "https://www.business-standard.com",                   "cat": "news"},
    {"name": "Reuters Markets",   "url": "https://www.reuters.com/markets",                     "cat": "news"},
    {"name": "Bloomberg",         "url": "https://www.bloomberg.com",                           "cat": "news"},
    # Data & charting
    {"name": "Investing.com",     "url": "https://www.investing.com",                           "cat": "data"},
    {"name": "TradingView",       "url": "https://www.tradingview.com",                         "cat": "data"},
    {"name": "Trading Economics", "url": "https://tradingeconomics.com",                        "cat": "data"},
    {"name": "Yahoo Finance",     "url": "https://finance.yahoo.com",                           "cat": "data"},
    # Official India
    {"name": "RBI",               "url": "https://rbi.org.in",                                  "cat": "govt"},
    {"name": "SEBI",              "url": "https://sebi.gov.in",                                 "cat": "govt"},
    {"name": "NSE India",         "url": "https://nseindia.com",                               "cat": "exchange"},
    {"name": "BSE India",         "url": "https://bseindia.com",                               "cat": "exchange"},
    {"name": "PIB",               "url": "https://pib.gov.in",                                  "cat": "govt"},
    {"name": "Ministry of Finance","url": "https://finmin.gov.in",                             "cat": "govt"},
    {"name": "MCA",               "url": "https://mca.gov.in",                                  "cat": "govt"},
    {"name": "NITI Aayog",        "url": "https://niti.gov.in",                                 "cat": "govt"},
    {"name": "DPIIT",             "url": "https://dpiit.gov.in",                                "cat": "govt"},
]

# ── DIRECT MARKET NEWS RSS FEEDS ─────────────────────────────────────────
# General market feeds — we filter their content by company name/symbol
_MARKET_RSS: list[tuple[str, str]] = [
    ("Moneycontrol",      "https://www.moneycontrol.com/rss/MCtopnews.xml"),
    ("Economic Times",    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("LiveMint",          "https://www.livemint.com/rss/markets"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Reuters India",     "https://feeds.reuters.com/reuters/INbusinessNews"),
]

# ── OFFICIAL POLICY / REGULATORY RSS FEEDS ───────────────────────────────
_POLICY_RSS: list[tuple[str, str]] = [
    ("RBI",          "https://www.rbi.org.in/Scripts/RSS.aspx?Id=72"),   # press releases
    ("PIB",          "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"),
    ("SEBI",         "https://www.sebi.gov.in/sebirss.xml"),
    ("BSE India",    "https://www.bseindia.com/xml/xml_rss.aspx?page=news"),
]

GOOGLE_NEWS_BASE = "https://news.google.com/rss/search"


# ── HELPERS ───────────────────────────────────────────────────────────────

def _parse_feed(url: str) -> list:
    """Synchronous feedparser call — run via asyncio.to_thread."""
    try:
        feed = feedparser.parse(url)
        return feed.entries
    except Exception as exc:
        logger.debug("feedparser failed for %s: %s", url, exc)
        return []


def _entry_to_dict(entry, source_override: str = "") -> dict:
    source = (
        source_override
        or entry.get("source", {}).get("title", "")
        or ""
    )
    title = entry.get("title", "").strip()
    # Google News appends " - Source Name" to titles — strip it
    if source and title.endswith(f" - {source}"):
        title = title[: -(len(source) + 3)].strip()

    return {
        "title":        title,
        "source":       source,
        "published_at": entry.get("published", ""),
        "url":          entry.get("link", ""),
        "description":  entry.get("summary", ""),
    }


def _is_relevant(entry_dict: dict, keywords: list[str]) -> bool:
    """Check if title or description contains at least one keyword (case-insensitive)."""
    haystack = (entry_dict["title"] + " " + entry_dict["description"]).lower()
    return any(kw.lower() in haystack for kw in keywords)


def _deduplicate(items: list[dict]) -> list[dict]:
    """Remove items whose titles are near-duplicate (same first 60 chars)."""
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        key = item["title"][:60].lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ── PUBLIC API ────────────────────────────────────────────────────────────

async def get_stock_news(symbol: str, company_name: str = "") -> list[dict]:
    """
    Aggregate stock-specific news from:
    1. Google News RSS — company-specific query targeting Indian financial media
    2. Direct RSS from Moneycontrol, ET, LiveMint, Business Standard, Reuters
       filtered by company name / symbol
    Returns up to 12 deduplicated items sorted newest-first.
    """
    name = company_name or symbol
    # Keywords for relevance filtering against broad market feeds
    keywords = [symbol.upper(), name.split()[0] if name else symbol]

    # Build Google News query targeting major Indian business sources
    sites = (
        "site:moneycontrol.com OR site:economictimes.com OR site:livemint.com "
        "OR site:business-standard.com OR site:reuters.com OR site:bloomberg.com "
        "OR site:finance.yahoo.com OR site:investing.com"
    )
    gn_query = urllib.parse.quote(f'"{name}" OR "{symbol}" NSE India stock')
    gn_url   = f"{GOOGLE_NEWS_BASE}?q={gn_query}&hl=en-IN&gl=IN&ceid=IN:en"

    # Fetch concurrently: Google News + all direct market RSS feeds
    tasks = [asyncio.to_thread(_parse_feed, gn_url)] + [
        asyncio.to_thread(_parse_feed, url) for _, url in _MARKET_RSS
    ]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    news: list[dict] = []

    # Process Google News results (index 0)
    gn_entries = all_results[0] if isinstance(all_results[0], list) else []
    for entry in gn_entries[:10]:
        d = _entry_to_dict(entry)
        if d["title"]:
            news.append(d)

    # Process direct RSS feeds (indices 1+), filter by relevance
    for i, (src_name, _) in enumerate(_MARKET_RSS):
        entries = all_results[i + 1] if isinstance(all_results[i + 1], list) else []
        for entry in entries:
            d = _entry_to_dict(entry, source_override=src_name)
            if d["title"] and _is_relevant(d, keywords):
                news.append(d)

    return _deduplicate(news)[:12]


async def get_policy_news(sector: str = "", company_name: str = "") -> list[dict]:
    """
    Aggregate regulatory/policy news from:
    1. Direct RSS from RBI, SEBI, PIB, BSE
    2. Google News RSS targeting Ministry of Finance, RBI, SEBI, PIB, NITI Aayog
    Returns up to 10 deduplicated items.
    """
    # Build Google News policy query
    sector_q = sector.replace(" ", "+") if sector else "economy"
    gn_query = urllib.parse.quote(
        f"RBI OR SEBI OR \"Ministry of Finance\" OR PIB OR NITI Aayog India {sector}"
    )
    gn_url = f"{GOOGLE_NEWS_BASE}?q={gn_query}&hl=en-IN&gl=IN&ceid=IN:en"

    # Sector-specific Google News (e.g. "RBI banking policy" or "SEBI IT sector")
    sector_gn_url = ""
    if sector:
        sq = urllib.parse.quote(f"{sector} India policy regulation RBI SEBI 2025 2026")
        sector_gn_url = f"{GOOGLE_NEWS_BASE}?q={sq}&hl=en-IN&gl=IN&ceid=IN:en"

    tasks = (
        [asyncio.to_thread(_parse_feed, gn_url)]
        + ([asyncio.to_thread(_parse_feed, sector_gn_url)] if sector_gn_url else [])
        + [asyncio.to_thread(_parse_feed, url) for _, url in _POLICY_RSS]
    )
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    policy: list[dict] = []
    task_idx = 0

    # Google News general policy
    for entry in (all_results[task_idx] if isinstance(all_results[task_idx], list) else [])[:8]:
        d = _entry_to_dict(entry)
        if d["title"]:
            policy.append(d)
    task_idx += 1

    # Google News sector-specific
    if sector_gn_url:
        for entry in (all_results[task_idx] if isinstance(all_results[task_idx], list) else [])[:5]:
            d = _entry_to_dict(entry)
            if d["title"]:
                policy.append(d)
        task_idx += 1

    # Official RSS feeds (RBI, SEBI, PIB, BSE)
    for src_name, _ in _POLICY_RSS:
        entries = all_results[task_idx] if isinstance(all_results[task_idx], list) else []
        for entry in entries[:4]:
            d = _entry_to_dict(entry, source_override=src_name)
            if d["title"]:
                policy.append(d)
        task_idx += 1

    return _deduplicate(policy)[:10]
