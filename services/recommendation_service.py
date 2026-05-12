"""
recommendation_service.py
Comprehensive stock recommendation engine.

Pre-analysis before recommending:
  1. Commodity correlations (crude, gold, copper, nat gas, silver)
  2. Geopolitical & national news
  3. Nifty 50 correlation (Beta) and index trend
  4. F&O OI sentiment (PCR, buildup, VIX) — NSE + TSR Pro cross-confirmed
  5. Sector rotation — leading vs lagging
  6. LIVE NSE prices for every candidate stock (Nifty 500 OHLC)

AI uses real NSE prices; post-processing validates & fixes price levels.
Cached 30 minutes.
"""
import asyncio
import json
import logging
import re
import urllib.parse
from datetime import datetime

from cachetools import TTLCache

logger = logging.getLogger(__name__)

_cache: TTLCache = TTLCache(maxsize=1, ttl=1800)


# ── Commodity → Stock mapping ─────────────────────────────────────────────────

COMMODITY_MAP: dict[str, dict] = {
    "Crude Oil": {
        "yf_key":   "CL=F",
        "positive": ["ONGC", "OIL", "MRPL", "CHENNPETRO", "BPCL", "HPCL"],
        "negative": ["INDIGO", "SPICEJET", "AKZOINDIA", "ASIANPAINT",
                     "BERGER", "MRF", "CEAT", "APOLLOTYRE", "PIDILITIND"],
        "pos_why":  "Upstream E&P and refining margin expansion",
        "neg_why":  "Airlines (fuel ~30% opex) and paints/tyres (petrochemical input)",
    },
    "Gold": {
        "yf_key":   "GC=F",
        "positive": ["TITAN", "KALYAN", "SENCO", "MUTHOOTFIN", "MANAPPURAM"],
        "negative": [],
        "pos_why":  "Jewellery demand + gold-loan NBFCs benefit from higher collateral",
        "neg_why":  "",
    },
    "Silver": {
        "yf_key":   "SI=F",
        "positive": ["TITAN", "HINDZINC", "VEDL"],
        "negative": [],
        "pos_why":  "Silver jewellery; Hindustan Zinc is India's primary silver producer",
        "neg_why":  "",
    },
    "Nat Gas": {
        "yf_key":   "NG=F",
        "positive": ["IGL", "MGL", "GAIL", "PETRONET"],
        "negative": ["GSFC", "RCF", "CHAMBALFERT", "GNFC"],
        "pos_why":  "City gas distribution and LNG companies benefit from gas tailwinds",
        "neg_why":  "Fertiliser companies use gas as feedstock — higher input cost",
    },
    "Copper": {
        "yf_key":   "HG=F",
        "positive": ["HINDALCO", "STERLITE", "POLYCAB", "HAVELLS", "KEI"],
        "negative": [],
        "pos_why":  "Non-ferrous metals and cable/wire manufacturers benefit",
        "neg_why":  "",
    },
}

GEO_MAP: dict[str, dict] = {
    "defense": {
        "triggers": ["war", "military", "conflict", "missile", "attack", "ceasefire",
                     "pakistan", "china border", "lac", "loc", "armed forces",
                     "airstrike", "navy", "drdo"],
        "positive": ["HAL", "BEL", "BDL", "COCHINSHIP", "MAZAGON", "GRSE",
                     "PARAS", "SOLAR", "ASTRA"],
        "pos_why":  "Defence capex accelerates under geopolitical stress",
    },
    "energy_crisis": {
        "triggers": ["opec", "oil embargo", "sanctions", "energy crisis", "oil cut"],
        "positive": ["ONGC", "OIL", "GAIL"],
        "negative": ["INDIGO", "SPICEJET"],
        "pos_why":  "Domestic E&P companies gain pricing power",
    },
    "rupee_pressure": {
        "triggers": ["fed rate", "dollar index", "dxy", "rupee fall", "forex reserves",
                     "rbi intervention", "capital outflow", "fii selling"],
        "positive": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM",
                     "SUNPHARMA", "DRREDDY", "CIPLA"],
        "negative": ["INDIGO", "IOC", "BPCL", "HPCL"],
        "pos_why":  "IT and pharma exporters benefit from weaker rupee (USD revenue)",
        "neg_why":  "Import-heavy companies face higher costs",
    },
    "rate_policy": {
        "triggers": ["rbi rate", "repo rate", "interest rate", "monetary policy",
                     "rbi mpc", "rate cut", "rate hike", "inflation"],
        "positive": ["SBIN", "HDFCBANK", "ICICIBANK", "KOTAKBANK",
                     "AXISBANK", "LICHSGFIN"],
        "pos_why":  "Rate-cut cycle: Banking NIM + housing finance demand",
    },
    "infra_policy": {
        "triggers": ["infrastructure", "capex", "road", "railway", "pm gati shakti",
                     "pli scheme", "defence corridor", "manufacturing"],
        "positive": ["LT", "ULTRACEMCO", "JSWSTEEL", "TATASTEEL", "NTPC",
                     "POWERGRID", "RECLTD", "PFC", "IRFC", "RVNL"],
        "pos_why":  "Government capex and PLI boost infra, cement, steel, power",
    },
}


# ── News helpers ───────────────────────────────────────────────────────────────

