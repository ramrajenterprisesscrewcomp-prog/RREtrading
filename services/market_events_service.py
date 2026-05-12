"""
market_events_service.py
Aggregates high-impact market news from Google News RSS and NSE FII/DII data.
"""
import hashlib
import logging
import asyncio
from datetime import datetime

import feedparser
import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)

# ── CATEGORIES ────────────────────────────────────────────────────────────────

CATEGORIES = {
    "CENTRAL_BANK":  ("🏛️", "Central Bank",   "HIGH"),
    "ECONOMIC_DATA": ("📊", "Economic Data",  "HIGH"),
    "FII_DII":       ("💰", "FII / DII Flow", "HIGH"),
    "COMMODITIES":   ("🛢️", "Commodities",    "MEDIUM"),
    "GOVERNMENT":    ("🏢", "Govt Policy",    "HIGH"),
    "CURRENCY":      ("💱", "Currency",       "MEDIUM"),
    "CORPORATE":     ("📰", "Corporate",      "MEDIUM"),
    "GEOPOLITICAL":  ("⚔️", "Geopolitical",   "HIGH"),
}

# RSS queries mapped to categories
_QUERIES = [
    ("RBI repo rate policy India monetary",            "CENTRAL_BANK"),
    ("Federal Reserve FOMC interest rate decision",    "CENTRAL_BANK"),
    ("India CPI inflation WPI GDP IIP release",        "ECONOMIC_DATA"),
    ("FII DII foreign institutional investor NSE",     "FII_DII"),
    ("crude oil Brent gold price surge drop",          "COMMODITIES"),
    ("India government SEBI regulation budget policy", "GOVERNMENT"),
    ("USD INR rupee dollar exchange rate",             "CURRENCY"),
    ("India geopolitical war sanctions election",      "GEOPOLITICAL"),
    ("NSE BSE Nifty quarterly earnings results",       "CORPORATE"),
]

# Category fetch priority order (higher priority = appears first)
_CATEGORY_PRIORITY = [
    "FII_DII", "CENTRAL_BANK", "ECONOMIC_DATA", "GOVERNMENT",
    "GEOPOLITICAL", "COMMODITIES", "CURRENCY", "CORPORATE",
]

_news_cache: TTLCache = TTLCache(maxsize=1, ttl=300)  # 5-minute cache


def _md5_id(text: str) -> str:
    """8-char MD5 hash of first 60 chars of title — used for dedup."""
    return hashlib.md5(text[:60].encode("utf-8", errors="replace")).hexdigest()[:8]


def _parse_source(entry) -> str:
    """Extract source name from feedparser entry."""
    try:
        source = entry.get("source", {})
        if isinstance(source, dict) and source.get("title"):
            return source["title"]
        tags = entry.get("tags", [])
        if tags:
            return tags[0].get("term", "")
        link = entry.get("link", "")
        if "economictimes" in link:
            return "Economic Times"
        if "reuters" in link:
            return "Reuters"
        if "bloomberg" in link:
            return "Bloomberg"
        if "moneycontrol" in link:
            return "Moneycontrol"
        if "livemint" in link:
            return "LiveMint"
        if "businessstandard" in link:
            return "Business Standard"
        if "ndtv" in link:
            return "NDTV"
    except Exception:
        pass
    return "News"


async def _fetch_rss_category(query: str, category: str) -> list:
    """Fetch Google News RSS for a query and return structured items."""
    encoded = query.replace(" ", "+")
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
    items = []
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
            feed = feedparser.parse(resp.text)
            for entry in (feed.entries or [])[:3]:
                title = (entry.get("title") or "").strip()
                if not title or len(title) < 15:
                    continue
                items.append({
                    "id":        _md5_id(title),
                    "category":  category,
                    "title":     title,
                    "details":   (entry.get("summary") or "").strip()[:300],
                    "url":       entry.get("link", ""),
                    "source":    _parse_source(entry),
                    "published": entry.get("published", ""),
                    "impact":    CATEGORIES[category][2],
                    "sentiment": None,
                })
    except Exception as exc:
        logger.debug("RSS fetch failed for query '%s': %s", query, exc)
    return items


