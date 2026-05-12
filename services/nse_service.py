import httpx
import asyncio
import logging
import json as _json
from typing import Optional
from datetime import date, datetime as _dt
from pathlib import Path as _Path
from config import NSE_BASE_URL, NSE_HEADERS, CACHE_TTL_FNO_LIST
from cachetools import TTLCache

logger = logging.getLogger(__name__)

# ── PERSISTENT OI CACHE (file-backed, survives restarts) ─────────────────
_OI_PERSIST_FILE = _Path(__file__).parent.parent / ".oi_last_session.json"
_oi_persist_mem: dict = {}   # in-memory mirror, survives TTLCache expiry

def _oi_persist_load() -> dict:
    global _oi_persist_mem
    if _oi_persist_mem:
        return _oi_persist_mem
    try:
        if _OI_PERSIST_FILE.exists():
            _oi_persist_mem = _json.loads(_OI_PERSIST_FILE.read_text())
    except Exception:
        pass
    return _oi_persist_mem

def _oi_persist_save(data: dict) -> None:
    global _oi_persist_mem
    _oi_persist_mem = data
    try:
        _OI_PERSIST_FILE.write_text(_json.dumps(data))
    except Exception as exc:
        logger.debug("OI persist save failed: %s", exc)

_fno_cache: TTLCache = TTLCache(maxsize=1,  ttl=CACHE_TTL_FNO_LIST)
_bhav_cache: TTLCache = TTLCache(maxsize=30, ttl=3600)

_session: Optional[httpx.AsyncClient] = None
_session_lock = asyncio.Lock()
_nse_semaphore = asyncio.Semaphore(2)   # max 2 concurrent NSE API requests


# ── SESSION MANAGEMENT ────────────────────────────────────────────────────

async def _get_session() -> httpx.AsyncClient:
    global _session
    async with _session_lock:
        if _session is None or _session.is_closed:
            headers = {**NSE_HEADERS, "Accept-Encoding": "gzip, deflate"}
            _session = httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20)
            try:
                await _session.get(f"{NSE_BASE_URL}/")
                await asyncio.sleep(0.5)
                await _session.get(f"{NSE_BASE_URL}/market-data/live-equity-market")
                await asyncio.sleep(0.3)
                # Warm option-chain page so cookies allow the API call
                await _session.get(f"{NSE_BASE_URL}/option-chain")
                await asyncio.sleep(0.3)
            except Exception:
                pass
    return _session


async def _nse_get(path: str, retries: int = 3) -> dict:
    global _session
    async with _nse_semaphore:
        for attempt in range(retries):
            try:
                session = await _get_session()
                resp = await session.get(
                    f"{NSE_BASE_URL}{path}",
                    headers={"Referer": f"{NSE_BASE_URL}/"},
                )
                if resp.status_code in (403, 429, 503):
                    async with _session_lock:
                        if _session and not _session.is_closed:
                            await _session.aclose()
                        _session = None
                    if attempt < retries - 1:
                        await asyncio.sleep(1.5)
                        continue
                    resp.raise_for_status()
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(1)
            except Exception as exc:
                if attempt == retries - 1:
                    raise
                logger.debug("NSE request error attempt %d for %s: %s", attempt, path, exc)
                await asyncio.sleep(1)
        return {}


# ── MARKET STATUS ─────────────────────────────────────────────────────────

async def get_market_status() -> dict:
    """Returns {'is_open': bool, 'trade_date': str, 'segments': dict}."""
    try:
        data = await _nse_get("/api/marketStatus")
        states = data.get("marketState", [])
        segments = {s.get("market", ""): s.get("marketStatus", "Closed") for s in states}
        trade_date = next(
            (s.get("tradeDate", "") for s in states if s.get("market") == "Capital Market"), ""
        )
        return {
            "is_open":    segments.get("Capital Market", "Closed") == "Open",
            "trade_date": trade_date,
            "segments":   segments,
        }
    except Exception as exc:
        logger.warning("Market status check failed: %s", exc)
        return {"is_open": False, "trade_date": "", "segments": {}}


# ── NSE EQUITY ENDPOINTS ──────────────────────────────────────────────────

async def get_equity_quote(symbol: str) -> dict:
    return await _nse_get(f"/api/quote-equity?symbol={symbol.upper()}")


async def get_trade_info(symbol: str) -> dict:
    return await _nse_get(f"/api/quote-equity?symbol={symbol.upper()}&section=trade_info")


async def get_fno_symbols() -> list[str]:
    if "fno" in _fno_cache:
        return _fno_cache["fno"]
    try:
        data = await _nse_get("/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O")
        symbols = [item["symbol"] for item in data.get("data", []) if "symbol" in item]
        if symbols:
            _fno_cache["fno"] = symbols
        return symbols
    except Exception as exc:
        logger.warning("Failed to fetch F&O list: %s", exc)
        return []