async def _fetch_geo_national_news() -> list[dict]:
    import feedparser
    queries = [
        "India geopolitical tension military 2025 2026",
        "India economy RBI SEBI budget policy 2025 2026",
        "India stock market Nifty today",
    ]
    base = "https://news.google.com/rss/search"

    async def _one(q: str) -> list:
        url = f"{base}?q={urllib.parse.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            feed = await asyncio.to_thread(feedparser.parse, url)
            return [
                {
                    "title":  e.get("title", "").strip(),
                    "source": e.get("source", {}).get("title", ""),
                }
                for e in (feed.entries or [])[:8]
                if e.get("title", "").strip()
            ]
        except Exception:
            return []

    results = await asyncio.gather(*[_one(q) for q in queries])
    seen: set[str] = set()
    out = []
    for batch in results:
        for item in batch:
            key = item["title"][:60].lower()
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out[:20]


def _detect_geo_themes(news_items: list[dict]) -> dict[str, list[str]]:
    active: dict[str, list[str]] = {}
    for theme, cfg in GEO_MAP.items():
        hits = []
        for item in news_items:
            text = item.get("title", "").lower()
            if any(kw.lower() in text for kw in cfg["triggers"]):
                hits.append(item["title"])
        if hits:
            active[theme] = hits[:3]
    return active


def _extract_commodities(global_mkts: dict) -> list[dict]:
    signals = []
    for sym, meta in COMMODITY_MAP.items():
        item = global_mkts.get(meta["yf_key"])
        if not item:
            for k, v in global_mkts.items():
                if v.get("name", "").lower() == sym.lower():
                    item = v
                    break
        if not item:
            continue
        pch   = item.get("pchange", 0) or 0
        price = item.get("price",   0) or 0
        if price == 0:
            continue
        direction = "UP" if pch > 0.3 else ("DOWN" if pch < -0.3 else "FLAT")
        magnitude = "HIGH" if abs(pch) >= 2.0 else ("MEDIUM" if abs(pch) >= 0.8 else "LOW")
        signals.append({
            "name":      sym,
            "price":     round(float(price), 2),
            "pchange":   round(float(pch),   2),
            "direction": direction,
            "magnitude": magnitude,
            "positive":  meta["positive"],
            "negative":  meta.get("negative", []),
            "pos_why":   meta["pos_why"],
            "neg_why":   meta.get("neg_why", ""),
        })
    return signals


# ── Live price context (NSE + TSR) ────────────────────────────────────────────

def _build_live_price_context(
    nifty500_ohlc:  list[dict],
    tsr_buildup:    dict,
    fno_buildup:    dict,
    commodity_signals: list[dict],
    geo_themes:     dict,
) -> str:
    """Build a live NSE price + multi-source signal table for all candidate stocks."""

    # ── Price lookup from NSE Nifty 500 data ──────────────────────────────
    price_map: dict[str, dict] = {}
    for s in (nifty500_ohlc or []):
        sym = (s.get("symbol") or "").upper()
        if sym:
            price_map[sym] = s

    # ── TSR signal sets ───────────────────────────────────────────────────
    tsr_long:  set[str] = set()
    tsr_short: set[str] = set()
    tsr_cover: set[str] = set()
    for item in (tsr_buildup or {}).get("long_buildup", []):
        raw = ((item.get("Name") or item.get("Code") or "")).split()[0].upper()
        if raw: tsr_long.add(raw)
    for item in (tsr_buildup or {}).get("short_buildup", []):
        raw = ((item.get("Name") or item.get("Code") or "")).split()[0].upper()
        if raw: tsr_short.add(raw)
    for item in (tsr_buildup or {}).get("short_covering", []):
        raw = ((item.get("Name") or item.get("Code") or "")).split()[0].upper()
        if raw: tsr_cover.add(raw)

    # ── NSE FNO signal sets ───────────────────────────────────────────────
    nse_long  = {s.get("symbol","") for s in (fno_buildup or {}).get("Long Buildup",   [])}
    nse_short = {s.get("symbol","") for s in (fno_buildup or {}).get("Short Buildup",  [])}
    nse_cover = {s.get("symbol","") for s in (fno_buildup or {}).get("Short Covering", [])}

    # ── Collect all candidate symbols ─────────────────────────────────────
    candidates: set[str] = set()
    for cfg in COMMODITY_MAP.values():
        candidates.update(cfg.get("positive", []))
        candidates.update(cfg.get("negative", []))
    for cfg in GEO_MAP.values():
        candidates.update(cfg.get("positive", []))
        candidates.update(cfg.get("negative", []))
    candidates.update(nse_long)
    candidates.update(nse_short)
    candidates.update(nse_cover)
    candidates.update(tsr_long)

    lines = [
        "\n=== LIVE NSE PRICES & MULTI-SOURCE SIGNALS (use these exact prices) ===",
        f"{'Symbol':<12} {'NSE Live':>10} {'Day%':>7} {'52W Pos':>7}  {'NSE OI':<16} {'TSR Signal':<14}",
        "─" * 75,
    ]

    found = 0
    for sym in sorted(candidates):
        data = price_map.get(sym)
        if not data:
            continue
        price  = data.get("close")  or data.get("lastPrice") or 0
        pch    = data.get("pchange") or 0
        yr_hi  = data.get("year_high") or 0
        yr_lo  = data.get("year_low")  or 0

        if price == 0:
            continue

        # 52-week position
        if yr_hi and yr_lo and yr_hi > yr_lo:
            pos52 = round((price - yr_lo) / (yr_hi - yr_lo) * 100)
            pos52_str = f"{pos52}%52W"
        else:
            pos52_str = "N/A"

        # NSE FNO signal
        if sym in nse_long:
            nse_sig = "Long Buildup ↑"
        elif sym in nse_short:
            nse_sig = "Short Buildup ↓"
        elif sym in nse_cover:
            nse_sig = "Short Cover ♻"
        else:
            nse_sig = "—"

        # TSR signal
        if sym in tsr_long:
            tsr_sig = "TSR Long ✓"
        elif sym in tsr_short:
            tsr_sig = "TSR Short ✗"
        elif sym in tsr_cover:
            tsr_sig = "TSR Cover ♻"
        else:
            tsr_sig = "—"

        pch_str = f"{pch:+.2f}%"
        lines.append(
            f"{sym:<12} ₹{price:>9.2f} {pch_str:>7} {pos52_str:>7}  {nse_sig:<16} {tsr_sig:<14}"
        )
        found += 1

    if found == 0:
        lines.append("  (NSE Nifty 500 data unavailable — AI will use estimated levels)")
    else:
        lines.append(f"\n  ⚠ USE ONLY THESE PRICES. Do NOT use training-data prices.")
        lines.append(f"  {found} candidate stocks with live NSE prices as of {datetime.now().strftime('%H:%M IST %d %b %Y')}")

    return "\n".join(lines)


