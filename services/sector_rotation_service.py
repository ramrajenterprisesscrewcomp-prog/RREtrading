"""
sector_rotation_service.py
5-factor sector rotation analysis across Daily / Weekly / Monthly timeframes.

  1. Relative Strength vs Nifty 50  → RRG quadrant (Leading/Improving/Weakening/Lagging)
  2. ADX (14-period Wilder)         → trend strength
  3. Volume ratio                   → institutional participation
  4. Market breadth (% advancing)   → broad sector participation
  5. Composite score → BUY / ACCUMULATE / WATCH / AVOID recommendation
"""
import asyncio
import httpx
import logging
from cachetools import TTLCache

logger = logging.getLogger(__name__)
_cache: TTLCache = TTLCache(maxsize=1, ttl=300)

_SECTOR_YF = {
    "IT":       "^CNXIT",
    "Banking":  "^NSEBANK",
    "Pharma":   "^CNXPHARMA",
    "Auto":     "^CNXAUTO",
    "Metal":    "^CNXMETAL",
    "FMCG":     "^CNXFMCG",
    "Energy":   "^CNXENERGY",
    "Realty":   "^CNXREALTY",
    "Media":    "^CNXMEDIA",
    "Infra":    "^CNXINFRA",
}

_SECTOR_NSE_INDEX = {
    "IT":       "NIFTY IT",
    "Banking":  "NIFTY BANK",
    "Pharma":   "NIFTY PHARMA",
    "Auto":     "NIFTY AUTO",
    "Metal":    "NIFTY METAL",
    "FMCG":     "NIFTY FMCG",
    "Energy":   "NIFTY ENERGY",
    "Realty":   "NIFTY REALTY",
}

_SECTOR_NSE_NAME = {
    "IT":      "NIFTY IT",
    "Banking": "NIFTY BANK",
    "Pharma":  "NIFTY PHARMA",
    "Auto":    "NIFTY AUTO",
    "Metal":   "NIFTY METAL",
    "FMCG":    "NIFTY FMCG",
    "Energy":  "NIFTY ENERGY",
    "Realty":  "NIFTY REALTY",
    "Media":   "NIFTY MEDIA",
    "Infra":   "NIFTY INFRA",
}

_NIFTY_YF = "^NSEI"
_UA = {"User-Agent": "Mozilla/5.0"}

# Timeframe config: (yf_range, yf_interval, rs_sma_period, vol_avg_n, min_bars)
_TF = {
    "daily":   ("3mo", "1d",   10, 20, 20),
    "weekly":  ("2y",  "1wk",  10, 20, 15),
    "monthly": ("5y",  "1mo",  10, 12, 12),
}


# ── Math helpers ────────────────────────────────────────────────────────────

def _wilder(data: list, period: int) -> list:
    if len(data) < period:
        return []
    result = [sum(data[:period]) / period]
    for v in data[period:]:
        result.append((result[-1] * (period - 1) + v) / period)
    return result