async def check_is_fno(symbol: str) -> bool:
    return symbol.upper() in await get_fno_symbols()


_INDEX_LABELS = {
    "NIFTY 50":           "Nifty 50",
    "NIFTY NEXT 50":      "Nifty Next 50",
    "NIFTY MIDCAP 100":   "Nifty Midcap 100",
    "NIFTY SMALLCAP 100": "Nifty Smallcap 100",
}

# Map NSE sector/industry strings → Nifty sector index name
_SECTOR_INDEX_MAP: dict[str, str] = {
    "bank":               "NIFTY BANK",
    "financial":          "NIFTY FIN SERVICE",
    "it":                 "NIFTY IT",
    "software":           "NIFTY IT",
    "information technology": "NIFTY IT",
    "pharma":             "NIFTY PHARMA",
    "healthcare":         "NIFTY PHARMA",
    "auto":               "NIFTY AUTO",
    "automobile":         "NIFTY AUTO",
    "metal":              "NIFTY METAL",
    "steel":              "NIFTY METAL",
    "oil":                "NIFTY ENERGY",
    "gas":                "NIFTY ENERGY",
    "energy":             "NIFTY ENERGY",
    "petroleum":          "NIFTY ENERGY",
    "fmcg":               "NIFTY FMCG",
    "consumer goods":     "NIFTY FMCG",
    "realty":             "NIFTY REALTY",
    "real estate":        "NIFTY REALTY",
    "media":              "NIFTY MEDIA",
    "telecom":            "NIFTY MEDIA",
    "infra":              "NIFTY INFRA",
    "infrastructure":     "NIFTY INFRA",
    "psu":                "NIFTY PSE",
    "power":              "NIFTY PSE",
    "cement":             "NIFTY INFRA",
    "chemical":           "NIFTY 500",
    "textile":            "NIFTY 500",
    "diversified":        "NIFTY 500",
}

_index_symbol_cache: TTLCache = TTLCache(maxsize=20, ttl=CACHE_TTL_FNO_LIST)
_all_indices_cache:  TTLCache = TTLCache(maxsize=1,  ttl=3600)   # 1-hour


async def _get_all_indices_pe() -> dict[str, float]:
    """Return {index_name: pe} dict from /api/allIndices (cached 1 h)."""
    if "all" in _all_indices_cache:
        return _all_indices_cache["all"]
    try:
        data = await _nse_get("/api/allIndices")
        pe_map: dict[str, float] = {}
        for row in data.get("data", []):
            name = row.get("index") or row.get("indexSymbol", "")
            pe_raw = row.get("pe")
            if name and pe_raw not in (None, "", "-"):
                try:
                    pe_map[name] = float(pe_raw)
                except (ValueError, TypeError):
                    pass
        _all_indices_cache["all"] = pe_map
        return pe_map
    except Exception as exc:
        logger.warning("allIndices PE fetch failed: %s", exc)
        return {}


async def get_sector_index_pe(sector: str, industry: str) -> tuple[float | None, str]:
    """
    Returns (sector_pe, index_name) for the given sector/industry.
    Looks up the best matching Nifty sector index from allIndices.
    """
    haystack = f"{sector} {industry}".lower()
    index_name = ""
    for keyword, idx in _SECTOR_INDEX_MAP.items():
        if keyword in haystack:
            index_name = idx
            break
    if not index_name:
        index_name = "NIFTY 500"   # broad market fallback

    pe_map = await _get_all_indices_pe()
    pe = pe_map.get(index_name)
    return pe, index_name


async def get_nifty_membership(symbol: str) -> list[str]:
    """Return list of major Nifty indices the symbol belongs to (cached 8 h)."""
    import urllib.parse
    symbol = symbol.upper()
    found: list[str] = []
    for index_key, label in _INDEX_LABELS.items():
        if index_key not in _index_symbol_cache:
            try:
                data = await _nse_get(
                    f"/api/equity-stockIndices?index={urllib.parse.quote(index_key)}"
                )
                syms = [item["symbol"] for item in data.get("data", []) if "symbol" in item]
                _index_symbol_cache[index_key] = syms
            except Exception:
                _index_symbol_cache[index_key] = []
        if symbol in _index_symbol_cache.get(index_key, []):
            found.append(label)
    return found


# ── LIVE OPTION CHAIN ─────────────────────────────────────────────────────

async def get_option_chain(symbol: str) -> dict:
    """Live option chain — only has data during market hours (9:15–15:30 IST)."""
    return await _nse_get(
        f"/api/option-chain-equities?symbol={symbol.upper()}",
    )


