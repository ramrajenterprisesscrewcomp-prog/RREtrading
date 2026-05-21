"""
tomorrow_scanner_service.py
EOD multi-factor scanner — identifies high-probability next-day runners.

10-step framework:
  1. Strong Sector    — Leading/Improving RRG quadrant
  2. Consolidation    — NR7 (narrowest range of last 7 bars), tight 10-day range
  3. Breakout         — above 20D high or 52W high
  4. Volume Explosion — today >= 1.5× 20-day average
  5. Close Near High  — close in top 30% of day range
  6. RS Outperform    — stock pchange > Nifty 50 by +1%
  7. Delivery %       — >= 50% delivery (genuine accumulation)
  8. F&O Long Buildup — price↑ + OI↑ (fresh long positions)
  9. Short Covering   — price↑ + OI↓ (explosive short-exit)
 10. TSR Buildup      — TSR Pro position buildup confirmation
"""
import asyncio
import logging
from cachetools import TTLCache

logger = logging.getLogger(__name__)
_cache: TTLCache = TTLCache(maxsize=1, ttl=300)

_MAX_CANDIDATES = 80   # max pre-screened stocks sent to Angel One
_DELIVERY_TOP_N = 20   # fetch delivery % for top N after initial scoring

_SECTOR_NSE_MAP = {
    "Banking": "NIFTY BANK",
    "IT":      "NIFTY IT",
    "Pharma":  "NIFTY PHARMA",
    "Auto":    "NIFTY AUTO",
    "Metal":   "NIFTY METAL",
    "FMCG":    "NIFTY FMCG",
    "Energy":  "NIFTY ENERGY",
    "Realty":  "NIFTY REALTY",
    "Media":   "NIFTY MEDIA",
    "Infra":   "NIFTY INFRA",
}


# ── Candle math helpers ────────────────────────────────────────────────────

def _is_nr7(all_highs: list, all_lows: list) -> bool:
    """True if the last bar has the narrowest H-L range of the last 7 bars."""
    if len(all_highs) < 7:
        return False
    ranges = [all_highs[-i-1] - all_lows[-i-1] for i in range(7)]
    today = ranges[0]
    return today > 0 and today == min(ranges)


def _is_consolidating(closes: list, n: int = 10) -> bool:
    """Coefficient of variation < 2.5% over last n closes → tight coil."""
    if len(closes) < n:
        return False
    subset = closes[-n:]
    mean_ = sum(subset) / n
    if mean_ < 0.001:
        return False
    std_ = (sum((c - mean_) ** 2 for c in subset) / n) ** 0.5
    return (std_ / mean_) < 0.025


# ── Scoring ────────────────────────────────────────────────────────────────

