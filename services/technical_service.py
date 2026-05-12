"""
Technical analysis: RSI(14), EMA20, EMA50, Supertrend(10,3)
OHLCV source: Yahoo Finance (.NS suffix) — no auth required.
"""
import httpx
import asyncio
import logging

logger = logging.getLogger(__name__)


_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


async def _fetch_ohlcv(symbol: str, range_: str = "1y") -> list[dict]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
    try:
        async with httpx.AsyncClient(headers=_YF_HEADERS, timeout=15, follow_redirects=True) as c:
            r = await c.get(url, params={"interval": "1d", "range": range_})
            r.raise_for_status()
            data = r.json()

        result = data.get("chart", {}).get("result", [])
        if not result:
            return []
        chart = result[0]
        ts = chart.get("timestamp", [])
        q  = chart.get("indicators", {}).get("quote", [{}])[0]

        candles = []
        for i in range(len(ts)):
            try:
                o = q["open"][i]
                h = q["high"][i]
                l = q["low"][i]
                c = q["close"][i]
                v = (q.get("volume") or [0])[i] or 0
            except (IndexError, KeyError):
                continue
            if None not in (o, h, l, c):
                # Store date string (YYYY-MM-DD) for alignment with Nifty
                date = str(ts[i] // 86400)
                candles.append({"open": o, "high": h, "low": l, "close": c,
                                 "volume": v, "_d": date})
        return candles

    except Exception as exc:
        logger.warning("Yahoo Finance OHLCV failed for %s: %s", symbol, exc)
        return []


async def _fetch_nifty_closes(range_: str = "1y") -> dict[str, float]:
    """Returns {day_key: close} where day_key = str(unix_ts // 86400)."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI"
    try:
        async with httpx.AsyncClient(headers=_YF_HEADERS, timeout=15, follow_redirects=True) as c:
            r = await c.get(url, params={"interval": "1d", "range": range_})
            r.raise_for_status()
            data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return {}
        chart = result[0]
        ts = chart.get("timestamp", [])
        q  = chart.get("indicators", {}).get("quote", [{}])[0]
        closes = q.get("close") or []
        return {
            str(ts[i] // 86400): closes[i]
            for i in range(min(len(ts), len(closes)))
            if closes[i] is not None
        }
    except Exception as exc:
        logger.warning("Nifty 50 fetch for beta failed: %s", exc)
        return {}


def _calc_beta(candles: list[dict], nifty_map: dict[str, float]) -> float | None:
    """Beta vs Nifty 50, date-aligned to avoid misalignment from differing candle counts."""
    pairs = [
        (c["close"], nifty_map[c["_d"]])
        for c in candles
        if c.get("_d") and c["_d"] in nifty_map
    ]
    if len(pairs) < 30:
        return None
    s_closes = [p[0] for p in pairs]
    m_closes = [p[1] for p in pairs]
    n = len(s_closes)
    s_ret = [(s_closes[i] - s_closes[i-1]) / s_closes[i-1] for i in range(1, n)]
    m_ret = [(m_closes[i] - m_closes[i-1]) / m_closes[i-1] for i in range(1, n)]
    k      = len(s_ret)
    mean_s = sum(s_ret) / k
    mean_m = sum(m_ret) / k
    cov = sum((s - mean_s) * (m - mean_m) for s, m in zip(s_ret, m_ret)) / k
    var = sum((m - mean_m) ** 2 for m in m_ret) / k
    if var < 1e-12:
        return None
    return round(cov / var, 2)


def _rsi(closes: list, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains  = [max(closes[i] - closes[i-1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    return 100.0 if avg_l == 0 else round(100 - 100 / (1 + avg_g / avg_l), 2)


def _ema(closes: list, period: int) -> float | None:
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    val = sum(closes[:period]) / period     # seed with SMA
    for p in closes[period:]:
        val = p * k + val * (1 - k)
    return round(val, 2)


def _supertrend(highs: list, lows: list, closes: list,
                period: int = 10, mult: float = 3.0) -> dict | None:
    n = len(closes)
    if n < period + 2:
        return None

    # Wilder-smoothed ATR
    tr0 = highs[0] - lows[0]
    trs = [tr0] + [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1]))
        for i in range(1, n)
    ]
    atrs: list[float] = []
    seed = sum(trs[:period]) / period
    atrs.append(seed)
    for tr in trs[period:]:
        atrs.append((atrs[-1] * (period - 1) + tr) / period)

    # Bands + direction (atrs[i] aligns with closes[period-1+i])
    prev_ub = prev_lb = 0.0
    direction = 1
    st_val = 0.0

    for i, atr in enumerate(atrs):
        ci  = period - 1 + i
        hl2 = (highs[ci] + lows[ci]) / 2
        b_ub = hl2 + mult * atr
        b_lb = hl2 - mult * atr

        ub = b_ub if (i == 0 or b_ub < prev_ub or closes[ci-1] > prev_ub) else prev_ub
        lb = b_lb if (i == 0 or b_lb > prev_lb or closes[ci-1] < prev_lb) else prev_lb

        if i == 0:
            direction = 1
        elif closes[ci] > prev_ub:
            direction = 1
        elif closes[ci] < prev_lb:
            direction = -1

        st_val   = lb if direction == 1 else ub
        prev_ub, prev_lb = ub, lb

    return {
        "value":     round(st_val, 2),
        "direction": "Bullish" if direction == 1 else "Bearish",
    }


async def get_technical_data(symbol: str) -> dict:
    """
    Returns RSI(14), EMA20, EMA50, Supertrend(10,3), Beta(1Y vs Nifty 50).
    Fetches stock and Nifty data in parallel; date-aligns them for accurate beta.
    """
    candles, nifty_map = await asyncio.gather(
        _fetch_ohlcv(symbol, "1y"),
        _fetch_nifty_closes("1y"),
    )
    if len(candles) < 16:
        logger.warning("Insufficient OHLCV data for %s (%d candles)", symbol, len(candles))
        return {}

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]

    return {
        "rsi":         _rsi(closes),
        "ema20":       _ema(closes, 20),
        "ema50":       _ema(closes, 50),
        "supertrend":  _supertrend(highs, lows, closes),
        "beta":        _calc_beta(candles, nifty_map),
        "last_close":  round(closes[-1], 2),
        "data_points": len(candles),
    }