def parse_option_chain(raw: dict) -> dict:
    """Parse live NSE option chain response into a normalised dict."""
    if not raw:
        return {}

    records  = raw.get("records", {})
    filtered = raw.get("filtered", {})

    total_call_oi = filtered.get("CE", {}).get("totOI", 0) or 0
    total_put_oi  = filtered.get("PE", {}).get("totOI", 0) or 0
    if not total_call_oi and not total_put_oi:
        return {}   # truly empty — caller should fall back to bhavcopy

    pcr = round(total_put_oi / max(total_call_oi, 1), 2)

    oi_change     = filtered.get("CE", {}).get("totOIChng", 0) or 0
    prev_oi       = total_call_oi - oi_change
    oi_change_pct = round((oi_change / max(prev_oi, 1)) * 100, 2) if prev_oi else 0

    lot_size = 0
    try:
        lot_size = int(records.get("marketLotSize") or 0)
    except (TypeError, ValueError):
        pass

    return {
        "source":            "Live",
        "source_date":       "Live",
        "total_call_oi":     total_call_oi,
        "total_put_oi":      total_put_oi,
        "pcr_oi":            pcr,
        "oi_change_pct":     oi_change_pct,
        "expiry_dates":      records.get("expiryDates", []),
        "strike_prices":     records.get("strikePrices", []),
        "option_chain_data": records.get("data", []),
        "underlying_value":  records.get("underlyingValue", 0),
        "market_lot_size":   lot_size,
    }


# ── F&O EOD OI SPURTS (fallback when market is closed) ───────────────────

async def get_fno_bhavcopy_data(symbol: str) -> dict:
    """
    Fetch EOD F&O data from NSE live-analysis-oi-spurts-underlyings endpoint.
    Available any time — provides futures OI, OI change, underlying price.
    PCR is not available from this source (requires live option chain).
    """
    symbol = symbol.upper()
    cache_key = f"bhav_{symbol}"
    if cache_key in _bhav_cache:
        return _bhav_cache[cache_key]

    try:
        raw = await _nse_get("/api/live-analysis-oi-spurts-underlyings")
        data = raw.get("data", [])
        row = next((r for r in data if r.get("symbol") == symbol), None)
        if not row:
            logger.debug("OI spurts: no row for %s", symbol)
            return {}

        latest_oi  = int(row.get("latestOI", 0) or 0)
        prev_oi    = int(row.get("prevOI", 0) or 0)
        change_oi  = int(row.get("changeInOI", 0) or 0)
        oi_chg_pct = round((change_oi / max(prev_oi, 1)) * 100, 2) if prev_oi else 0
        trade_date = raw.get("currTradingDate", date.today().strftime("%d-%b-%Y"))

        result = {
            "source":         "EOD",
            "source_date":    trade_date,
            "total_fut_oi":   latest_oi,
            "oi_change":      change_oi,
            "oi_change_pct":  oi_chg_pct,
            "total_call_oi":  None,
            "total_put_oi":   None,
            "pcr_oi":         None,
            "futures_ltp":    float(row.get("underlyingValue", 0) or 0),
            "futures_settle": None,
            "expiry_date":    "",
            "expiry_dates":   [],
        }
        _bhav_cache[cache_key] = result
        logger.info("OI spurts loaded for %s (%s): FutOI=%s OIChg=%s", symbol, trade_date, latest_oi, change_oi)
        return result

    except Exception as exc:
        logger.warning("OI spurts fetch failed for %s: %s", symbol, exc)
        return {}


# ── HOME PAGE: INDEX QUOTES ───────────────────────────────────────────────

_index_quote_cache: TTLCache = TTLCache(maxsize=1, ttl=8)

