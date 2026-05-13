"""
pivot_scanner_service.py
Live pivot breakout scanner for Nifty 200.

Pivot formulas (classic):
  PP = (H + L + C) / 3
  R1 = 2*PP - L        S1 = 2*PP - H
  R2 = PP + (H - L)    S2 = PP - (H - L)

Bullish signal : LTP > R1  (R1 broken — next target R2)
Bearish signal : LTP < S1  (S1 broken — next target S2)

Telegram alerts fire once per signal per day.
"""
import asyncio
import logging
from datetime import datetime, date, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    return datetime.now(_IST)

import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)

_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

_cache: TTLCache = TTLCache(maxsize=1, ttl=120)   # 2-minute result cache

# Alert dedup: "SYMBOL:bullish" / "SYMBOL:bearish", cleared daily
_alerted:    set[str]    = set()
_alert_date: date | None = None


def _calc_pivots(ph: float, pl: float, pc: float) -> dict:
    """Classic pivot levels including R2/S2 as next targets."""
    pp  = (ph + pl + pc) / 3
    rng = ph - pl
    r1  = 2 * pp - pl
    r2  = pp + rng
    s1  = 2 * pp - ph
    s2  = pp - rng
    return {
        "pp": round(pp, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
    }


async def _fetch_nifty200_live() -> list[dict]:
    """Fetch live Nifty 200 prices from NSE — all 200 in one request."""
    url = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20200"
    try:
        async with httpx.AsyncClient(headers=_NSE_HEADERS, timeout=15,
                                     follow_redirects=True) as c:
            await c.get("https://www.nseindia.com", timeout=10)
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        stocks = []
        for s in data.get("data", [])[1:]:    # row 0 = index itself
            sym = s.get("symbol", "")
            ltp = s.get("lastPrice", 0)
            if not sym or not ltp:
                continue
            stocks.append({
                "symbol":  sym,
                "ltp":     float(ltp),
                "pchange": float(s.get("pChange", 0)),
            })
        logger.info("Nifty 200 NSE fetch: %d stocks", len(stocks))
        return stocks
    except Exception as exc:
        logger.warning("Nifty 200 NSE fetch failed: %s", exc)
        return []


async def _yf_prev_ohlc(symbol: str, client: httpx.AsyncClient) -> dict | None:
    """Return previous session's H/L/C from Yahoo Finance 5-day 1d data."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
    try:
        r = await client.get(url, params={"interval": "1d", "range": "5d"})
        r.raise_for_status()
        data   = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        q      = result[0].get("indicators", {}).get("quote", [{}])[0]
        highs  = q.get("high",  [])
        lows   = q.get("low",   [])
        closes = q.get("close", [])
        if len(closes) < 2:
            return None
        ph, pl, pc = highs[-2], lows[-2], closes[-2]
        if None in (ph, pl, pc) or ph == 0:
            return None
        return {"prev_high": ph, "prev_low": pl, "prev_close": pc}
    except Exception:
        return None


async def scan_pivot_breakouts() -> dict:
    """
    Scan Nifty 200 for R1 breakouts (bullish) and S1 breakdowns (bearish).
    Cached 2 min. New signals trigger Telegram alert once per day per symbol.
    Returns R1/R2/S1/S2 pivot levels for each hit.
    """
    global _alerted, _alert_date

    if "r" in _cache:
        return _cache["r"]

    today = _now_ist().date()
    if _alert_date != today:
        _alerted    = set()
        _alert_date = today

    stocks = await _fetch_nifty200_live()
    if not stocks:
        return {
            "bullish": [], "bearish": [], "total_scanned": 0,
            "timestamp": datetime.now().isoformat(),
            "error": "NSE Nifty 200 fetch failed",
        }

    sem = asyncio.Semaphore(20)

    async def _analyze(s: dict, client: httpx.AsyncClient) -> dict | None:
        async with sem:
            ohlc = await _yf_prev_ohlc(s["symbol"], client)
            if not ohlc:
                return None
            pivots = _calc_pivots(
                ohlc["prev_high"], ohlc["prev_low"], ohlc["prev_close"]
            )
            ltp = s["ltp"]
            r1, r2 = pivots["r1"], pivots["r2"]
            s1, s2 = pivots["s1"], pivots["s2"]
            pp     = pivots["pp"]

            if ltp > r1:
                pct = round((ltp - r1) / r1 * 100, 2)
                return {
                    **s,
                    "pp": pp, "r1": r1, "r2": r2, "s1": s1, "s2": s2,
                    "signal": "bullish", "breakout_pct": pct,
                }
            if ltp < s1:
                pct = round((s1 - ltp) / s1 * 100, 2)
                return {
                    **s,
                    "pp": pp, "r1": r1, "r2": r2, "s1": s1, "s2": s2,
                    "signal": "bearish", "breakout_pct": pct,
                }
            return None

    async with httpx.AsyncClient(headers=_YF_HEADERS, timeout=12,
                                  follow_redirects=True) as client:
        raw = await asyncio.gather(*[_analyze(s, client) for s in stocks])

    hits    = [r for r in raw if r]
    bullish = sorted(
        [h for h in hits if h["signal"] == "bullish"],
        key=lambda x: -x["breakout_pct"],
    )
    bearish = sorted(
        [h for h in hits if h["signal"] == "bearish"],
        key=lambda x: -x["breakout_pct"],
    )

    # Telegram alerts — R1 breakout only, before 11:00 AM IST, no duplicates
    now_ist = _now_ist()
    alert_window = now_ist.hour < 11
    new_alerts = []
    for h in hits:
        if h["signal"] != "bullish":
            continue
        key = f"{h['symbol']}:r1"
        if key not in _alerted:
            _alerted.add(key)
            if alert_window:
                new_alerts.append(h)
    if new_alerts:
        asyncio.create_task(_send_pivot_alerts(new_alerts))

    result = {
        "bullish":       bullish,
        "bearish":       bearish,
        "total_scanned": len(stocks),
        "timestamp":     _now_ist().isoformat(),
    }
    _cache["r"] = result
    logger.info("Pivot scan: %d bullish, %d bearish / %d scanned",
                len(bullish), len(bearish), len(stocks))
    return result


async def _send_pivot_alerts(alerts: list[dict]):
    """Send R1 breakout alerts to Telegram. One alert per symbol per day, before 11 AM only."""
    from services.telegram_service import send_message

    now  = _now_ist().strftime("%H:%M IST")
    lines = [f"📡 <b>R1 Breakout Alert · Nifty 200</b>  <i>{now}</i>", ""]
    lines.append("🟢 <b>R1 BREAKOUT — Bullish</b>")

    for a in alerts:
        lines.append(
            f"  <b>{a['symbol']}</b>   LTP ₹{a['ltp']:,.2f}\n"
            f"  R1 Breached: ₹{a['r1']:,.2f}  (+{a['breakout_pct']:.2f}% above R1)\n"
            f"  Next Target R2: ₹{a['r2']:,.2f}   PP: ₹{a['pp']:,.2f}"
        )

    lines.append("")
    lines.append("<i>One alert per stock per day · Before 11:00 AM only</i>")

    try:
        await send_message("\n".join(lines))
    except Exception as exc:
        logger.warning("Pivot Telegram alert failed: %s", exc)
