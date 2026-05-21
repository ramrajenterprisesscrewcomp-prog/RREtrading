"""
technical_analysis.py — Candlestick pattern detection, RSI, VWMA, volume ratio.
All functions are pure (no I/O) and work on lists of OHLCV dicts.
"""


def detect_patterns(ohlcv: list[dict]) -> list[str]:
    """
    Detect candlestick patterns from last 3 candles.
    ohlcv: [{open, high, low, close, volume}, ...] oldest→newest
    Returns list of pattern names found.
    """
    if not ohlcv:
        return []
    patterns = []

    c  = ohlcv[-1]
    o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
    body   = abs(cl - o)
    rng    = h - l
    bull   = cl >= o

    if rng < 0.001:
        return patterns

    upper_shadow = h - max(o, cl)
    lower_shadow = min(o, cl) - l

    # ── Single-candle patterns ─────────────────────────────────────────────
    if body / rng < 0.1:
        patterns.append("Doji")

    if body > 0 and lower_shadow >= 2 * body and upper_shadow <= 0.5 * body:
        patterns.append("Hammer" if bull else "Hanging Man")

    if body > 0 and upper_shadow >= 2 * body and lower_shadow <= 0.5 * body:
        patterns.append("Inverted Hammer" if bull else "Shooting Star")

    # ── Two-candle patterns ────────────────────────────────────────────────
    if len(ohlcv) >= 2:
        p   = ohlcv[-2]
        po, ph, pl, pcl = float(p["open"]), float(p["high"]), float(p["low"]), float(p["close"])
        pbull = pcl >= po

        # Bullish Engulfing: prev bearish, today bullish, today body wraps prev body
        if not pbull and bull and o <= pcl and cl >= po:
            patterns.append("Bullish Engulfing")

        # Bearish Engulfing: prev bullish, today bearish, today body wraps prev body
        if pbull and not bull and o >= pcl and cl <= po:
            patterns.append("Bearish Engulfing")

        # Piercing Line
        if not pbull and bull and o < pl and cl > (po + pcl) / 2 and cl < po:
            patterns.append("Piercing Line")

        # Dark Cloud Cover
        if pbull and not bull and o > ph and cl < (po + pcl) / 2 and cl > pcl:
            patterns.append("Dark Cloud Cover")

    # ── Three-candle patterns ──────────────────────────────────────────────
    if len(ohlcv) >= 3:
        p1  = ohlcv[-3]
        p2  = ohlcv[-2]
        p1o, p1h, p1l, p1c = float(p1["open"]), float(p1["high"]), float(p1["low"]), float(p1["close"])
        p2o, p2h, p2l, p2c = float(p2["open"]), float(p2["high"]), float(p2["low"]), float(p2["close"])

        p2_body   = abs(p2c - p2o)
        p2_range  = p2h - p2l
        p2_small  = p2_range > 0 and p2_body / p2_range < 0.35

        # Morning Star: bearish → star → bullish
        if p1c < p1o and p2_small and bull and cl > (p1o + p1c) / 2:
            patterns.append("Morning Star")

        # Evening Star: bullish → star → bearish
        if p1c > p1o and p2_small and not bull and cl < (p1o + p1c) / 2:
            patterns.append("Evening Star")

        # Three White Soldiers
        if (p1c > p1o and p2c > p2o and bull
                and p2c > p1c and cl > p2c
                and p2o >= p1o and o >= p2o):
            patterns.append("Three White Soldiers")

        # Three Black Crows
        if (p1c < p1o and p2c < p2o and not bull
                and p2c < p1c and cl < p2c
                and p2o <= p1o and o <= p2o):
            patterns.append("Three Black Crows")

    return patterns


def calc_rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's RSI from a list of closing prices."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 1)


def calc_vwma(ohlcv: list[dict], period: int = 20) -> float:
    """Volume-Weighted Moving Average of closing prices."""
    data = [d for d in ohlcv[-period:] if d.get("close") and d.get("volume")]
    total_pv = sum(float(d["close"]) * float(d["volume"]) for d in data)
    total_v  = sum(float(d["volume"]) for d in data)
    return round(total_pv / total_v, 2) if total_v else 0.0


def calc_volume_ratio(ohlcv: list[dict], period: int = 20) -> float:
    """Today's volume vs N-day average (excluding today)."""
    hist = [float(d["volume"]) for d in ohlcv[-(period + 1):-1] if d.get("volume")]
    if not hist:
        return 1.0
    avg = sum(hist) / len(hist)
    today_vol = float(ohlcv[-1].get("volume", 0))
    return round(today_vol / avg, 2) if avg else 1.0


def calc_rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """Return a list of RSI values (same length as closes, NaN-padded as 50.0 at start)."""
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    result = [50.0] * (period + 1)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rsi = 100.0 if avg_l == 0 else round(100 - 100 / (1 + avg_g / avg_l), 1)
        result.append(rsi)
    return result


def summarise(ohlcv: list[dict]) -> dict:
    """One-stop summary: patterns, RSI (+ direction), VWMA, volume ratio, trend."""
    closes     = [float(d["close"]) for d in ohlcv if d.get("close")]
    patterns   = detect_patterns(ohlcv)
    rsi        = calc_rsi(closes)
    vwma       = calc_vwma(ohlcv)
    vol_ratio  = calc_volume_ratio(ohlcv)
    last_close = closes[-1] if closes else 0
    trend      = "Uptrend" if (len(closes) >= 5 and closes[-1] > closes[-5]) else "Downtrend"
    above_vwma = last_close > vwma if vwma else False

    # RSI direction: compare last RSI to 3 bars ago
    rsi_dir = "Flat"
    if len(closes) >= 18:
        rsi_series = calc_rsi_series(closes)
        if len(rsi_series) >= 4:
            diff = rsi_series[-1] - rsi_series[-4]
            rsi_dir = "Rising" if diff > 1.5 else ("Falling" if diff < -1.5 else "Flat")

    return {
        "patterns":      patterns,
        "rsi":           rsi,
        "rsi_direction": rsi_dir,
        "vwma":          vwma,
        "vol_ratio":     vol_ratio,
        "above_vwma":    above_vwma,
        "trend":         trend,
        "rsi_label":     ("Overbought" if rsi > 70 else "Oversold" if rsi < 30 else "Neutral"),
    }