def _score(s: dict) -> tuple[float, list[str]]:
    """Returns (score, reason_list). Max theoretical score ≈ 15."""
    score = 0.0
    reasons: list[str] = []

    # Volume explosion  (max 2.5)
    vr = s.get("vol_ratio", 0.0)
    if vr >= 2.0:
        score += 2.5
        reasons.append(f"Vol {vr:.1f}× avg 🔊")
    elif vr >= 1.5:
        score += 1.5
        reasons.append(f"Vol {vr:.1f}× avg")

    # Breakout level  (max 3.0; exclusive — highest wins)
    if s.get("is_52w_breakout"):
        score += 3.0
        reasons.append("52W High Breakout 🚀")
    elif s.get("is_20d_breakout"):
        score += 2.0
        reasons.append("20D High Breakout ↑")

    # Close near high  (max 1.5; exclusive)
    cp = s.get("close_position", 0.0)
    if cp >= 0.75:
        score += 1.5
        reasons.append(f"Strong Close {cp*100:.0f}%")
    elif cp >= 0.50:
        score += 0.5

    # Consolidation / NR7  (max 1.5; exclusive)
    if s.get("is_nr7"):
        score += 1.5
        reasons.append("NR7 Squeeze 🎯")
    elif s.get("is_consolidating"):
        score += 1.0
        reasons.append("Tight Consolidation")

    # Sector strength  (max 2.0; exclusive)
    sec_q = s.get("sector_rrg", "")
    sec_n = s.get("sector", "")
    if sec_q == "Leading":
        score += 2.0
        reasons.append(f"Sector Leading ({sec_n})")
    elif sec_q == "Improving":
        score += 1.0
        reasons.append(f"Sector Improving ({sec_n})")

    # F&O OI  (stackable)
    if s.get("is_long_buildup"):
        score += 2.0
        reasons.append("F&O Long Buildup 📈")
    if s.get("is_short_covering"):
        score += 1.5
        reasons.append("Short Covering ⚡")
    if s.get("is_tsr_buildup"):
        score += 0.5
        reasons.append("TSR Buildup")

    # Delivery  (max 1.0)
    dp = s.get("delivery_pct", 0.0)
    if dp >= 50:
        score += 1.0
        reasons.append(f"Delivery {dp:.0f}%")

    # RS outperformance  (max 1.0)
    if s.get("rs_outperform"):
        score += 1.0
        reasons.append("RS > Nifty ↑")

    # Overnight gap filter: large gap = exhausted move, small gap = clean breakout
    gap_pct = s.get("gap_pct", 0.0)
    if gap_pct > 6:
        score -= 1.5
        reasons.append(f"Gap Exhausted ({gap_pct:.1f}%)")
    elif gap_pct > 3.5:
        score -= 0.5
        reasons.append(f"Gap Risk ({gap_pct:.1f}%)")
    elif 0 <= gap_pct <= 1 and s.get("pchange", 0) >= 2:
        score += 0.3
        reasons.append("Clean Intraday Move")

    # Repeat winner: was a HIGH/STRONG runner yesterday AND gained again today
    if s.get("is_repeat_winner") and s.get("pchange", 0) >= 1:
        score += 0.5
        reasons.append("Repeat Winner")

    return round(score, 1), reasons


def _grade(score: float) -> str:
    if score >= 9:  return "HIGH"
    if score >= 6:  return "STRONG"
    if score >= 4:  return "WATCH"
    return "MONITOR"


# ── Data fetchers ──────────────────────────────────────────────────────────

async def _fetch_sector_stock_map() -> dict[str, tuple[str, str]]:
    """
    Returns {symbol: (sector_short, rrg_quadrant)} for stocks
    in Leading/Improving sectors (from sector rotation analysis).
    """
    from services.nse_service import _nse_get
    from services.sector_rotation_service import get_sector_rotation_analysis
    import urllib.parse

    try:
        sector_data = await get_sector_rotation_analysis()
    except Exception:
        return {}

    strong = [
        s for s in sector_data.get("sectors", [])
        if s["rrg_quadrant"] in ("Leading", "Improving")
    ]
    if not strong:
        return {}

    result: dict[str, tuple[str, str]] = {}
    sem = asyncio.Semaphore(3)

    async def _constituents(sec: dict):
        nse_idx = _SECTOR_NSE_MAP.get(sec["short"])
        if not nse_idx:
            return
        async with sem:
            try:
                data = await _nse_get(
                    f"/api/equity-stockIndices?index={urllib.parse.quote(nse_idx)}"
                )
                for row in data.get("data", []):
                    sym = row.get("symbol", "")
                    if sym and not sym.upper().startswith("NIFTY"):
                        result[sym] = (sec["short"], sec["rrg_quadrant"])
            except Exception:
                pass

    await asyncio.gather(*[_constituents(s) for s in strong])
    return result