def _calc_adx(highs: list, lows: list, closes: list, period: int = 14):
    n = len(closes)
    if n < period * 2 + 2:
        return 0.0, 0.0, 0.0
    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, n):
        h, l, ph, pl, pc = highs[i], lows[i], highs[i-1], lows[i-1], closes[i-1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up, dn = h - ph, pl - l
        tr_list.append(max(tr, 1e-9))
        pdm_list.append(up if up > dn and up > 0 else 0.0)
        ndm_list.append(dn if dn > up and dn > 0 else 0.0)
    atr  = _wilder(tr_list,  period)
    spdm = _wilder(pdm_list, period)
    sndm = _wilder(ndm_list, period)
    if not atr:
        return 0.0, 0.0, 0.0
    dx_list, last_pdi, last_ndi = [], 0.0, 0.0
    for a, p, nd in zip(atr, spdm, sndm):
        if a < 1e-9:
            dx_list.append(0.0)
            continue
        pdi_v = 100.0 * p  / a
        ndi_v = 100.0 * nd / a
        last_pdi, last_ndi = pdi_v, ndi_v
        denom = pdi_v + ndi_v
        dx_list.append(100.0 * abs(pdi_v - ndi_v) / denom if denom else 0.0)
    adx_s = _wilder(dx_list, period)
    return (round(adx_s[-1], 1) if adx_s else 0.0), round(last_pdi, 1), round(last_ndi, 1)


def _calc_rs_rrg(sector_closes: list, nifty_closes: list, rs_period: int = 10):
    n = min(len(sector_closes), len(nifty_closes))
    if n < rs_period + 7:
        return 100.0, 100.0, 0.0
    sc = sector_closes[-n:]
    nc = nifty_closes[-n:]
    rs = [s / max(b, 1e-9) for s, b in zip(sc, nc)]
    rs_sma      = sum(rs[-rs_period:]) / rs_period
    rs_ratio    = round(rs[-1] / max(rs_sma, 1e-9) * 100, 2)
    rs_momentum = round(rs[-1] / max(rs[-6],  1e-9) * 100, 2)
    rs_1d       = round((rs[-1] / max(rs[-2], 1e-9) - 1) * 100, 2) if n >= 2 else 0.0
    return rs_ratio, rs_momentum, rs_1d


def _rrg_quadrant(rs_ratio: float, rs_momentum: float) -> str:
    if   rs_ratio >= 100 and rs_momentum >= 100: return "Leading"
    elif rs_ratio <  100 and rs_momentum >= 100: return "Improving"
    elif rs_ratio >= 100 and rs_momentum <  100: return "Weakening"
    else:                                         return "Lagging"


def _score(s: dict) -> int:
    score = 0
    rs_m = s.get("rs_momentum", 100)
    score += 2 if rs_m >= 101 else (-1 if rs_m <= 99 else 0)
    score += {"Leading": 4, "Improving": 2, "Weakening": 0, "Lagging": -2}.get(
        s.get("rrg_quadrant", "Lagging"), 0)
    adx = s.get("adx", 0)
    score += 3 if adx >= 30 else (2 if adx >= 25 else (1 if adx >= 20 else 0))
    vr = s.get("vol_ratio", 1.0)
    score += 3 if vr >= 2.0 else (2 if vr >= 1.5 else (1 if vr >= 1.2 else 0))
    bp = s.get("breadth_pct", 50)
    score += 3 if bp >= 70 else (2 if bp >= 60 else (1 if bp >= 50 else 0))
    return score


def _rating(score: int) -> str:
    if score >= 12: return "Strong Buy"
    if score >= 8:  return "Buy"
    if score >= 5:  return "Watch"
    return "Avoid"


# ── Data fetchers ───────────────────────────────────────────────────────────

async def _yf_ohlcv(symbol: str, sem: asyncio.Semaphore,
                    yf_range: str = "3mo", yf_interval: str = "1d") -> dict:
    enc = symbol.replace("^", "%5E")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{enc}"
           f"?range={yf_range}&interval={yf_interval}")
    async with sem:
        try:
            async with httpx.AsyncClient(timeout=14, headers=_UA) as c:
                r  = await c.get(url)
                d  = r.json()
                res = (d.get("chart", {}).get("result") or [None])[0]
                if not res:
                    return {}
                q = res.get("indicators", {}).get("quote", [{}])[0]
                rc, rh, rl, rv = (q.get(k, []) or [] for k in ("close", "high", "low", "volume"))
                closes, highs, lows, vols = [], [], [], []
                for cl, h, l, v in zip(rc, rh, rl, rv):
                    if cl is None:
                        continue
                    closes.append(cl)
                    highs.append(h if h is not None else cl)
                    lows.append(l  if l  is not None else cl)
                    vols.append(int(v) if v is not None else 0)
                return {"closes": closes, "highs": highs, "lows": lows, "volumes": vols}
        except Exception as exc:
            logger.debug("YF OHLCV %s %s/%s: %s", symbol, yf_range, yf_interval, exc)
            return {}


async def _fetch_breadth(nse_index: str) -> float:
    try:
        import urllib.parse
        from services.nse_service import _nse_get
        data   = await _nse_get(f"/api/equity-stockIndices?index={urllib.parse.quote(nse_index)}")
        stocks = [s for s in data.get("data", [])
                  if s.get("symbol") and not s["symbol"].upper().startswith("NIFTY")]
        if not stocks:
            return 50.0
        advancing = sum(1 for s in stocks if float(s.get("pChange") or 0) > 0)
        return round(advancing / len(stocks) * 100, 1)
    except Exception as exc:
        logger.debug("Breadth fetch failed for %s: %s", nse_index, exc)
        return 50.0


# ── Per-timeframe analysis ──────────────────────────────────────────────────

