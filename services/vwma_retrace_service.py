"""
vwma_retrace_service.py
Live 2:45 PM scan — "VWMA(20) Retraces" on Nifty 500, DAILY timeframe.

Logic:
  - VWMA(20) is computed from the last 20 COMPLETED daily candles (via Yahoo Finance)
  - "Current candle" = today's live O/H/L/C from NSE (already fetched in pm_scan_and_send)
  - All conditions evaluated on the daily chart

Conditions (all must pass):
  1. Today's low retraced to VWMA(20)   — low <= VWMA * 1.01  (within 1%)
  2. VWMA acts as dynamic support        — previous 2 completed days closed above VWMA
  3. Today's candle (live, partial) is:  Hammer | Dragonfly Doji | Bullish Engulfing | Pin Bar
  4. Lower wick > body
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

_VWMA_PERIOD      = 20
_SUPPORT_LOOKBACK = 2    # completed days that must close above VWMA
_TOUCH_THRESHOLD  = 0.01  # today's low within 1% above VWMA counts as a touch
_CLOSE_THRESHOLD  = 0.01  # today's price up to 1% below VWMA still allowed


# ── Yahoo Finance daily fetch ─────────────────────────────────────────────────

async def _fetch_daily(symbol: str, client: httpx.AsyncClient) -> dict | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
    try:
        r = await client.get(url, params={"interval": "1d", "range": "35d"}, timeout=12)
        r.raise_for_status()
        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return None
        q = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes  = [x for x in (q.get("close")  or []) if x is not None]
        volumes = [x for x in (q.get("volume") or []) if x is not None]
        opens   = [x for x in (q.get("open")   or []) if x is not None]
        n = min(len(closes), len(volumes), len(opens))
        # Need at least 20 completed days + support lookback days
        if n < _VWMA_PERIOD + _SUPPORT_LOOKBACK + 1:
            return None
        return {"closes": closes[:n], "volumes": volumes[:n], "opens": opens[:n]}
    except Exception:
        return None


# ── VWMA from last N completed days ──────────────────────────────────────────

def _vwma(closes: list, volumes: list, n: int = _VWMA_PERIOD) -> float | None:
    if len(closes) < n:
        return None
    c = closes[-n:]
    v = volumes[-n:]
    tv = sum(v)
    return sum(c[i] * v[i] for i in range(n)) / tv if tv > 0 else None


# ── Pattern detection on today's live daily candle ───────────────────────────

def _detect_daily_pattern(
    o: float, h: float, l: float, c: float,
    prev_o: float | None = None, prev_c: float | None = None,
) -> list[str]:
    hl = h - l
    if hl < 0.001:
        return []

    body        = abs(c - o)
    upper_wick  = h - max(o, c)
    lower_wick  = min(o, c) - l
    body_pct    = body / hl

    # Condition 4: lower wick must be larger than body
    if lower_wick <= body:
        return []

    patterns: list[str] = []

    # Hammer: small body near top, lower wick >= 1.8× body, tiny upper wick
    if (body_pct < 0.40
            and lower_wick >= 1.8 * max(body, 0.001)
            and upper_wick <= 0.20 * hl
            and min(o, c) >= l + 0.40 * hl):
        patterns.append("Hammer")

    # Dragonfly Doji: near-zero body, long lower wick
    if body_pct < 0.07 and lower_wick / hl >= 0.60:
        patterns.append("Dragonfly Doji")

    # Pin Bar: lower wick >= 55% of range, close near top
    if (lower_wick / hl >= 0.55
            and upper_wick / hl <= 0.30
            and "Hammer" not in patterns):
        patterns.append("Pin Bar")

    # Bullish Engulfing: current bull candle engulfs a previous bearish candle
    if (prev_o is not None and prev_c is not None
            and c > o              # today is bullish
            and prev_c < prev_o   # yesterday was bearish
            and o <= prev_c       # today opens at or below yesterday's close
            and c >= prev_o):     # today closes at or above yesterday's open
        patterns.append("Bullish Engulfing")

    return patterns


# ── Scanner ───────────────────────────────────────────────────────────────────

async def scan_vwma_retraces(stocks: list[dict]) -> list[dict]:
    """
    Scan Nifty 500 for daily VWMA(20) retrace with reversal candle.

    `stocks` must be today's live NSE OHLC data (from get_nifty500_ohlc()).
    Historical VWMA is fetched from Yahoo Finance daily.

    Returns list sorted by wick_pct descending (strongest lower wick first).
    """
    if not stocks:
        return []

    # Build lookup: symbol → today's live NSE candle
    live: dict[str, dict] = {s["symbol"]: s for s in stocks if s.get("symbol")}
    symbols = list(live.keys())
    logger.info("VWMA Daily Retrace scan: %d symbols", len(symbols))

    sem = asyncio.Semaphore(25)

    async def _check(sym: str, client: httpx.AsyncClient) -> dict | None:
        async with sem:
            hist = await _fetch_daily(sym, client)
            if not hist:
                return None

            closes  = hist["closes"]
            volumes = hist["volumes"]
            opens   = hist["opens"]

            # At 2:45 PM, Yahoo Finance daily likely includes today's partial candle
            # as the last entry. Use all entries EXCEPT the last as completed history.
            # hist_* = completed trading days (yesterday and before)
            hist_c = closes[:-1]
            hist_v = volumes[:-1]
            hist_o = opens[:-1]

            if len(hist_c) < _VWMA_PERIOD + _SUPPORT_LOOKBACK:
                return None

            # VWMA(20) from last 20 completed days
            vwma = _vwma(hist_c, hist_v)
            if not vwma:
                return None

            # Condition 2: previous N completed days all closed above VWMA
            for i in range(1, _SUPPORT_LOOKBACK + 1):
                if hist_c[-i] <= vwma:
                    return None

            # Today's live candle from NSE
            today = live.get(sym)
            if not today:
                return None

            o = float(today.get("open",  0) or 0)
            h = float(today.get("high",  0) or 0)
            l = float(today.get("low",   0) or 0)
            c = float(today.get("close", 0) or 0)
            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                return None

            # Condition 1: today's low retraced to VWMA (within touch threshold)
            if l > vwma * (1 + _TOUCH_THRESHOLD):
                return None  # low never came close to VWMA

            # Current price shouldn't have broken well below VWMA
            if c < vwma * (1 - _CLOSE_THRESHOLD):
                return None

            # Yesterday's candle for Bullish Engulfing check
            prev_o = hist_o[-1] if hist_o else None
            prev_c = hist_c[-1] if hist_c else None

            # Conditions 3 + 4: pattern detection
            patterns = _detect_daily_pattern(o, h, l, c, prev_o, prev_c)
            if not patterns:
                return None

            hl         = h - l
            body       = abs(c - o)
            lower_wick = min(o, c) - l
            body_pct   = round(body / hl * 100, 1) if hl > 0 else 0
            wick_pct   = round(lower_wick / hl * 100, 1) if hl > 0 else 0
            pchange    = float(today.get("pchange", 0) or 0)
            # How far did the low go below VWMA (negative = below, positive = above)
            touch_pct  = round((l - vwma) / vwma * 100, 2)

            return {
                "symbol":   sym,
                "patterns": patterns,
                "ltp":      round(c, 2),
                "vwma":     round(vwma, 2),
                "open":     round(o, 2),
                "high":     round(h, 2),
                "low":      round(l, 2),
                "pchange":  round(pchange, 2),
                "body_pct": body_pct,
                "wick_pct": wick_pct,
                "touch_pct": touch_pct,
            }

    async with httpx.AsyncClient(headers=_YF_HEADERS, follow_redirects=True) as client:
        raw = await asyncio.gather(*[_check(s, client) for s in symbols])

    hits = sorted([r for r in raw if r], key=lambda x: -x["wick_pct"])
    logger.info("VWMA Daily Retrace scan: %d hits / %d scanned", len(hits), len(symbols))
    return hits