async def _fetch_delivery(symbol: str, sem: asyncio.Semaphore) -> float:
    """Delivery-to-traded-quantity % from NSE trade_info."""
    async with sem:
        try:
            from services.nse_service import _nse_get
            data = await _nse_get(f"/api/quote-equity?symbol={symbol}&section=trade_info")
            # Field lives in different sub-keys depending on NSE version
            ti = (data.get("deliverAndOI")
                  or data.get("tradeInfo")
                  or data.get("marketDeptOrderBook", {}).get("tradeInfo")
                  or {})
            raw = (ti.get("deliveryToTradedQuantity")
                   or ti.get("deliveryPercentage")
                   or ti.get("deliv_per"))
            if raw is None:
                return 0.0
            return round(float(str(raw).replace(",", "")), 1)
        except Exception:
            return 0.0


# ── Main scanner ───────────────────────────────────────────────────────────

async def scan_tomorrow_runners() -> list[dict]:
    """
    Full multi-step EOD scan. Returns up to 25 stocks sorted by score desc.
    Cached 5 min so repeated calls within a scan window are free.
    """
    if "r" in _cache:
        return _cache["r"]

    from services.nse_service import get_nifty500_ohlc, get_fno_oi_buildup, _nse_get
    from services.angel_service import get_historical_ohlcv
    from services.tsr_service import get_tsr_buildup

    logger.info("Tomorrow scanner: starting EOD scan")

    # ── Phase 1: fast bulk fetches (parallel) ─────────────────────────────
    try:
        stocks, oi_data, tsr_bu, sector_map, nse_all = await asyncio.gather(
            get_nifty500_ohlc(),
            get_fno_oi_buildup(),
            get_tsr_buildup(),
            _fetch_sector_stock_map(),
            _nse_get("/api/allIndices"),
        )
    except Exception as exc:
        logger.warning("Tomorrow scanner phase1 failed: %s", exc)
        return []

    if not stocks:
        return []

    # Sector data freshness check
    if sector_map:
        logger.info("Tomorrow scanner: %d sector-mapped stocks (Leading/Improving)", len(sector_map))
    else:
        logger.warning("Tomorrow scanner: sector map empty — RRG data unavailable, sector scoring skipped")

    # Nifty 50 pchange for RS comparison
    nifty_pchange = 0.0
    for row in nse_all.get("data", []):
        name = row.get("index") or row.get("indexSymbol", "")
        if name == "NIFTY 50":
            nifty_pchange = float(row.get("percentChange") or row.get("changeInPer") or 0)
            break

    # F&O OI signal sets
    lb_set  = {s["symbol"] for s in oi_data.get("Long Buildup",   [])}
    sc_set  = {s["symbol"] for s in oi_data.get("Short Covering",  [])}

    # TSR buildup set — extract symbol from first column value of each row
    tsr_bu_set: set[str] = set()
    for lst in tsr_bu.values():
        for r in lst:
            sym = (r.get("Code") or r.get("Symbol") or r.get("Name") or "").split()[0].upper()
            if sym:
                tsr_bu_set.add(sym)

    # ── Load yesterday's runners for repeat-winner bonus ─────────────────
    prev_runner_syms: set[str] = set()
    try:
        from services.supabase_service import load_runners_db
        res = load_runners_db()
        if res:
            prev_runner_syms = {r["symbol"] for r in res[1] if r.get("grade") in ("HIGH", "STRONG")}
            logger.info("Tomorrow scanner: %d prev HIGH/STRONG runners loaded", len(prev_runner_syms))
    except Exception as exc:
        logger.debug("Tomorrow scanner: prev runners load failed: %s", exc)

    # ── Phase 2: pre-screen using NSE data ───────────────────────────────
    candidates = []
    for s in stocks:
        hl = s["high"] - s["low"]
        if hl < 0.001 or s["year_high"] <= 0:
            continue
        close_pos = (s["close"] - s["low"]) / hl
        if s["pchange"] < 0.0 or close_pos < 0.50:
            continue
        # Overnight gap calculation: implied prev_close from today's pchange
        prev_c  = s["close"] / (1 + s["pchange"] / 100) if s["pchange"] != -100 else s["close"]
        gap_pct = round((s["open"] - prev_c) / max(prev_c, 0.001) * 100, 2)
        candidates.append({
            **s,
            "close_position": round(close_pos, 3),
            "gap_pct":        gap_pct,
            "is_repeat_winner": s["symbol"] in prev_runner_syms,
        })

    candidates.sort(key=lambda x: x["close_position"], reverse=True)
    candidates = candidates[:_MAX_CANDIDATES]
    logger.info("Tomorrow scanner: %d candidates after pre-screen", len(candidates))

    # ── Phase 3: Angel One 30-day history per candidate ──────────────────
    angel_sem = asyncio.Semaphore(4)

    async def _enrich(s: dict) -> dict:
        result = {**s}
        sym = s["symbol"]
        try:
            async with angel_sem:
                candles = await asyncio.to_thread(get_historical_ohlcv, sym, "ONE_DAY", 35)
            if not candles or len(candles) < 22:
                return result

            history = candles[:-1]  # exclude today (may be partial)
            if len(history) < 20:
                return result

            h_high  = [float(c[2] or 0) for c in history]
            h_low   = [float(c[3] or 0) for c in history]
            h_close = [float(c[4] or 0) for c in history]
            h_vol   = [int(c[5]   or 0) for c in history]

            vol_avg20 = sum(h_vol[-20:]) / 20
            result["vol_ratio"]       = round(s["volume"] / max(vol_avg20, 1), 1)
            result["is_20d_breakout"] = (len(h_high) >= 20
                                          and s["close"] >= max(h_high[-20:]))
            result["is_52w_breakout"] = s["close"] >= s["year_high"] * 0.998
            result["is_nr7"]          = _is_nr7(
                h_high + [s["high"]], h_low + [s["low"]]
            )
            result["is_consolidating"] = _is_consolidating(h_close)
        except Exception as exc:
            logger.debug("Angel enrich failed for %s: %s", sym, exc)
        return result

    enriched = await asyncio.gather(*[_enrich(s) for s in candidates])

    # ── Phase 4: attach tags and initial score ────────────────────────────
    pre_scored = []
    for s in enriched:
        sym = s["symbol"]
        sec = sector_map.get(sym, ("", ""))
        tagged = {
            **s,
            "sector":            sec[0],
            "sector_rrg":        sec[1],
            "is_long_buildup":   sym in lb_set,
            "is_short_covering": sym in sc_set,
            "is_tsr_buildup":    sym in tsr_bu_set,
            "rs_outperform":     s["pchange"] > nifty_pchange + 1.0,
            "delivery_pct":      0.0,
            "gap_pct":           s.get("gap_pct", 0.0),
            "is_repeat_winner":  s.get("is_repeat_winner", False),
        }
        sc, reasons = _score(tagged)
        tagged["score"]   = sc
        tagged["reasons"] = reasons
        tagged["grade"]   = _grade(sc)
        pre_scored.append(tagged)

    pre_scored.sort(key=lambda x: x["score"], reverse=True)

    # ── Phase 5: delivery % for top N candidates ──────────────────────────
    top_n = pre_scored[:_DELIVERY_TOP_N]
    rest  = pre_scored[_DELIVERY_TOP_N:]
    del_sem = asyncio.Semaphore(3)
    delivery_vals = await asyncio.gather(
        *[_fetch_delivery(s["symbol"], del_sem) for s in top_n]
    )
    final = []
    for s, dp in zip(top_n, delivery_vals):
        if dp > 0:
            s["delivery_pct"] = dp
            s["score"], s["reasons"] = _score(s)
            s["grade"] = _grade(s["score"])
        final.append(s)
    final.extend(rest)

    # ── Final sort and filter ─────────────────────────────────────────────
    final.sort(key=lambda x: x["score"], reverse=True)
    final = [s for s in final if s["score"] >= 3.0][:25]

    logger.info("Tomorrow scanner: %d runners found (top score=%.1f)",
                len(final), final[0]["score"] if final else 0)
    _cache["r"] = final
    return final