def _analyse_tf(sector_keys: list, ohlcv_map: dict, nifty_closes: list,
                breadth_map: dict, nse_live: dict,
                tf: str, rs_period: int, vol_avg_n: int,
                min_bars: int = 20) -> list:
    """Compute all 5 factors for each sector in one timeframe."""
    sectors_out = []
    for short in sector_keys:
        sd     = ohlcv_map.get((tf, short), {})
        closes = sd.get("closes", [])
        highs  = sd.get("highs",  [])
        lows   = sd.get("lows",   [])
        vols   = sd.get("volumes",[])
        if not closes or len(closes) < min_bars:
            continue

        n = min(len(closes), len(nifty_closes))

        # RS / RRG
        rs_ratio, rs_momentum, rs_1d = _calc_rs_rrg(closes[-n:], nifty_closes[-n:], rs_period)
        rrg_q = _rrg_quadrant(rs_ratio, rs_momentum)

        # ADX
        m = min(len(highs), len(lows), len(closes))
        adx, pdi, ndi = _calc_adx(highs[-m:], lows[-m:], closes[-m:])

        # Volume
        vol_avg = (sum(vols[-(vol_avg_n + 1):-1]) / vol_avg_n
                   if len(vols) >= vol_avg_n + 1 else
                   sum(vols[:-1]) / max(len(vols) - 1, 1) if len(vols) > 1 else 1)
        vol_ratio = round((vols[-1] if vols else 0) / max(vol_avg, 1), 2)

        # Breadth (daily NSE data reused across all timeframes)
        breadth_pct = breadth_map.get(short, 50.0)

        # Live NSE price (1D only meaningful on daily, but keep field consistent)
        nse_name   = _SECTOR_NSE_NAME.get(short, "")
        nse_info   = nse_live.get(nse_name, {})
        pchange_1d = nse_info.get("pchange", 0.0)
        price      = nse_info.get("price", closes[-1] if closes else 0)

        entry = {
            "short":        short,
            "price":        round(price, 2),
            "pchange_1d":   round(pchange_1d, 2),
            "rs_ratio":     rs_ratio,
            "rs_momentum":  rs_momentum,
            "rs_1d":        rs_1d,
            "rrg_quadrant": rrg_q,
            "adx":          adx,
            "pdi":          pdi,
            "ndi":          ndi,
            "vol_ratio":    vol_ratio,
            "breadth_pct":  breadth_pct,
        }
        entry["score"]  = _score(entry)
        entry["rating"] = _rating(entry["score"])

        sigs = []
        if rs_momentum >= 101:  sigs.append("RS Rising ↑")
        elif rs_momentum <= 99: sigs.append("RS Falling ↓")
        if adx >= 25:           sigs.append(f"ADX {adx:.0f} ✓")
        if vol_ratio >= 1.5:    sigs.append(f"Vol {vol_ratio:.1f}× 🔊")
        if breadth_pct >= 60:   sigs.append(f"Breadth {breadth_pct:.0f}%")
        entry["signals"] = sigs

        sectors_out.append(entry)

    sectors_out.sort(key=lambda x: x["score"], reverse=True)
    return sectors_out


# ── Recommendation builder ──────────────────────────────────────────────────

def _build_recommendation(sectors: list, tf_label: str = "Daily") -> dict:
    buy = [s for s in sectors
           if s["score"] >= 7 and s["rrg_quadrant"] in ("Leading", "Improving")]
    avoid = [s for s in sectors
             if s["score"] <= 2 and s["rrg_quadrant"] == "Lagging"]

    if not buy:
        watch = [s for s in sectors if s["score"] >= 5]
        return {
            "action":     "WAIT",
            "tf":         tf_label,
            "sectors":    [s["short"] for s in watch[:3]],
            "top_sector": watch[0]["short"] if watch else "—",
            "top_score":  watch[0]["score"]  if watch else 0,
            "rating":     watch[0]["rating"] if watch else "Watch",
            "reasons":    [
                f"No strong {tf_label} sector rotation signal",
                "Wait for Leading/Improving RRG with ADX > 25",
            ],
            "avoid": [s["short"] for s in avoid[:3]],
        }

    top = buy[0]
    reasons = []
    if top["rrg_quadrant"] == "Leading":
        reasons.append(f"RRG {tf_label}: Leading — sustained RS outperformance vs Nifty")
    elif top["rrg_quadrant"] == "Improving":
        reasons.append(f"RRG {tf_label}: Improving — early rotation, momentum building")
    if top["adx"] >= 25:
        reasons.append(f"ADX {top['adx']:.0f} — trend confirmed, not a headfake")
    if top["vol_ratio"] >= 1.5:
        reasons.append(f"Volume {top['vol_ratio']:.1f}× avg — institutional participation")
    if top["breadth_pct"] >= 60:
        reasons.append(f"Breadth {top['breadth_pct']:.0f}% — broad internal participation")
    if top["pdi"] > top["ndi"]:
        reasons.append(f"+DI {top['pdi']:.0f} > -DI {top['ndi']:.0f} — bullish trend direction")

    return {
        "action":     "BUY" if top["rrg_quadrant"] == "Leading" else "ACCUMULATE",
        "tf":         tf_label,
        "sectors":    [s["short"] for s in buy[:3]],
        "top_sector": top["short"],
        "top_score":  top["score"],
        "rating":     top["rating"],
        "reasons":    reasons,
        "avoid":      [s["short"] for s in avoid[:3]],
    }


