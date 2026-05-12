"""
volume_breakout_service.py
Scans Nifty 500 for multi-year price breakouts confirmed by volume buzz.

Strategy:
  1. Pre-screen via NSE 52W high (already in get_nifty500_ohlc).
  2. For each candidate, fetch up to 10 years of daily Angel One candles.
  3. Classify the highest breakout period (10Y → 7Y → 5Y → 3Y → 2Y → 1Y → 52W).
  4. Flag volume buzz where today's volume >= 2× 20-day average.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

_TDAYS = 252  # approximate trading days per year

# Ordered highest-first; first match wins as breakout label
_PERIODS = [
    ("10Y", 10 * _TDAYS),
    ("7Y",   7 * _TDAYS),
    ("5Y",   5 * _TDAYS),
    ("3Y",   3 * _TDAYS),
    ("2Y",   2 * _TDAYS),
    ("1Y",   1 * _TDAYS),
]

VOLUME_BUZZ_RATIO = 2.0   # today >= 2× 20-day avg = "buzz"
_NEAR_HIGH_PCT    = 0.985  # within 1.5% of 52W high qualifies as candidate


async def scan_volume_buzz(stocks: list[dict]) -> dict:
    """
    Scans Nifty 500 stocks for extreme volume days.
    Takes top 100 by raw volume, fetches 22-day Angel One history,
    returns {"5x": [...], "2x": [...]} with vol_ratio annotated.
    """
    from services.angel_service import get_historical_ohlcv

    # Top 40 by today's raw volume (minimum liquidity filter)
    candidates = sorted(
        [s for s in stocks if s.get("volume", 0) > 10_000],
        key=lambda x: x["volume"],
        reverse=True,
    )[:40]

    if not candidates:
        return {"5x": [], "2x": []}

    sem = asyncio.Semaphore(2)

    async def _ratio(s: dict) -> dict | None:
        async with sem:
            try:
                candles = await asyncio.to_thread(
                    get_historical_ohlcv, s["symbol"], "ONE_DAY", 22
                )
                if not candles or len(candles) < 21:
                    return None
                hist_vols = [int(c[5] or 0) for c in candles[:-1]]
                avg20 = sum(hist_vols[-20:]) / max(len(hist_vols[-20:]), 1)
                if avg20 < 1:
                    return None
                vol_ratio = round(s["volume"] / avg20, 1)
                if vol_ratio < 2.0:
                    return None
                return {
                    "symbol":    s["symbol"],
                    "close":     s["close"],
                    "pchange":   s["pchange"],
                    "volume":    s["volume"],
                    "avg_vol":   int(avg20),
                    "vol_ratio": vol_ratio,
                }
            except Exception as exc:
                logger.debug("Volume buzz check failed for %s: %s", s["symbol"], exc)
                return None

    raw = await asyncio.gather(*[_ratio(s) for s in candidates])
    hits = sorted([r for r in raw if r], key=lambda x: x["vol_ratio"], reverse=True)

    five_x = [r for r in hits if r["vol_ratio"] >= 5.0]
    two_x  = [r for r in hits if 2.0 <= r["vol_ratio"] < 5.0]

    logger.info("Volume buzz: %d stocks at 5×+, %d stocks at 2×–5×", len(five_x), len(two_x))
    return {"5x": five_x, "2x": two_x}


async def scan_volume_breakouts() -> list[dict]:
    """
    Return list of Nifty 500 stocks at multi-year highs, sorted by breakout
    period (longest first) then by volume ratio (highest first).
    Each dict has: symbol, close, pchange, volume, vol_avg_20d, vol_ratio,
                   is_buzz, breakout_level, year_high, data_years.
    """
    from services.nse_service import get_nifty500_ohlc
    from services.angel_service import get_historical_ohlcv

    stocks = await get_nifty500_ohlc()
    if not stocks:
        return []

    candidates = [
        s for s in stocks
        if s.get("year_high", 0) > 0
        and s["close"] >= s["year_high"] * _NEAR_HIGH_PCT
    ]
    logger.info("Volume breakout: %d candidates near 52W high out of %d", len(candidates), len(stocks))
    if not candidates:
        return []

    sem = asyncio.Semaphore(4)

    async def _analyze(s: dict) -> dict | None:
        async with sem:
            sym = s["symbol"]
            try:
                candles = await asyncio.to_thread(
                    get_historical_ohlcv, sym, "ONE_DAY", 3650
                )
                if not candles or len(candles) < 22:
                    return None

                # Exclude the last candle (today may be partial or duplicate)
                history = candles[:-1]
                if len(history) < 20:
                    return None

                # ── Volume buzz ───────────────────────────────────────────
                recent_vols = [int(c[5] or 0) for c in history[-20:]]
                vol_avg_20d = sum(recent_vols) / len(recent_vols) if recent_vols else 1
                vol_ratio   = round(s["volume"] / max(vol_avg_20d, 1), 1)
                is_buzz     = vol_ratio >= VOLUME_BUZZ_RATIO

                # ── Breakout period ───────────────────────────────────────
                all_highs = [float(c[2] or 0) for c in history]
                breakout_level = "52W"
                for label, n_days in _PERIODS:
                    subset = all_highs[-n_days:] if len(all_highs) >= n_days else all_highs
                    if not subset:
                        continue
                    if s["close"] >= max(subset):
                        breakout_level = label
                        break

                return {
                    "symbol":         sym,
                    "close":          s["close"],
                    "pchange":        s["pchange"],
                    "volume":         s["volume"],
                    "vol_avg_20d":    int(vol_avg_20d),
                    "vol_ratio":      vol_ratio,
                    "is_buzz":        is_buzz,
                    "breakout_level": breakout_level,
                    "year_high":      s["year_high"],
                    "data_years":     round(len(history) / _TDAYS, 1),
                }
            except Exception as exc:
                logger.debug("Breakout check failed for %s: %s", sym, exc)
                return None

    raw = await asyncio.gather(*[_analyze(s) for s in candidates])
    results = [r for r in raw if r]

    level_order = {label: i for i, (label, _) in enumerate(_PERIODS)}
    level_order["52W"] = len(_PERIODS)
    results.sort(key=lambda x: (level_order.get(x["breakout_level"], 99), -x["vol_ratio"]))
    logger.info("Volume breakout: %d results (%d with buzz)", len(results), sum(1 for r in results if r["is_buzz"]))
    return results