# ── AI context builder ────────────────────────────────────────────────────────

def _build_ai_context(
    commodity_signals:  list[dict],
    geo_themes:         dict[str, list[str]],
    news_items:         list[dict],
    nifty_oi:           dict,
    fno_buildup:        dict,
    sectors:            list[dict],
    indices:            dict,
    nifty500_ohlc:      list[dict],
    tsr_buildup:        dict,
) -> tuple[str, dict]:
    """
    Assemble prompt context string and return (context_str, price_map).
    price_map: {symbol -> {close, year_high, year_low, pchange}}
    """
    price_map = {
        (s.get("symbol") or "").upper(): s
        for s in (nifty500_ohlc or [])
        if s.get("close") or s.get("lastPrice")
    }

    lines = []

    # ── A. Commodity signals ──────────────────────────────────────────────
    lines.append("=== A. COMMODITY SIGNALS (today's moves) ===")
    for c in commodity_signals:
        arrow = "↑" if c["direction"] == "UP" else ("↓" if c["direction"] == "DOWN" else "→")
        lines.append(
            f"  {c['name']}: ${c['price']} ({arrow}{c['pchange']:+.2f}%)  [{c['magnitude']}]"
        )
        if c["direction"] != "FLAT":
            if c["positive"]:
                lines.append(f"    + Beneficiary stocks: {', '.join(c['positive'][:6])} — {c['pos_why']}")
            if c["negative"]:
                lines.append(f"    - Hurt stocks: {', '.join(c['negative'][:4])} — {c['neg_why']}")

    # ── B. Geopolitical & national themes ─────────────────────────────────
    lines.append("\n=== B. GEOPOLITICAL & NATIONAL THEMES ===")
    if geo_themes:
        for theme, headlines in geo_themes.items():
            cfg = GEO_MAP.get(theme, {})
            lines.append(f"  ACTIVE: {theme.upper().replace('_', ' ')}")
            for h in headlines[:2]:
                lines.append(f"    News: \"{h}\"")
            if cfg.get("positive"):
                lines.append(f"    → Positive stocks: {', '.join(cfg['positive'][:6])}")
            if cfg.get("pos_why"):
                lines.append(f"      Reason: {cfg['pos_why']}")
            if cfg.get("negative"):
                lines.append(f"    → Negative stocks: {', '.join(cfg['negative'][:4])}")
    else:
        lines.append("  No major geopolitical triggers in current news")

    # ── C. National/policy news ───────────────────────────────────────────
    lines.append("\n=== C. INDIA MARKET & POLICY NEWS (top 12) ===")
    for item in news_items[:12]:
        lines.append(f"  • {item['title']}  [{item.get('source', '')}]")

    # ── D. Nifty OI & F&O sentiment ───────────────────────────────────────
    lines.append("\n=== D. NIFTY 50 TREND & F&O SENTIMENT ===")
    nifty = (indices or {}).get("nifty50", {})
    vix   = (indices or {}).get("indiavix", {})
    nifty_val = nifty.get("value", "N/A")
    nifty_pch = nifty.get("pchange", 0) or 0
    lines.append(f"  Nifty 50: {nifty_val} ({nifty_pch:+.2f}%)")
    vix_val = vix.get("value") or 0
    vix_mood = "High fear" if vix_val > 20 else "Moderate" if vix_val > 15 else "Low volatility"
    lines.append(f"  India VIX: {vix_val} — {vix_mood}")

    pcr  = (nifty_oi or {}).get("oi_opinion", {}).get("pcr", "N/A")
    sent = (nifty_oi or {}).get("oi_opinion", {}).get("sentiment", "N/A")
    supp = (nifty_oi or {}).get("oi_opinion", {}).get("support", "N/A")
    res  = (nifty_oi or {}).get("oi_opinion", {}).get("resistance", "N/A")
    lines.append(f"  PCR: {pcr} → Sentiment: {sent}")
    lines.append(f"  OI Support: {supp}  |  OI Resistance: {res}")

    # NSE FNO buildup
    lb = [s.get("symbol", "") for s in (fno_buildup or {}).get("Long Buildup",   [])[:8]]
    sb = [s.get("symbol", "") for s in (fno_buildup or {}).get("Short Buildup",  [])[:5]]
    sc = [s.get("symbol", "") for s in (fno_buildup or {}).get("Short Covering", [])[:5]]
    if lb: lines.append(f"  NSE Long Buildup (fresh longs): {', '.join(lb)}")
    if sb: lines.append(f"  NSE Short Buildup (fresh shorts): {', '.join(sb)}")
    if sc: lines.append(f"  NSE Short Covering: {', '.join(sc)}")

    # TSR buildup
    tsr_lb = [
        (item.get("Name") or item.get("Code") or "").split()[0].upper()
        for item in (tsr_buildup or {}).get("long_buildup", [])[:8]
    ]
    tsr_sb = [
        (item.get("Name") or item.get("Code") or "").split()[0].upper()
        for item in (tsr_buildup or {}).get("short_buildup", [])[:5]
    ]
    tsr_lb = [s for s in tsr_lb if s]
    tsr_sb = [s for s in tsr_sb if s]
    if tsr_lb: lines.append(f"  TSR Pro Long Buildup: {', '.join(tsr_lb)}")
    if tsr_sb: lines.append(f"  TSR Pro Short Buildup: {', '.join(tsr_sb)}")

    # Cross-confirmed (in both NSE and TSR long buildup)
    cross = [s for s in lb if s in tsr_lb]
    if cross:
        lines.append(f"  ⭐ NSE+TSR Cross-Confirmed Long: {', '.join(cross)}  ← HIGHEST CONVICTION")

    # ── E. Sector rotation ────────────────────────────────────────────────
    lines.append("\n=== E. SECTOR ROTATION (daily change) ===")
    sorted_sec = sorted(sectors or [], key=lambda x: x.get("pchange", 0) or 0, reverse=True)
    top3 = [f"{s['short']} ({s.get('pchange', 0):+.1f}%)" for s in sorted_sec[:3]]
    bot3 = [f"{s['short']} ({s.get('pchange', 0):+.1f}%)" for s in sorted_sec[-3:]]
    if top3: lines.append(f"  Leading sectors:  {' | '.join(top3)}")
    if bot3: lines.append(f"  Lagging sectors:  {' | '.join(bot3)}")

    # ── F. Nifty 500 top movers ───────────────────────────────────────────
    lines.append("\n=== F. NIFTY 500 TOP MOVERS TODAY ===")
    top_g = sorted(nifty500_ohlc or [], key=lambda x: x.get("pchange", 0) or 0, reverse=True)[:10]
    top_l = sorted(nifty500_ohlc or [], key=lambda x: x.get("pchange", 0) or 0)[:5]
    g_str = ", ".join(
        f"{s['symbol']}(₹{s.get('close',0):.0f}, {s.get('pchange',0):+.1f}%)"
        for s in top_g
    )
    l_str = ", ".join(
        f"{s['symbol']}(₹{s.get('close',0):.0f}, {s.get('pchange',0):+.1f}%)"
        for s in top_l
    )
    if g_str: lines.append(f"  Top gainers: {g_str}")
    if l_str: lines.append(f"  Top losers:  {l_str}")

    # ── G. Live price table (the critical section) ────────────────────────
    live_ctx = _build_live_price_context(
        nifty500_ohlc, tsr_buildup, fno_buildup, commodity_signals, geo_themes
    )
    lines.append(live_ctx)

    return "\n".join(lines), price_map