async def _fetch_fii_dii() -> dict | None:
    """Fetch FII/DII trade data from NSE and return a structured news item."""
    try:
        from services.nse_service import _nse_get
        raw = await _nse_get("/api/fiidiiTradeReact")
        rows = raw.get("data") or []
        if not rows:
            return None
        row = rows[0]

        # Field names may vary — try multiple possibilities
        fii_net = None
        dii_net = None
        trade_date = ""

        for fii_key in ("fiiNetValue", "fii_netValue", "netValue", "FII_NET_BUY_SELL"):
            val = row.get(fii_key)
            if val is not None:
                try:
                    fii_net = float(str(val).replace(",", ""))
                    break
                except (ValueError, TypeError):
                    pass

        for dii_key in ("diiNetValue", "dii_netValue", "DII_NET_BUY_SELL"):
            val = row.get(dii_key)
            if val is not None:
                try:
                    dii_net = float(str(val).replace(",", ""))
                    break
                except (ValueError, TypeError):
                    pass

        for date_key in ("date", "tradeDate", "DATE"):
            val = row.get(date_key)
            if val:
                trade_date = str(val)
                break

        if fii_net is None and dii_net is None:
            return None

        fii_cr = round((fii_net or 0) / 1e7, 2) if fii_net is not None else None
        dii_cr = round((dii_net or 0) / 1e7, 2) if dii_net is not None else None

        def _flow_label(val, cr):
            if cr is None:
                return "N/A"
            abs_cr = abs(cr)
            action = "Buying" if cr >= 0 else "Selling"
            return f"{action} ₹{abs_cr:,.0f} Cr"

        fii_label = _flow_label(fii_net, fii_cr)
        dii_label = _flow_label(dii_net, dii_cr)
        title = f"FII {fii_label} | DII {dii_label}"

        # Sentiment based on FII net value
        if fii_net is not None and fii_net > 500e7:   # > ₹500 Cr
            sentiment = "Bullish"
        elif fii_net is not None and fii_net < -500e7: # < -₹500 Cr
            sentiment = "Bearish"
        else:
            sentiment = "Neutral"

        return {
            "id":        _md5_id(title),
            "category":  "FII_DII",
            "title":     title,
            "details":   f"Trade Date: {trade_date}" if trade_date else "",
            "url":       "https://www.nseindia.com/reports-indices-derivatives/fii-dii-data",
            "source":    "NSE India",
            "published": datetime.now().strftime("%a, %d %b %Y %H:%M:%S GMT"),
            "impact":    "HIGH",
            "sentiment": sentiment,
        }
    except Exception as exc:
        logger.debug("FII/DII fetch failed: %s", exc)
        return None


async def get_high_impact_news() -> list:
    """
    Return up to 20 high-impact market news items.
    FII/DII appears first, then items sorted by category priority.
    Results are cached for 5 minutes.
    """
    if "n" in _news_cache:
        return _news_cache["n"]

    # Fetch FII/DII and all RSS queries concurrently
    fii_task = _fetch_fii_dii()
    rss_tasks = [_fetch_rss_category(q, cat) for q, cat in _QUERIES]

    results = await asyncio.gather(fii_task, *rss_tasks, return_exceptions=True)

    fii_item   = results[0] if not isinstance(results[0], Exception) else None
    rss_results = results[1:]

    # Deduplicate by MD5 id
    seen_ids: set[str] = set()
    by_category: dict[str, list] = {cat: [] for cat in CATEGORIES}

    for result in rss_results:
        if isinstance(result, Exception):
            continue
        for item in (result or []):
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                by_category[item["category"]].append(item)

    # Build final list: FII/DII first, then by priority order
    final: list = []

    if fii_item and fii_item["id"] not in seen_ids:
        seen_ids.add(fii_item["id"])
        final.append(fii_item)

    for cat in _CATEGORY_PRIORITY:
        if cat == "FII_DII":
            continue  # already handled above
        for item in by_category.get(cat, []):
            if len(final) >= 20:
                break
            final.append(item)
        if len(final) >= 20:
            break

    _news_cache["n"] = final
    return final