async def get_index_quotes() -> dict:
    """Nifty 50, Bank Nifty, Sensex, Midcap 100 from allIndices."""
    if "q" in _index_quote_cache:
        return _index_quote_cache["q"]
    try:
        data = await _nse_get("/api/allIndices")
    except Exception:
        return {}

    TARGET = {
        "NIFTY 50":          "nifty50",
        "NIFTY BANK":        "banknifty",
        "NIFTY MIDCAP 100":  "midcap100",
        "NIFTY IT":          "niftyit",
        "S&P BSE SENSEX":    "sensex",
        "NIFTY NEXT 50":     "niftynext50",
        "INDIA VIX":         "indiavix",
    }
    result = {}
    for row in data.get("data", []):
        idx_name = row.get("index") or row.get("indexSymbol", "")
        key = TARGET.get(idx_name)
        if not key:
            continue
        price = row.get("last") or row.get("current") or row.get("previousClose")
        result[key] = {
            "name":    idx_name,
            "value":   price,
            "change":  row.get("variation") or row.get("change"),
            "pchange": row.get("percentChange") or row.get("changeInPer"),
            "open":    row.get("open"),
            "high":    row.get("high"),
            "low":     row.get("low"),
            "prev":    row.get("previousClose"),
        }
    # Append Bitcoin price from Yahoo Finance
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=8) as _c:
            _r = await _c.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?range=5d&interval=1d",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            _d = _r.json()
            _meta = _d.get("chart", {}).get("result", [{}])[0].get("meta", {})
            _closes = (
                _d.get("chart", {}).get("result", [{}])[0]
                .get("indicators", {}).get("quote", [{}])[0].get("close", [])
            )
            _closes = [c for c in _closes if c is not None]
            _price = _meta.get("regularMarketPrice") or (_closes[-1] if _closes else None)
            _prev  = _meta.get("chartPreviousClose") or (_closes[-2] if len(_closes) >= 2 else None)
            if _price:
                result["btc"] = {
                    "name":    "Bitcoin",
                    "value":   round(_price, 0),
                    "change":  round(_price - _prev, 0) if _prev else None,
                    "pchange": round((_price / _prev - 1) * 100, 2) if _prev else None,
                }
    except Exception:
        pass

    _index_quote_cache["q"] = result
    return result


# ── HOME PAGE: GAINERS / LOSERS ───────────────────────────────────────────

_movers_cache: TTLCache = TTLCache(maxsize=1, ttl=10)

async def get_market_movers(n: int = 7) -> dict:
    """Top N gainers and losers from the F&O universe."""
    if "m" in _movers_cache:
        return _movers_cache["m"]
    try:
        data = await _nse_get("/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O")
    except Exception:
        return {"gainers": [], "losers": []}

    stocks = [
        s for s in data.get("data", [])
        if s.get("symbol") and not s.get("symbol", "").upper().startswith("NIFTY")
    ]

    def extract(s):
        raw = s.get("lastPrice")
        return {
            "symbol":    s.get("symbol", ""),
            "lastPrice": float(str(raw).replace(",", "")) if raw is not None else None,
            "change":    s.get("change"),
            "pChange":   s.get("pChange"),
            "volume":    s.get("totalTradedVolume"),
        }

    ranked = sorted(stocks, key=lambda x: float(x.get("pChange") or 0), reverse=True)
    result = {
        "gainers": [extract(s) for s in ranked[:n]],
        "losers":  [extract(s) for s in reversed(ranked[-n:])],
    }
    _movers_cache["m"] = result
    return result


# ── BREAKOUT STOCKS (Nifty 500) ──────────────────────────────────────────

_breakout_cache: TTLCache = TTLCache(maxsize=1, ttl=300)  # 5-min cache


async def get_breakout_stocks() -> dict:
    """Nifty 500 stocks near 52W high (yearly) or top 15% of 52W range (monthly)."""
    if "b" in _breakout_cache:
        return _breakout_cache["b"]
    try:
        data = await _nse_get("/api/equity-stockIndices?index=NIFTY%20500")
    except Exception:
        return {"yearly": [], "monthly": []}

    yearly, monthly = [], []
    for s in data.get("data", []):
        sym = s.get("symbol", "")
        if not sym or sym.startswith("NIFTY"):
            continue
        price   = float(str(s.get("lastPrice", 0)).replace(",", "") or 0)
        yr_high = float(str(s.get("yearHigh",  0)).replace(",", "") or 0)
        yr_low  = float(str(s.get("yearLow",   0)).replace(",", "") or 0)
        pchange = float(s.get("pChange") or 0)

        if yr_high <= 0 or price <= 0:
            continue

        rng = yr_high - yr_low
        pos = (price - yr_low) / max(rng, 1)  # 0=52W low, 1=52W high

        entry = {
            "symbol":       sym,
            "lastPrice":    price,
            "pChange":      pchange,
            "yearHigh":     yr_high,
            "pct_from_high": round((yr_high - price) / yr_high * 100, 1),
            "range_pos":    round(pos * 100, 0),
        }
        if pos >= 0.97:                          # within 3% of 52W high
            yearly.append(entry)
        elif pos >= 0.85 and pchange > 0:        # top 15% of range, up today
            monthly.append(entry)

    yearly.sort(key=lambda x: x["pct_from_high"])
    monthly.sort(key=lambda x: x["range_pos"], reverse=True)
    result = {"yearly": yearly[:15], "monthly": monthly[:15]}
    _breakout_cache["b"] = result
    return result


# ── SECTOR ROTATION ───────────────────────────────────────────────────────