# ── Main analysis ───────────────────────────────────────────────────────────

async def get_sector_rotation_analysis() -> dict:
    """
    Full 5-factor analysis for Daily, Weekly, Monthly timeframes.
    Each timeframe uses the appropriate YF range/interval.
    Cached 5 min.
    """
    if "r" in _cache:
        return _cache["r"]

    sem = asyncio.Semaphore(5)
    sector_keys    = list(_SECTOR_YF.keys())
    sector_symbols = list(_SECTOR_YF.values())

    # Build all YF fetch tasks in one batch (3 TFs × 11 symbols = 33 calls)
    fetch_tasks: dict[tuple, asyncio.coroutine] = {}
    for tf, (yf_range, yf_interval, _, _, _) in _TF.items():
        for short, sym in _SECTOR_YF.items():
            fetch_tasks[(tf, short)] = _yf_ohlcv(sym, sem, yf_range, yf_interval)
        fetch_tasks[(tf, "_nifty")] = _yf_ohlcv(_NIFTY_YF, sem, yf_range, yf_interval)

    task_keys = list(fetch_tasks.keys())
    all_results = await asyncio.gather(*fetch_tasks.values())
    ohlcv_map = dict(zip(task_keys, all_results))

    # Market breadth (once — reused across timeframes)
    breadth_keys = [k for k in sector_keys if k in _SECTOR_NSE_INDEX]
    breadth_vals = await asyncio.gather(
        *[_fetch_breadth(_SECTOR_NSE_INDEX[k]) for k in breadth_keys]
    )
    breadth_map = dict(zip(breadth_keys, breadth_vals))

    # NSE live prices (once)
    try:
        from services.nse_service import _nse_get
        nse_raw = await _nse_get("/api/allIndices")
        nse_live = {
            (row.get("index") or row.get("indexSymbol", "")): {
                "pchange": float(row.get("percentChange") or row.get("changeInPer") or 0),
                "price":   float(row.get("last") or row.get("current") or 0),
            }
            for row in nse_raw.get("data", [])
        }
    except Exception:
        nse_live = {}

    # Analyse each timeframe
    result: dict = {}
    tf_labels = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}

    for tf, (_, _, rs_period, vol_avg_n, min_bars) in _TF.items():
        nifty_closes = ohlcv_map.get((tf, "_nifty"), {}).get("closes", [])
        sectors_out = _analyse_tf(
            sector_keys, ohlcv_map, nifty_closes,
            breadth_map, nse_live,
            tf, rs_period, vol_avg_n, min_bars,
        )
        result[tf] = sectors_out
        result[f"{tf}_recommendation"] = _build_recommendation(
            sectors_out, tf_labels[tf]
        )
        if sectors_out:
            logger.info("Sector analysis %s: %d sectors computed", tf, len(sectors_out))
        else:
            logger.warning("Sector analysis %s: no sectors — YF data may be unavailable", tf)

    # Primary recommendation = daily (most actionable for swing traders)
    result["recommendation"] = result["daily_recommendation"]

    # Fallback: fill any missing sectors from daily (partial or full YF failure)
    daily_sectors = result.get("daily", [])
    if daily_sectors:
        for tf in ("weekly", "monthly"):
            existing = {s["short"] for s in result[tf]}
            missing  = [s for s in daily_sectors if s["short"] not in existing]
            if missing:
                result[tf].extend([{**s, "is_fallback": True} for s in missing])
                result[tf].sort(key=lambda x: x["score"], reverse=True)
                logger.info("Sector analysis %s: %d sectors from daily fallback", tf, len(missing))
            # Full fallback: rebuild recommendation note if any fallback added
            if not result[tf]:
                result[tf] = [{**s, "is_fallback": True} for s in daily_sectors]
                rec = dict(result["daily_recommendation"])
                rec["tf"]     = tf_labels[tf] + " (Daily fallback)"
                rec["reasons"] = ["⚠ Yahoo Finance data unavailable for this timeframe",
                                   "Showing Daily analysis as reference"] + rec.get("reasons", [])
                result[f"{tf}_recommendation"] = rec
                logger.info("Sector analysis %s: fully using daily fallback", tf)

    # Cross-timeframe confirmation: enrich daily top sector with weekly/monthly RRG
    daily_top = result["daily"][:3] if result["daily"] else []
    for ds in daily_top:
        sym = ds["short"]
        ds["weekly_rrg"]  = next((s["rrg_quadrant"] for s in result["weekly"]
                                  if s["short"] == sym and not s.get("is_fallback")), "—")
        ds["monthly_rrg"] = next((s["rrg_quadrant"] for s in result["monthly"]
                                  if s["short"] == sym and not s.get("is_fallback")), "—")

    _cache["r"] = result
    return result
