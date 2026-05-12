import httpx
import asyncio
import logging
from cachetools import TTLCache

logger = logging.getLogger(__name__)

_cache: TTLCache = TTLCache(maxsize=1, ttl=30)

SYMBOLS: dict[str, dict] = {
    # US Indices
    "^GSPC":     {"name": "S&P 500",       "region": "US"},
    "^DJI":      {"name": "Dow Jones",     "region": "US"},
    "^IXIC":     {"name": "NASDAQ",        "region": "US"},
    "^VIX":      {"name": "VIX",           "region": "US"},
    # US Futures
    "ES=F":      {"name": "S&P 500 Fut",   "region": "US Futures"},
    "NQ=F":      {"name": "NASDAQ Fut",    "region": "US Futures"},
    "YM=F":      {"name": "Dow Fut",       "region": "US Futures"},
    # Europe
    "^FTSE":     {"name": "FTSE 100",      "region": "Europe"},
    "^GDAXI":    {"name": "DAX",           "region": "Europe"},
    "^FCHI":     {"name": "CAC 40",        "region": "Europe"},
    "^STOXX50E": {"name": "Euro Stoxx 50", "region": "Europe"},
    # Asia
    "^N225":     {"name": "Nikkei 225",    "region": "Asia"},
    "^HSI":      {"name": "Hang Seng",     "region": "Asia"},
    "000001.SS": {"name": "Shanghai",      "region": "Asia"},
    "^STI":      {"name": "SGX Singapore", "region": "Asia"},
    "^KS11":     {"name": "KOSPI",         "region": "Asia"},
    "^AXJO":     {"name": "ASX 200",       "region": "Asia"},
    # Commodities
    "GC=F":      {"name": "Gold",          "region": "Commodities"},
    "CL=F":      {"name": "Crude Oil",     "region": "Commodities"},
    "SI=F":      {"name": "Silver",        "region": "Commodities"},
    "NG=F":      {"name": "Nat Gas",       "region": "Commodities"},
    # Forex
    "USDINR=X":  {"name": "USD/INR",       "region": "Forex"},
    "DX-Y.NYB":  {"name": "DXY (USD Idx)", "region": "Forex"},
    "EURINR=X":  {"name": "EUR/INR",       "region": "Forex"},
    # Crypto
    "BTC-USD":   {"name": "Bitcoin",       "region": "Crypto"},
}

REGION_ORDER = ["US", "US Futures", "Europe", "Asia", "Commodities", "Forex"]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
_sem = asyncio.Semaphore(6)  # max 6 concurrent Yahoo requests


async def _fetch_one(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """Fetch one symbol via Yahoo Finance v8 chart API (free, no auth)."""
    encoded = symbol.replace("^", "%5E").replace("=", "%3D").replace(".", "%2E")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d"
    async with _sem:
        try:
            r = await client.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            result = data.get("chart", {}).get("result", [None])[0]
            if not result:
                return None
            meta   = result.get("meta", {})
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            closes = [c for c in closes if c is not None]

            price    = meta.get("regularMarketPrice")
            prev_cls = meta.get("chartPreviousClose")

            # Prefer chartPreviousClose; fall back to second-to-last close
            if price is None and closes:
                price = closes[-1]
            if prev_cls is None and len(closes) >= 2:
                prev_cls = closes[-2]

            if price is None:
                return None

            change  = round(price - prev_cls, 2)              if prev_cls else None
            pchange = round((price / prev_cls - 1) * 100, 2)  if prev_cls else None

            meta_sym = SYMBOLS.get(symbol, {})
            return {
                "name":    meta_sym.get("name", symbol),
                "region":  meta_sym.get("region", "Other"),
                "price":   round(price, 2),
                "change":  change,
                "pchange": pchange,
            }
        except Exception as exc:
            logger.debug("Global fetch failed for %s: %s", symbol, exc)
            return None


async def get_global_markets() -> dict:
    if "data" in _cache:
        return _cache["data"]

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=15) as client:
        tasks = [_fetch_one(client, sym) for sym in SYMBOLS]
        results = await asyncio.gather(*tasks)

    out: dict[str, dict] = {}
    for sym, res in zip(SYMBOLS.keys(), results):
        if res:
            out[sym] = res

    if out:
        _cache["data"] = out
    return out