# ── Price validation helpers ───────────────────────────────────────────────────

def _parse_price(val: str) -> float | None:
    """Parse '285', '285-295', '₹285', '₹285-295' → midpoint float."""
    if not val:
        return None
    val = str(val).replace("₹", "").replace(",", "").strip()
    if "-" in val:
        parts = val.split("-")
        try:
            return (float(parts[0].strip()) + float(parts[1].strip())) / 2
        except Exception:
            return None
    try:
        return float(val)
    except Exception:
        return None


def _fmt_price_zone(price: float, pct_low: float, pct_high: float) -> str:
    """Return a price range string like '285.50-292.00'."""
    lo = round(price * (1 + pct_low  / 100) / 0.5) * 0.5
    hi = round(price * (1 + pct_high / 100) / 0.5) * 0.5
    if lo == hi:
        return f"{lo:.2f}"
    return f"{lo:.2f}-{hi:.2f}"


def _post_process_recs(recs: list[dict], price_map: dict) -> list[dict]:
    """
    Validate AI price levels against live NSE data.
    If entry/target/SL are >40% off from live price, recalculate.
    """
    out = []
    for rec in recs:
        sym   = (rec.get("symbol") or "").upper()
        data  = price_map.get(sym)
        live  = float(data.get("close") or data.get("lastPrice") or 0) if data else 0

        if live <= 0:
            out.append(rec)
            continue

        action = rec.get("action", "WATCH")

        # Check entry zone
        entry_mid = _parse_price(rec.get("entry_zone", ""))
        if entry_mid and abs(entry_mid - live) / live > 0.40:
            # AI gave stale price — recalculate from live
            logger.info("Price fix for %s: AI entry %s vs live ₹%.2f", sym, rec["entry_zone"], live)
            if action == "BUY":
                rec["entry_zone"] = _fmt_price_zone(live, -0.5, +0.5)
                rec["target"]     = str(round(live * 1.07, 2))
                rec["stop_loss"]  = str(round(live * 0.96, 2))
            elif action == "WATCH":
                rec["entry_zone"] = _fmt_price_zone(live, -0.5, +1.0)
                rec["target"]     = str(round(live * 1.06, 2))
                rec["stop_loss"]  = str(round(live * 0.97, 2))
            # AVOID doesn't need price levels
        else:
            # Validate target and SL individually
            tgt_mid = _parse_price(rec.get("target", ""))
            sl_mid  = _parse_price(rec.get("stop_loss", ""))
            if tgt_mid and abs(tgt_mid - live) / live > 0.40:
                rec["target"] = str(round(live * 1.07, 2))
            if sl_mid and abs(sl_mid - live) / live > 0.40:
                rec["stop_loss"] = str(round(live * 0.96, 2))

        out.append(rec)
    return out


