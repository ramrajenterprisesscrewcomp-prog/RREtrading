"""
pivot_scanner_service.py
Live pivot breakout scanner — Nifty 200 daily.

scan_pivot_breakouts() : pure data scan — no alerts, safe for API use
pivot_alert_loop()     : background-only loop that owns all alert state
"""
import asyncio
import logging
from datetime import datetime, date, timezone, timedelta

import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    return datetime.now(_IST)

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

_cache: TTLCache = TTLCache(maxsize=1, ttl=55)


# ── Pivot math ────────────────────────────────────────────────────────────────

def _calc_pivots(ph: float, pl: float, pc: float) -> dict:
    pp  = (ph + pl + pc) / 3
    rng = ph - pl
    return {
        "pp": round(pp, 2),
        "r1": round(2 * pp - pl, 2),
        "r2": round(pp + rng, 2),
        "s1": round(2 * pp - ph, 2),
        "s2": round(pp - rng, 2),
    }


# ── Data fetchers ─────────────────────────────────────────────────────────────

async def _fetch_nifty200_live() -> list[dict]:
    url = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20200"
    try:
        async with httpx.AsyncClient(headers=_NSE_HEADERS, timeout=15,
                                     follow_redirects=True) as c:
            await c.get("https://www.nseindia.com", timeout=10)
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        stocks = []
        for s in data.get("data", [])[1:]:
            sym = s.get("symbol", "")
            ltp = s.get("lastPrice", 0)
            if sym and ltp:
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
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
    try:
        r = await client.get(url, params={"interval": "1d", "range": "5d"})
        r.raise_for_status()
        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return None
        q = result[0].get("indicators", {}).get("quote", [{}])[0]
        highs, lows, closes = q.get("high", []), q.get("low", []), q.get("close", [])
        if len(closes) < 2:
            return None
        ph, pl, pc = highs[-2], lows[-2], closes[-2]
        if None in (ph, pl, pc) or ph == 0:
            return None
        return {"prev_high": ph, "prev_low": pl, "prev_close": pc}
    except Exception:
        return None


# ── Pure scan — NO alert logic, safe to call from API endpoints ──────────────

async def scan_pivot_breakouts() -> dict:
    """Return R1/S1 breakout data for Nifty 200. No Telegram side-effects."""
    if "r" in _cache:
        return _cache["r"]

    stocks = await _fetch_nifty200_live()
    if not stocks:
        return {
            "bullish": [], "bearish": [], "total_scanned": 0,
            "timestamp": _now_ist().isoformat(),
            "error": "NSE Nifty 200 fetch failed",
        }

    sem = asyncio.Semaphore(20)

    async def _analyze(s: dict, client: httpx.AsyncClient) -> dict | None:
        async with sem:
            ohlc = await _yf_prev_ohlc(s["symbol"], client)
            if not ohlc:
                return None
            piv = _calc_pivots(ohlc["prev_high"], ohlc["prev_low"], ohlc["prev_close"])
            ltp = s["ltp"]
            if ltp > piv["r1"]:
                return {**s, **piv, "signal": "bullish",
                        "breakout_pct": round((ltp - piv["r1"]) / piv["r1"] * 100, 2)}
            if ltp < piv["s1"]:
                return {**s, **piv, "signal": "bearish",
                        "breakout_pct": round((piv["s1"] - ltp) / piv["s1"] * 100, 2)}
            return None

    async with httpx.AsyncClient(headers=_YF_HEADERS, timeout=12,
                                  follow_redirects=True) as client:
        raw = await asyncio.gather(*[_analyze(s, client) for s in stocks])

    hits    = [r for r in raw if r]
    bullish = sorted([h for h in hits if h["signal"] == "bullish"], key=lambda x: -x["breakout_pct"])
    bearish = sorted([h for h in hits if h["signal"] == "bearish"], key=lambda x: -x["breakout_pct"])

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


# ── Alert sender ──────────────────────────────────────────────────────────────

async def _send_pivot_alerts(alerts: list[dict]) -> None:
    from services.telegram_service import send_message
    for a in alerts:
        now_ist = _now_ist()
        if now_ist.hour >= 11:
            break                                         # hard cutoff — never send after 11 AM
        msg = (
            f"📡 <b>R1 Breakout · {a['symbol']}</b>  "
            f"<i>{now_ist.strftime('%H:%M IST')}</i>\n\n"
            f"🟢 <b>BUY SIGNAL — R1 Breached</b>\n"
            f"LTP: ₹{a['ltp']:,.2f}  (+{a['breakout_pct']:.2f}% above R1)\n"
            f"R1: ₹{a['r1']:,.2f}   Target R2: ₹{a['r2']:,.2f}\n"
            f"PP: ₹{a['pp']:,.2f}   Chg: {a['pchange']:+.2f}%\n\n"
            f"<i>Nifty 200 · One alert per stock per day</i>"
        )
        try:
            await send_message(msg)
        except Exception as exc:
            logger.warning("Pivot alert failed [%s]: %s", a["symbol"], exc)


# ── Background loop — owns ALL alert state, never shared with API ─────────────

async def pivot_alert_loop() -> None:
    """
    Runs every 60 s on weekdays 9:15–11:00 AM IST.
    All alert dedup state is LOCAL to this function — API calls never trigger alerts.
    """
    logger.info("Pivot alert loop started")
    await asyncio.sleep(30)

    alerted:         set[str]    = set()
    alert_date:      date | None = None
    prev_above_r1:   set[str]    = set()
    baseline_seeded: bool        = False

    while True:
        now     = _now_ist()
        h, m    = now.hour, now.minute
        in_window = now.weekday() < 5 and ((h == 9 and m >= 15) or h == 10)

        if in_window:
            # Reset state at start of each new day
            today = now.date()
            if alert_date != today:
                alerted         = set()
                alert_date      = today
                prev_above_r1   = set()
                baseline_seeded = False

            try:
                data    = await scan_pivot_breakouts()
                bullish = data.get("bullish", [])
                current_above_r1 = {s["symbol"] for s in bullish}

                if not baseline_seeded:
                    # First scan of the day — record state, send NO alerts
                    prev_above_r1   = current_above_r1
                    baseline_seeded = True
                    logger.info("Pivot baseline seeded: %d above R1", len(prev_above_r1))
                else:
                    newly_crossed = current_above_r1 - prev_above_r1
                    prev_above_r1 = current_above_r1

                    new_alerts = [
                        s for s in bullish
                        if s["symbol"] in newly_crossed
                        and s["symbol"] not in alerted
                    ]
                    for s in new_alerts:
                        alerted.add(s["symbol"])

                    if new_alerts:
                        logger.info("Pivot: %d new R1 crossovers", len(new_alerts))
                        await _send_pivot_alerts(new_alerts)

            except Exception as exc:
                logger.warning("Pivot alert loop error: %s", exc)

        await asyncio.sleep(60)