_SECTOR_NAMES = {
    "NIFTY IT":                    "IT",
    "NIFTY BANK":                  "Banking",
    "NIFTY PHARMA":                "Pharma",
    "NIFTY AUTO":                  "Auto",
    "NIFTY METAL":                 "Metal",
    "NIFTY FMCG":                  "FMCG",
    "NIFTY ENERGY":                "Energy",
    "NIFTY REALTY":                "Realty",
    "NIFTY MEDIA":                 "Media",
    "NIFTY INFRA":                 "Infra",
    "NIFTY PSE":                   "PSE",
    "NIFTY CONSUMER DURABLES":     "Cons Durables",
    "NIFTY HEALTHCARE INDEX":      "Healthcare",
    "NIFTY OIL & GAS":             "Oil & Gas",
    "NIFTY FINANCIAL SERVICES":    "Fin Services",
    "NIFTY MIDCAP SELECT":         "Midcap Sel",
}

_sector_cache: TTLCache = TTLCache(maxsize=1, ttl=30)


async def get_sector_rotation() -> list:
    """All sector indices sorted by day % change (high momentum to low)."""
    if "s" in _sector_cache:
        return _sector_cache["s"]
    try:
        data = await _nse_get("/api/allIndices")
    except Exception:
        return []

    sectors = []
    for row in data.get("data", []):
        name  = row.get("index") or row.get("indexSymbol", "")
        short = _SECTOR_NAMES.get(name)
        if not short:
            continue
        pch   = row.get("percentChange") or row.get("changeInPer") or 0
        price = row.get("last") or row.get("current") or 0
        sectors.append({
            "name":    name,
            "short":   short,
            "value":   price,
            "pchange": float(pch),
        })

    sectors.sort(key=lambda x: x["pchange"], reverse=True)
    _sector_cache["s"] = sectors
    return sectors


# ── HOME PAGE: F&O OI BUILDUP ─────────────────────────────────────────────

_buildup_cache: TTLCache = TTLCache(maxsize=1, ttl=10)

async def get_fno_oi_buildup(top_n: int = 10) -> dict:
    """Classify F&O stocks into Long/Short Buildup, Short/Long Covering."""
    if "b" in _buildup_cache:
        return _buildup_cache["b"]

    from utils.helpers import classify_oi_buildup
    try:
        oi_raw, price_raw = await asyncio.gather(
            _nse_get("/api/live-analysis-oi-spurts-underlyings"),
            _nse_get("/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"),
        )
    except Exception:
        return {"Long Buildup": [], "Short Buildup": [], "Short Covering": [], "Long Covering": []}

    price_map = {s.get("symbol"): s for s in price_raw.get("data", []) if s.get("symbol")}
    cats = {"Long Buildup": [], "Short Buildup": [], "Short Covering": [], "Long Covering": []}

    for row in oi_raw.get("data", []):
        sym = row.get("symbol")
        if not sym:
            continue
        prev_oi   = int(row.get("prevOI", 0) or 0)
        change_oi = int(row.get("changeInOI", 0) or 0)
        oi_chg_pct = round(change_oi / max(prev_oi, 1) * 100, 2) if prev_oi else 0

        p = price_map.get(sym, {})
        price_chg = float(p.get("pChange") or 0)
        signal = classify_oi_buildup(oi_chg_pct, price_chg)
        if signal in cats:
            raw_price = p.get("lastPrice") or row.get("underlyingValue") or 0
            cats[signal].append({
                "symbol":     sym,
                "lastPrice":  float(str(raw_price).replace(",", "") or 0),
                "pChange":    price_chg,
                "oi_chg_pct": oi_chg_pct,
            })

    for k in cats:
        cats[k] = sorted(cats[k], key=lambda x: abs(x["oi_chg_pct"]), reverse=True)[:top_n]

    _buildup_cache["b"] = cats
    return cats


# ── OI OPINION HELPER ────────────────────────────────────────────────────