# ── AI call ───────────────────────────────────────────────────────────────────

def _call_ai(context: str) -> dict:
    from services.openai_service import _get_client

    prompt = f"""You are a senior Indian equity market analyst at a leading brokerage.
Today: {datetime.now().strftime('%d %b %Y %H:%M IST')}

Below is comprehensive real-time market intelligence. Section G contains LIVE NSE prices
fetched right now — you MUST use those exact price levels for all entry/target/stop-loss.
Do NOT use prices from your training data — they are outdated.

{context}

=== ANALYSIS TASK (follow all 6 steps before recommending) ===
Step 1 — COMMODITY: Which stocks from Section A have the strongest commodity tailwind/headwind today?
Step 2 — GEOPOLITICAL/NEWS: Which stocks from Section B/C are directly affected by active themes?
Step 3 — NIFTY CORRELATION: Rising Nifty → prefer high-beta (>1.2) sector leaders. Falling → defensives (<0.8 beta).
Step 4 — F&O CONFIRMATION: NSE+TSR cross-confirmed Long Buildup stocks = HIGHEST conviction. Short Covering = momentum squeeze.
Step 5 — SECTOR ALIGNMENT: Only recommend stocks in LEADING sectors (Section E). Avoid stocks in LAGGING sectors.
Step 6 — SYNTHESIS: Cross-confirmed stocks (multiple factors aligned) = BUY. Single-factor only = WATCH. Headwind + lagging = AVOID.

=== CRITICAL PRICE RULES ===
• entry_zone MUST be within ±2% of the live NSE price in Section G
• target: 5-10% above entry for positional; 2-4% for intraday
• stop_loss: 2-4% below entry (never more than 5% below)
• If a stock is NOT in Section G (no live price), omit entry/target/SL or write "market"
• Use format "285.50-292.00" for ranges or "285.50" for single levels (no ₹ symbol)

OUTPUT (strict JSON only, no extra text):
{{
  "market_thesis": "3-4 sentence overall market view combining all 6 factors",
  "pre_analysis_summary": {{
    "commodity_impact": "Which commodities moved and exactly which NSE stocks benefit/suffer",
    "geo_news_impact": "Active geo themes and directly affected stocks with reason",
    "nifty_trend": "Nifty level, direction, key OI support/resistance, VIX reading",
    "fno_bias": "NSE+TSR cross-confirmed signals — bullish/bearish/neutral",
    "sector_bias": "Leading vs lagging sectors and what this means for stock selection"
  }},
  "recommendations": [
    {{
      "symbol": "STOCKSYMBOL",
      "action": "BUY or WATCH or AVOID",
      "conviction": "HIGH or MEDIUM or LOW",
      "entry_zone": "price based on live NSE price e.g. 285.50-290.00",
      "target": "price e.g. 305.00",
      "stop_loss": "price e.g. 276.00",
      "holding": "Intraday or 2-3 Days or Positional (1-2 weeks)",
      "rationale": "2-3 sentences citing SPECIFIC factors: commodity move, NSE/TSR OI signal, sector, Nifty level",
      "primary_driver": "commodity or geopolitical or fno or sector",
      "factors": ["factor1","factor2"],
      "nifty_beta_note": "e.g. High beta ~1.4 — amplifies Nifty moves",
      "risk": "specific risk that invalidates the trade"
    }}
  ],
  "stocks_to_avoid": [
    {{
      "symbol": "SYMBOL",
      "reason": "specific quantified reason e.g. crude +3% raises jet fuel cost — margin compression"
    }}
  ],
  "watchlist": ["SYM1", "SYM2", "SYM3"]
}}

Generate 6-10 recommendations. Prioritise cross-confirmed stocks.
For AVOID, give specific quantified reasons tied to today's data."""

    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=2500,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("Recommendation AI failed: %s", exc)
        return {"error": str(exc), "recommendations": [], "stocks_to_avoid": []}


# ── Public API ─────────────────────────────────────────────────────────────────

async def generate_recommendations(force: bool = False) -> dict:
    """Aggregate all data, run 6-factor analysis, return AI-powered recommendations."""
    if not force and "r" in _cache:
        return _cache["r"]

    logger.info("Recommendation engine: starting full analysis")

    from services.global_markets_service import get_global_markets
    from services.news_service import get_policy_news
    from services.nse_service import (
        get_nifty_oi_analysis, get_fno_oi_buildup, get_sector_rotation,
        get_nifty500_ohlc, get_index_quotes,
    )
    from services.tsr_service import get_tsr_buildup

    # ── Parallel data fetch ────────────────────────────────────────────────
    (
        global_mkts, policy_news, nifty_oi, fno_buildup,
        sectors, nifty500_ohlc, geo_news, idx_data, tsr_buildup,
    ) = await asyncio.gather(
        _safe(get_global_markets()),
        _safe(get_policy_news("India", "NSE market")),
        _safe(get_nifty_oi_analysis()),
        _safe(get_fno_oi_buildup(10)),
        _safe(get_sector_rotation()),
        _safe(get_nifty500_ohlc()),
        _fetch_geo_national_news(),
        _safe(get_index_quotes()),
        _safe(get_tsr_buildup()),
    )

    global_mkts   = global_mkts   or {}
    policy_news   = policy_news   or []
    nifty_oi      = nifty_oi      or {}
    fno_buildup   = fno_buildup   or {}
    sectors       = sectors       or []
    nifty500_ohlc = nifty500_ohlc or []
    tsr_buildup   = tsr_buildup   or {}
    idx_data      = idx_data      or {}

    commodity_signals = _extract_commodities(global_mkts)
    all_news          = (geo_news or []) + (policy_news or [])
    geo_themes        = _detect_geo_themes(all_news)

    context, price_map = _build_ai_context(
        commodity_signals,
        geo_themes,
        all_news,
        nifty_oi,
        fno_buildup,
        sectors,
        idx_data,
        nifty500_ohlc,
        tsr_buildup,
    )

    # ── AI synthesis ──────────────────────────────────────────────────────
    ai_result = await asyncio.to_thread(_call_ai, context)

    # ── Post-process: validate price levels against live NSE data ─────────
    recs = _post_process_recs(
        ai_result.get("recommendations", []),
        price_map,
    )
    ai_result["recommendations"] = recs

    result = {
        **ai_result,
        "commodity_signals":  commodity_signals,
        "geo_themes_active":  list(geo_themes.keys()),
        "news_count":         len(all_news),
        "tsr_long_count":     len(tsr_buildup.get("long_buildup", [])),
        "price_data_stocks":  len(price_map),
        "timestamp":          datetime.now().isoformat(),
        "cache_expires_min":  30,
    }
    _cache["r"] = result
    logger.info(
        "Recommendations: %d picks, %d avoid, %d live prices, themes: %s",
        len(recs),
        len(ai_result.get("stocks_to_avoid", [])),
        len(price_map),
        list(geo_themes.keys()),
    )
    return result


async def _safe(coro):
    try:
        return await coro
    except Exception as exc:
        logger.debug("Recommendation fetch error: %s", exc)
        return None


# ── PDF Builder ────────────────────────────────────────────────────────────────