def _compute_oi_opinion(strikes: list, underlying: float) -> dict:
    if not strikes:
        return {}
    total_ce = sum(s["ce_oi"] for s in strikes)
    total_pe = sum(s["pe_oi"] for s in strikes)
    pcr = round(total_pe / max(total_ce, 1), 2)
    max_ce = max(strikes, key=lambda x: x["ce_oi"])
    max_pe = max(strikes, key=lambda x: x["pe_oi"])
    ce_chg = sum(s["ce_chg"] for s in strikes)
    pe_chg = sum(s["pe_chg"] for s in strikes)

    if pcr > 1.3:
        sentiment = "Bullish"
    elif pcr > 1.0:
        sentiment = "Mildly Bullish"
    elif pcr < 0.7:
        sentiment = "Bearish"
    elif pcr < 1.0:
        sentiment = "Mildly Bearish"
    else:
        sentiment = "Neutral"

    writer_bias = (
        "Put writers active (bullish)" if pe_chg > ce_chg > 0
        else "Call writers active (bearish)" if ce_chg > pe_chg > 0
        else "Mixed writer activity"
    )

    # Fresh buildup = strike with highest positive OI change today (new positions)
    fresh_ce = max(strikes, key=lambda x: x["ce_chg"])
    fresh_pe = max(strikes, key=lambda x: x["pe_chg"])

    return {
        "pcr":              pcr,
        "sentiment":        sentiment,
        "support":          max_pe["strike"],
        "resistance":       max_ce["strike"],
        "writer_bias":      writer_bias,
        "total_ce_oi":      total_ce,
        "total_pe_oi":      total_pe,
        "fresh_resistance": fresh_ce["strike"] if fresh_ce["ce_chg"] > 0 else None,
        "fresh_support":    fresh_pe["strike"] if fresh_pe["pe_chg"] > 0 else None,
        "fresh_ce_chg":     fresh_ce["ce_chg"] if fresh_ce["ce_chg"] > 0 else 0,
        "fresh_pe_chg":     fresh_pe["pe_chg"] if fresh_pe["pe_chg"] > 0 else 0,
    }


# ── HOME PAGE: NIFTY OPTION CHAIN OI ─────────────────────────────────────

_nifty_oi_cache: TTLCache = TTLCache(maxsize=2, ttl=10)

def _parse_oc(oc_data: list, expiry: str, n: int = 5) -> list:
    """Extract top-N strikes by total OI for a given expiry."""
    sm: dict = {}
    for row in oc_data:
        if row.get("expiryDate") != expiry:
            continue
        sp = row.get("strikePrice", 0)
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        if sp not in sm:
            sm[sp] = {"strike": sp, "ce_oi": 0, "pe_oi": 0, "ce_chg": 0, "pe_chg": 0}
        sm[sp]["ce_oi"]  += int(ce.get("openInterest", 0) or 0)
        sm[sp]["pe_oi"]  += int(pe.get("openInterest", 0) or 0)
        sm[sp]["ce_chg"] += int(ce.get("changeinOpenInterest", 0) or 0)
        sm[sp]["pe_chg"] += int(pe.get("changeinOpenInterest", 0) or 0)
    for v in sm.values():
        v["total_oi"] = v["ce_oi"] + v["pe_oi"]
    return sorted(sm.values(), key=lambda x: x["total_oi"], reverse=True)[:n]


def _stale_fallback(symbol: str) -> dict:
    """Return last persisted OI data with a stale label, or {} if nothing cached."""
    cached = _oi_persist_load()
    if not cached or cached.get("symbol") != symbol:
        return {}
    raw_ts = cached.get("cached_at", "")
    try:
        label = _dt.fromisoformat(raw_ts).strftime("Last Session · %a %d %b %H:%M")
    except Exception:
        label = "Last Session"
    return {**cached, "is_stale": True, "stale_label": label}


async def _get_angel_oi_fallback(symbol: str) -> dict:
    """
    Try Angel One as OI data source when NSE returns empty (weekends/after-hours).
    Gets spot price from NSE allIndices (returns last close even on weekends).
    """
    try:
        from services.angel_service import get_angel_nifty_oi

        # Get last known spot price from NSE allIndices
        spot = 0.0
        try:
            idx_data = await _nse_get("/api/allIndices")
            for row in idx_data.get("data", []):
                name = row.get("index") or row.get("indexSymbol", "")
                if symbol == "NIFTY" and name == "NIFTY 50":
                    spot = float(row.get("last") or row.get("previousClose") or 0)
                    break
                elif symbol == "BANKNIFTY" and name == "NIFTY BANK":
                    spot = float(row.get("last") or row.get("previousClose") or 0)
                    break
        except Exception:
            pass

        result = await get_angel_nifty_oi(symbol=symbol, spot_price=spot)
        if not result or not result.get("weekly_strikes"):
            return {}

        # Add oi_opinion computed from weekly strikes
        result["oi_opinion"] = _compute_oi_opinion(
            result["weekly_strikes"], result["underlying"]
        )
        logger.info("Angel One OI fallback succeeded for %s (spot=%.0f)", symbol, spot)
        return result
    except Exception as exc:
        logger.debug("Angel One OI fallback failed for %s: %s", symbol, exc)
        return {}