def build_recommendations_pdf(rec_data: dict) -> bytes:
    """Build a formatted A4 PDF from AI recommendation data."""
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY

    DARK_BLUE = colors.HexColor("#1e3a5f")
    GREEN     = colors.HexColor("#059669")
    RED       = colors.HexColor("#dc2626")
    AMBER     = colors.HexColor("#d97706")
    PURPLE    = colors.HexColor("#7c3aed")
    TEAL      = colors.HexColor("#0e7490")
    COBALT    = colors.HexColor("#1d4ed8")
    GRAY      = colors.HexColor("#6b7280")
    BORDER    = colors.HexColor("#e5e7eb")
    ALT_ROW   = colors.HexColor("#f9fafb")
    CONV_CLR  = {"HIGH": GREEN, "MEDIUM": AMBER, "LOW": GRAY}

    def sty(name, font="Helvetica", size=9, color=DARK_BLUE,
            align=TA_LEFT, before=0, after=2, bold=False, italic=False):
        fn = ("Helvetica-BoldOblique" if (bold and italic)
              else "Helvetica-Bold"    if bold
              else "Helvetica-Oblique" if italic
              else font)
        return ParagraphStyle(name, fontName=fn, fontSize=size, textColor=color,
                              alignment=align, spaceBefore=before, spaceAfter=after)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=12*mm,  bottomMargin=12*mm,
    )
    story = []

    recs        = rec_data.get("recommendations", [])
    avoids      = rec_data.get("stocks_to_avoid", [])
    watchlist   = rec_data.get("watchlist", [])
    thesis      = rec_data.get("market_thesis", "")
    pre         = rec_data.get("pre_analysis_summary") or {}
    commodities = rec_data.get("commodity_signals", [])
    geo_themes  = rec_data.get("geo_themes_active", [])
    price_stocks = rec_data.get("price_data_stocks", 0)
    tsr_count    = rec_data.get("tsr_long_count", 0)

    ts = rec_data.get("timestamp", "")
    try:
        date_str = datetime.fromisoformat(ts).strftime("%d %b %Y  %H:%M IST")
    except Exception:
        date_str = datetime.now().strftime("%d %b %Y  %H:%M IST")

    buy_recs   = [r for r in recs if r.get("action") == "BUY"]
    watch_recs = [r for r in recs if r.get("action") == "WATCH"]
    avoid_recs = [r for r in recs if r.get("action") == "AVOID"]
    comm_active = [c for c in commodities if c.get("direction") != "FLAT"]

    # ── Cover ────────────────────────────────────────────────────────────────
    story.append(Paragraph(
        "RRE AI Stock Recommendations",
        sty("T", size=16, color=DARK_BLUE, align=TA_CENTER, bold=True, after=3),
    ))
    story.append(Paragraph(
        f"6-Factor Pre-Analysis  ·  {date_str}  ·  NSE Live Prices + TSR Pro",
        sty("S", size=8, color=GRAY, align=TA_CENTER, after=2),
    ))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=DARK_BLUE))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        f"🟢 BUY: <b>{len(buy_recs)}</b>   ·   "
        f"🟡 WATCH: <b>{len(watch_recs)}</b>   ·   "
        f"🚫 AVOID: <b>{len(avoids) + len(avoid_recs)}</b>   ·   "
        f"Live Prices: <b>{price_stocks}</b> stocks   ·   "
        f"TSR Signals: <b>{tsr_count}</b>   ·   "
        f"Geo Themes: <b>{len(geo_themes)}</b>",
        sty("SUM", size=8, color=DARK_BLUE, align=TA_CENTER, after=2),
    ))
    story.append(Spacer(1, 5*mm))

    def _sec_hdr(title, subtitle, color):
        story.append(HRFlowable(width="100%", thickness=1.2, color=color))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(title, sty("SH", size=11, color=color, bold=True,
                                          align=TA_CENTER, before=2, after=2)))
        if subtitle:
            story.append(Paragraph(subtitle, sty("SD", size=7, color=GRAY,
                                                 italic=True, align=TA_CENTER, after=4)))

    # ── Market Thesis ─────────────────────────────────────────────────────────
    if thesis:
        _sec_hdr("📊 Market Thesis", "AI synthesis — all 6 factors combined", COBALT)
        story.append(Paragraph(
            thesis, sty("TH", size=9, color=DARK_BLUE, align=TA_JUSTIFY, before=2, after=4),
        ))
        story.append(Spacer(1, 4*mm))

    # ── Pre-Analysis Summary ──────────────────────────────────────────────────
    pa_items = [
        ("🛢️ Commodity",   pre.get("commodity_impact", "")),
        ("🌍 Geo/News",    pre.get("geo_news_impact",  "")),
        ("📊 Nifty Trend", pre.get("nifty_trend",      "")),
        ("📈 F&O Bias",    pre.get("fno_bias",         "")),
        ("🏆 Sector",      pre.get("sector_bias",      "")),
    ]
    pa_items = [(k, v) for k, v in pa_items if v]
    if pa_items:
        _sec_hdr("🔍 Pre-Analysis Summary", "6-factor analysis before stock selection", PURPLE)
        pa_rows = [
            [
                Paragraph(k, sty(f"pk{i}", size=7.5, color=COBALT, bold=True)),
                Paragraph(v, sty(f"pv{i}", size=7.5, color=DARK_BLUE)),
            ]
            for i, (k, v) in enumerate(pa_items)
        ]
        t_pa = Table(pa_rows, colWidths=[42*mm, 138*mm])
        t_pa.setStyle(TableStyle([
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("BACKGROUND",    (0, 0), (0, -1),  colors.HexColor("#f5f3ff")),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ]))
        story.append(t_pa)
        story.append(Spacer(1, 5*mm))

    # ── Commodity Signals ────────────────────────────────────────────────────
    if comm_active:
        _sec_hdr(f"⚡ Commodity Signals  ({len(comm_active)} moving)",
                 "Non-flat commodities and their NSE stock impact", AMBER)
        c_hdr = [["Commodity", "Price ($)", "Chg %", "Dir", "Beneficiary Stocks (NSE)", "Hurt By"]]
        c_data = [
            [
                c["name"],
                f"{c['price']:,.2f}",
                f"{c['pchange']:+.2f}%",
                f"{'↑' if c['direction']=='UP' else '↓'}",
                ", ".join(c.get("positive", [])[:5]),
                ", ".join(c.get("negative", [])[:3]) or "—",
            ]
            for c in comm_active
        ]
        t_c = Table(c_hdr + c_data, colWidths=[27*mm, 22*mm, 16*mm, 12*mm, 56*mm, 47*mm])
        ts_c = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), AMBER),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (4, 1), (5, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        for i, c in enumerate(comm_active, 1):
            ts_c.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            clr = GREEN if c["pchange"] >= 0 else RED
            ts_c.add("TEXTCOLOR", (2, i), (3, i), clr)
            ts_c.add("FONTNAME",  (3, i), (3, i), "Helvetica-Bold")
        t_c.setStyle(ts_c)
        story.append(t_c)
        story.append(Spacer(1, 5*mm))

    # ── Shared rec table builder ──────────────────────────────────────────────
    def _rec_table(rec_list, hdr_color, title, subtitle):
        if not rec_list:
            return
        _sec_hdr(title, subtitle, hdr_color)
        r_hdr = ["#", "Symbol", "Conv", "Entry ₹", "Target ₹", "SL ₹", "Holding", "Rationale / Risk"]
        cw = [7*mm, 22*mm, 14*mm, 26*mm, 20*mm, 18*mm, 24*mm, 0]
        cw[-1] = 160*mm - sum(cw[:-1])
        rows = [r_hdr]
        for rank, r in enumerate(rec_list, 1):
            rat  = (r.get("rationale") or "")[:160]
            risk = r.get("risk", "")
            detail = rat + (f"  ⚠ {risk}" if risk else "")
            hold = (r.get("holding") or "")[:20]
            rows.append([
                str(rank),
                r.get("symbol", ""),
                r.get("conviction", "—"),
                r.get("entry_zone", "—"),
                r.get("target", "—"),
                r.get("stop_loss", "—"),
                hold,
                detail,
            ])
        t = Table(rows, colWidths=cw)
        ts = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), hdr_color),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (1, 1), (1, -1), "LEFT"),
            ("ALIGN",         (7, 1), (7, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ])
        for i, r in enumerate(rec_list, 1):
            ts.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            ts.add("TEXTCOLOR",  (2, i), (2, i),  CONV_CLR.get(r.get("conviction", ""), GRAY))
            ts.add("FONTNAME",   (2, i), (2, i),  "Helvetica-Bold")
            ts.add("TEXTCOLOR",  (4, i), (4, i),  GREEN)
            ts.add("TEXTCOLOR",  (5, i), (5, i),  RED)
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 5*mm))

    _rec_table(buy_recs,   GREEN, f"🟢 BUY Recommendations  ({len(buy_recs)} stocks)",
               "Entry · Target · SL based on live NSE prices  ·  NSE+TSR cross-confirmed")
    _rec_table(watch_recs, AMBER, f"🟡 Watch List  ({len(watch_recs)} stocks)",
               "Monitor for entry signals — conditions not yet fully confirmed")

    # ── Stocks to Avoid ───────────────────────────────────────────────────────
    all_avoids = list(avoids) + [
        {"symbol": r.get("symbol", ""), "reason": (r.get("rationale") or "")[:150]}
        for r in avoid_recs
    ]
    if all_avoids:
        _sec_hdr(f"🚫 Stocks to Avoid  ({len(all_avoids)})",
                 "Specific reasons tied to today's commodity / geo / sector data", RED)
        av_rows = [["Symbol", "Reason"]]
        for a in all_avoids:
            av_rows.append([a.get("symbol", ""), (a.get("reason") or "—")[:200]])
        t_av = Table(av_rows, colWidths=[35*mm, 145*mm])
        ts_av = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), RED),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (0, -1), "CENTER"),
            ("ALIGN",         (1, 1), (1, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7),
            ("TEXTCOLOR",     (0, 1), (0, -1), RED),
            ("FONTNAME",      (0, 1), (0, -1), "Helvetica-Bold"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ])
        for i in range(1, len(all_avoids) + 1):
            ts_av.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
        t_av.setStyle(ts_av)
        story.append(t_av)
        story.append(Spacer(1, 4*mm))

    # ── Watchlist ─────────────────────────────────────────────────────────────
    if watchlist:
        _sec_hdr("👁 Watchlist", "Monitor for entry opportunities", TEAL)
        story.append(Paragraph(
            "   ·   ".join(watchlist),
            sty("WL", size=9.5, color=TEAL, align=TA_CENTER, bold=True, before=2, after=4),
        ))
        story.append(Spacer(1, 4*mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
    story.append(Paragraph(
        f"Generated by RRE Market Scanner  ·  {date_str}  ·  "
        f"Data: NSE India (live prices) · TSR Pro · Yahoo Finance · OpenAI GPT-4o",
        sty("F", size=6.5, color=GRAY, align=TA_CENTER, before=6),
    ))
    story.append(Paragraph(
        "For informational purposes only. Not financial advice. "
        "Verify all prices before trading.",
        sty("D", size=6, color=GRAY, align=TA_CENTER, italic=True, before=2),
    ))
    doc.build(story)
    return buf.getvalue()


async def send_recommendations_report(rec_data: dict | None = None) -> None:
    """Build PDF from rec_data (or generate fresh) and send to Telegram."""
    from services.telegram_service import send_document, send_message

    if rec_data is None:
        rec_data = await generate_recommendations()

    recs = rec_data.get("recommendations", [])
    if not recs:
        await send_message("⚠️ AI Recommendations: no picks generated — analysis may have failed.")
        return

    pdf_bytes = await asyncio.to_thread(build_recommendations_pdf, rec_data)

    buy_list   = [r["symbol"] for r in recs if r.get("action") == "BUY"]
    watch_list = [r["symbol"] for r in recs if r.get("action") == "WATCH"]
    avoids     = [a["symbol"] for a in rec_data.get("stocks_to_avoid", [])]
    geo        = rec_data.get("geo_themes_active", [])
    comms      = [c for c in rec_data.get("commodity_signals", []) if c.get("direction") != "FLAT"]

    GEO_LABEL = {
        "defense": "🛡️ Defense", "energy_crisis": "⛽ Energy",
        "rupee_pressure": "💱 Rupee", "rate_policy": "🏦 Rate Policy",
        "infra_policy": "🏗️ Infra",
    }

    thesis_short = (rec_data.get("market_thesis") or "")[:220]
    ts = rec_data.get("timestamp", "")
    try:
        dt_str = datetime.fromisoformat(ts).strftime("%d %b %Y  %H:%M IST")
    except Exception:
        dt_str = datetime.now().strftime("%d %b %Y  %H:%M IST")

    lines = [f"🎯 <b>RRE AI Recommendations — {dt_str}</b>", ""]
    if thesis_short:
        lines += [f"📊 {thesis_short}", ""]

    if buy_list:
        lines.append(f"🟢 <b>BUY ({len(buy_list)})</b>: {' · '.join(buy_list[:8])}")
    if watch_list:
        lines.append(f"🟡 <b>WATCH ({len(watch_list)})</b>: {' · '.join(watch_list[:6])}")
    if avoids:
        lines.append(f"🚫 <b>AVOID ({len(avoids)})</b>: {' · '.join(avoids[:5])}")

    lines.append("")
    if comms:
        c_str = "  ".join(
            f"{'↑' if c['direction']=='UP' else '↓'}{c['name']} {c['pchange']:+.1f}%"
            for c in comms[:4]
        )
        lines.append(f"⚡ <b>Commodities:</b> {c_str}")
    if geo:
        lines.append(f"🌍 <b>Active Themes:</b> {' · '.join(GEO_LABEL.get(t, t) for t in geo)}")

    lines += [
        "",
        f"📦 {len(recs)} picks · {rec_data.get('price_data_stocks', 0)} live NSE prices · TSR Pro signals",
        "📑 Full analysis + entry/target/SL in PDF",
    ]

    filename = f"RRE_AI_Rec_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    await send_document(pdf_bytes, filename, "\n".join(lines))
    logger.info("AI Recommendations PDF sent — %d picks", len(recs))