async def get_nifty_oi_analysis(symbol: str = "NIFTY") -> dict:
    """Top 5 OI strikes for weekly and monthly expiry.
    Fallback chain: NSE live → Angel One → last session file cache."""
    ckey = symbol
    if ckey in _nifty_oi_cache:
        return _nifty_oi_cache[ckey]

    raw = {}
    try:
        raw = await _nse_get(f"/api/option-chain-indices?symbol={symbol}")
    except Exception:
        pass

    records      = raw.get("records", {})
    expiry_dates = records.get("expiryDates", [])
    oc_data      = records.get("data", [])
    underlying   = records.get("underlyingValue", 0)

    if not expiry_dates or not oc_data:
        # NSE empty — try Angel One, then file cache
        angel = await _get_angel_oi_fallback(symbol)
        if angel:
            _nifty_oi_cache[ckey] = angel
            return angel
        return _stale_fallback(symbol)

    weekly_exp      = expiry_dates[0]
    monthly_exp     = expiry_dates[1] if len(expiry_dates) > 1 else None
    weekly_strikes  = _parse_oc(oc_data, weekly_exp)
    monthly_strikes = _parse_oc(oc_data, monthly_exp) if monthly_exp else []

    if not weekly_strikes and not monthly_strikes:
        angel = await _get_angel_oi_fallback(symbol)
        if angel:
            _nifty_oi_cache[ckey] = angel
            return angel
        return _stale_fallback(symbol)

    result = {
        "symbol":           symbol,
        "underlying":       underlying,
        "weekly_expiry":    weekly_exp,
        "monthly_expiry":   monthly_exp,
        "weekly_strikes":   weekly_strikes,
        "monthly_strikes":  monthly_strikes,
        "all_expiry_dates": expiry_dates,
        "oi_opinion":       _compute_oi_opinion(weekly_strikes, underlying),
        "is_stale":         False,
        "stale_label":      "",
        "source":           "NSE",
    }
    _oi_persist_save({**result, "cached_at": _dt.now().isoformat()})
    _nifty_oi_cache[ckey] = result
    return result


async def get_nifty_oi_for_expiry(symbol: str = "NIFTY", expiry: str = "") -> dict:
    """Return top 5 OI strikes for a given expiry.
    Fallback chain: NSE live → Angel One → last session file cache."""
    raw = {}
    try:
        raw = await _nse_get(f"/api/option-chain-indices?symbol={symbol}")
    except Exception:
        pass

    records      = raw.get("records", {})
    expiry_dates = records.get("expiryDates", [])
    oc_data      = records.get("data", [])
    underlying   = records.get("underlyingValue", 0)

    if not expiry_dates or not oc_data:
        # Try Angel One first
        angel = await _get_angel_oi_fallback(symbol)
        if angel:
            # Map to the per-expiry format expected by the endpoint
            target_exp = expiry if expiry in angel.get("all_expiry_dates", []) else angel.get("weekly_expiry", "")
            if target_exp == angel.get("monthly_expiry"):
                strikes = angel.get("monthly_strikes", [])
            else:
                strikes = angel.get("weekly_strikes", [])
            return {
                "symbol":           symbol,
                "expiry":           target_exp,
                "underlying":       angel["underlying"],
                "all_expiry_dates": angel.get("all_expiry_dates", []),
                "strikes":          strikes,
                "oi_opinion":       _compute_oi_opinion(strikes, angel["underlying"]),
                "is_stale":         False,
                "stale_label":      "",
                "source":           "Angel One",
            }
        # Fall back to file cache
        cached = _oi_persist_load()
        if cached:
            target_exp = expiry if expiry in cached.get("all_expiry_dates", []) else cached.get("weekly_expiry", "")
            cached_strikes = cached.get("weekly_strikes", []) if target_exp == cached.get("weekly_expiry") else cached.get("monthly_strikes", [])
            raw_ts = cached.get("cached_at", "")
            try:
                label = _dt.fromisoformat(raw_ts).strftime("Last Session · %a %d %b %H:%M")
            except Exception:
                label = "Last Session"
            return {
                "symbol":           symbol,
                "expiry":           target_exp,
                "underlying":       cached.get("underlying", 0),
                "all_expiry_dates": cached.get("all_expiry_dates", []),
                "strikes":          cached_strikes,
                "oi_opinion":       _compute_oi_opinion(cached_strikes, cached.get("underlying", 0)),
                "is_stale":         True,
                "stale_label":      label,
                "source":           "Cache",
            }
        return {}

    target = expiry if expiry and expiry in expiry_dates else (expiry_dates[0] if expiry_dates else "")
    if not target:
        return {}

    strikes = _parse_oc(oc_data, target)
    return {
        "symbol":           symbol,
        "expiry":           target,
        "underlying":       underlying,
        "all_expiry_dates": expiry_dates,
        "strikes":          strikes,
        "oi_opinion":       _compute_oi_opinion(strikes, underlying),
        "is_stale":         False,
        "stale_label":      "",
        "source":           "NSE",
    }


# ── MULTI-TIMEFRAME SECTOR ROTATION (Yahoo Finance) ──────────────────────

_SECTOR_YF = {
    "IT":      "^CNXIT",
    "Banking": "^NSEBANK",
    "Pharma":  "^CNXPHARMA",
    "Auto":    "^CNXAUTO",
    "Metal":   "^CNXMETAL",
    "FMCG":    "^CNXFMCG",
    "Energy":  "^CNXENERGY",
    "Realty":  "^CNXREALTY",
    "Media":   "^CNXMEDIA",
    "Infra":   "^CNXINFRA",
}

_sec_multi_cache: TTLCache = TTLCache(maxsize=1, ttl=300)

async def get_sector_rotation_multi() -> list:
    """Sector returns for 1D/1W/1M/3M. Daily from NSE, longer periods from Yahoo Finance."""
    if "m" in _sec_multi_cache:
        return _sec_multi_cache["m"]

    # Get daily data from NSE allIndices first
    daily_map: dict[str, dict] = {}
    try:
        data = await _nse_get("/api/allIndices")
        for row in data.get("data", []):
            name  = row.get("index") or row.get("indexSymbol", "")
            short = _SECTOR_NAMES.get(name)
            if not short:
                continue
            daily_map[short] = {
                "name":        name,
                "short":       short,
                "value":       row.get("last") or row.get("current") or 0,
                "pchange_1d":  float(row.get("percentChange") or row.get("changeInPer") or 0),
                "pchange_1w":  None,
                "pchange_1m":  None,
                "pchange_3m":  None,
            }
    except Exception:
        pass

    # Fetch historical from Yahoo Finance for known sector symbols
    _yf_sem = asyncio.Semaphore(5)

    async def _yf_fetch(short: str, sym: str) -> tuple[str, dict]:
        encoded = sym.replace("^", "%5E")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=3mo&interval=1d"
        async with _yf_sem:
            try:
                async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as c:
                    r = await c.get(url)
                    d = r.json()
                    result = d.get("chart", {}).get("result", [None])[0]
                    if not result:
                        return short, {}
                    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    closes = [x for x in closes if x is not None]
                    if len(closes) < 5:
                        return short, {}
                    cur = closes[-1]
                    def _pct(n: int):
                        idx = len(closes) - 1 - n
                        return round((cur / closes[idx] - 1) * 100, 2) if idx >= 0 and closes[idx] else None
                    return short, {
                        "pchange_1w": _pct(5),
                        "pchange_1m": _pct(22),
                        "pchange_3m": _pct(len(closes) - 1),
                    }
            except Exception:
                return short, {}

    tasks = [_yf_fetch(short, sym) for short, sym in _SECTOR_YF.items()]
    yf_results = await asyncio.gather(*tasks)
    yf_map = dict(yf_results)

    result = []
    for short, entry in daily_map.items():
        yf = yf_map.get(short, {})
        result.append({**entry, **{k: v for k, v in yf.items() if v is not None}})

    result.sort(key=lambda x: x.get("pchange_1d", 0), reverse=True)
    _sec_multi_cache["m"] = result
    return result


# ── NEWS (delegated to news_service) ─────────────────────────────────────
from services.news_service import get_stock_news, get_policy_news


# ── NIFTY 500 OHLC (for candlestick pattern scanner) ─────────────────────

_nifty500_ohlc_cache: TTLCache = TTLCache(maxsize=1, ttl=60)


async def get_nifty500_ohlc() -> list[dict]:
    """Return Nifty 500 stocks with today's OHLC for candlestick pattern detection."""
    if "d" in _nifty500_ohlc_cache:
        return _nifty500_ohlc_cache["d"]
    try:
        data = await _nse_get("/api/equity-stockIndices?index=NIFTY%20500")
        stocks = []
        for row in data.get("data", []):
            sym = row.get("symbol", "")
            if not sym or sym.upper().startswith("NIFTY"):
                continue
            def _f(k):
                return float(str(row.get(k, 0) or 0).replace(",", ""))
            o = _f("open") or _f("previousClose")
            h = _f("dayHigh")
            l = _f("dayLow")
            c = _f("lastPrice")
            if h > 0 and l > 0 and h > l and o > 0 and c > 0:
                stocks.append({
                    "symbol":    sym,
                    "open":      o,
                    "high":      h,
                    "low":       l,
                    "close":     c,
                    "pchange":   float(row.get("pChange") or 0),
                    "volume":    int(str(row.get("totalTradedVolume", 0) or 0).replace(",", "") or 0),
                    "year_high": float(str(row.get("yearHigh", 0) or 0).replace(",", "")),
                    "year_low":  float(str(row.get("yearLow",  0) or 0).replace(",", "")),
                })
        _nifty500_ohlc_cache["d"] = stocks
        return stocks
    except Exception as exc:
        logger.warning("Nifty 500 OHLC fetch failed: %s", exc)
        return []
