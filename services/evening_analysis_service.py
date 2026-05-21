"""
evening_analysis_service.py — 8 PM comprehensive daily analysis PDF.
Covers: Top 10 Nifty 50 + Top 10 Nifty 500 gainers, candlestick patterns,
RSI, VWMA, volume, news/announcements, FII/DII, AI reasoning per stock.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)
_IST = timezone(timedelta(hours=5, minutes=30))


# ── Yahoo Finance OHLCV ───────────────────────────────────────────────────────

async def _fetch_ohlcv(symbols: list[str], days: int = 30) -> dict[str, list[dict]]:
    """Fetch daily OHLCV for each symbol from Yahoo Finance. Returns {symbol: [ohlcv]}."""
    result: dict[str, list[dict]] = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    range_map = {30: "1mo", 60: "3mo", 90: "3mo"}
    yf_range = range_map.get(days, "1mo")

    async def _fetch_one(sym: str) -> tuple[str, list[dict]]:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS"
               f"?interval=1d&range={yf_range}")
        try:
            async with httpx.AsyncClient(timeout=10, headers=headers) as client:
                r    = await client.get(url)
                data = r.json()["chart"]["result"][0]
                ts   = data["timestamp"]
                q    = data["indicators"]["quote"][0]
                rows = []
                for i, t in enumerate(ts):
                    o = q["open"][i]; h = q["high"][i]
                    l = q["low"][i];  c = q["close"][i]; v = q["volume"][i]
                    if all(x is not None for x in (o, h, l, c)):
                        rows.append({"open": o, "high": h, "low": l,
                                     "close": c, "volume": v or 0})
                return sym, rows
        except Exception:
            return sym, []

    tasks   = [_fetch_one(s) for s in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, tuple):
            result[r[0]] = r[1]
    return result


# ── AI per-stock explanation ──────────────────────────────────────────────────

def _generate_stock_ai(gainers_data: list[dict]) -> dict[str, str]:
    """
    Batch AI call: given list of {symbol, pchange, patterns, rsi, vwma_status,
    vol_ratio, trend, news}, returns {symbol: explanation_text}.
    """
    try:
        from services.openai_service import _get_client, _under_limit, _record_cost
        if not _under_limit():
            return {}

        lines = []
        for g in gainers_data[:12]:
            sym      = g["symbol"]
            pchange  = g.get("pchange", 0)
            patterns = ", ".join(g.get("patterns", [])) or "None detected"
            rsi      = g.get("rsi", 50)
            rsi_lbl  = g.get("rsi_label", "Neutral")
            vol_r    = g.get("vol_ratio", 1.0)
            vwma_st  = "Above VWMA (bullish)" if g.get("above_vwma") else "Below VWMA (caution)"
            trend    = g.get("trend", "Unknown")
            news     = "; ".join(g.get("news", [])[:2]) or "No announcements"
            lines.append(
                f"{sym}: +{pchange:.2f}%  Pattern={patterns}  RSI={rsi}({rsi_lbl})  "
                f"{vwma_st}  Volume={vol_r}x avg  Trend={trend}  News={news}"
            )

        prompt = (
            "You are a senior Indian equity research analyst.\n"
            "For each stock below, write EXACTLY 50 words explaining WHY it gained today. "
            "Cover: technical signal (candlestick pattern, RSI level, volume surge), "
            "fundamental catalyst (news/policy/deal/earnings/FII buying), "
            "and sector/macro context. Be specific — mention the actual pattern and catalyst.\n\n"
            + "\n".join(lines)
            + "\n\nRespond ONLY in JSON (no markdown). Each value must be ~50 words: "
            '{"SYMBOL": "50-word explanation", ...}'
        )
        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        _record_cost(resp.usage)
        import json
        return json.loads(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("Evening AI failed: %s", exc)
        return {}


# ── PDF builder ───────────────────────────────────────────────────────────────

def _build_evening_pdf(date_str: str, nse_gainers: list[dict],
                       nifty500_gainers: list[dict], fii_dii: dict,
                       tech_map: dict, ai_map: dict) -> bytes:
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             leftMargin=12*mm, rightMargin=12*mm,
                             topMargin=12*mm, bottomMargin=12*mm)

    GREEN  = colors.HexColor("#22c55e")
    RED    = colors.HexColor("#ef4444")
    YELLOW = colors.HexColor("#f59e0b")
    BLUE   = colors.HexColor("#3b82f6")
    SLATE  = colors.HexColor("#1e293b")
    NAVY   = colors.HexColor("#0f172a")
    WHITE  = colors.white
    LGRAY  = colors.HexColor("#94a3b8")

    def _p(txt, size=8, bold=False, color=WHITE, align=TA_LEFT):
        return Paragraph(txt, ParagraphStyle("x", fontSize=size, leading=size+3,
                          textColor=color, alignment=align,
                          fontName="Helvetica-Bold" if bold else "Helvetica"))

    def _tbl(rows, cw, hdr_color=SLATE):
        t  = Table(rows, colWidths=cw, repeatRows=1)
        ts = TableStyle([
            ("BACKGROUND",     (0,0),(-1,0), hdr_color),
            ("TEXTCOLOR",      (0,0),(-1,0), WHITE),
            ("FONTNAME",       (0,0),(-1,0), "Helvetica-Bold"),
            ("FONTSIZE",       (0,0),(-1,-1), 7.5),
            ("ROWBACKGROUNDS", (0,1),(-1,-1), [NAVY, colors.HexColor("#111827")]),
            ("TEXTCOLOR",      (0,1),(-1,-1), WHITE),
            ("GRID",           (0,0),(-1,-1), 0.3, colors.HexColor("#1e1e30")),
            ("LEFTPADDING",    (0,0),(-1,-1), 4),
            ("RIGHTPADDING",   (0,0),(-1,-1), 4),
            ("TOPPADDING",     (0,0),(-1,-1), 3),
            ("BOTTOMPADDING",  (0,0),(-1,-1), 3),
        ])
        return t, ts

    def _hr():
        return HRFlowable(width="100%", thickness=0.5, color=SLATE, spaceAfter=4)

    story = []

    # ── Title ─────────────────────────────────────────────────────────────────
    story.append(_p("RRE EVENING ANALYSIS REPORT", size=14, bold=True,
                    color=YELLOW, align=TA_CENTER))
    story.append(_p(date_str, size=9, color=LGRAY, align=TA_CENTER))
    story.append(Spacer(1, 3*mm))
    story.append(_hr())

    # ── FII / DII Activity ────────────────────────────────────────────────────
    story.append(_p("FII / DII Market Activity Today", size=10, bold=True, color=BLUE))
    story.append(Spacer(1, 2*mm))
    if fii_dii:
        fii_rows = [["Category", "Buy (₹Cr)", "Sell (₹Cr)", "Net (₹Cr)", "Sentiment"]]
        cw_f = [40*mm, 32*mm, 32*mm, 32*mm, 30*mm]
        for cat, v in fii_dii.items():
            net  = v["net_value"]
            sent = ("Buying ▲" if net > 0 else "Selling ▼")
            fii_rows.append([cat,
                             f"{v['buy_value']:,.0f}", f"{v['sell_value']:,.0f}",
                             f"{net:+,.0f}", sent])
        t_f, ts_f = _tbl(fii_rows, cw_f, hdr_color=BLUE)
        for i, (cat, v) in enumerate(fii_dii.items(), 1):
            c = GREEN if v["net_value"] > 0 else RED
            ts_f.add("TEXTCOLOR", (3, i), (4, i), c)
            ts_f.add("FONTNAME",  (3, i), (4, i), "Helvetica-Bold")
        t_f.setStyle(ts_f)
        story.append(t_f)
    else:
        story.append(_p("FII/DII data unavailable.", size=8, color=LGRAY))
    story.append(Spacer(1, 4*mm))
    story.append(_hr())

    # ── Gainers section ───────────────────────────────────────────────────────
    def _gainers_section(title, gainers, hdr_color):
        story.append(_p(title, size=10, bold=True, color=hdr_color))
        story.append(Spacer(1, 2*mm))
        if not gainers:
            story.append(_p("No data.", size=8, color=LGRAY))
            return

        hdr = ["#", "Symbol", "LTP", "Chg %", "Volume×", "RSI", "Pattern", "VWMA"]
        cw  = [8*mm, 22*mm, 18*mm, 14*mm, 16*mm, 12*mm, 40*mm, 20*mm]
        rows = [hdr]
        for i, g in enumerate(gainers, 1):
            sym  = g.get("symbol", "")
            tech = tech_map.get(sym, {})
            rows.append([
                str(i),
                sym,
                f"₹{float(g.get('lastPrice') or g.get('ltp') or g.get('close') or 0):,.2f}",
                f"+{float(g.get('pchange', 0)):.2f}%",
                f"{tech.get('vol_ratio', 0):.1f}x",
                f"{tech.get('rsi', 0):.0f}",
                ", ".join(tech.get("patterns", [])[:2]) or "—",
                "▲ Above" if tech.get("above_vwma") else "▼ Below",
            ])
        t, ts = _tbl(rows, cw, hdr_color=hdr_color)
        for i, g in enumerate(gainers, 1):
            sym  = g.get("symbol", "")
            tech = tech_map.get(sym, {})
            # % change color
            ts.add("TEXTCOLOR", (3, i), (3, i), GREEN)
            ts.add("FONTNAME",  (3, i), (3, i), "Helvetica-Bold")
            # RSI color
            rsi = tech.get("rsi", 50)
            rc  = RED if rsi > 70 else (GREEN if rsi < 40 else WHITE)
            ts.add("TEXTCOLOR", (5, i), (5, i), rc)
            # VWMA color
            vc = GREEN if tech.get("above_vwma") else YELLOW
            ts.add("TEXTCOLOR", (7, i), (7, i), vc)
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 3*mm))

        # Deep-dive per stock
        story.append(_p("Why These Stocks Gained — AI + Technical Deep Dive",
                        size=9, bold=True, color=WHITE))
        story.append(Spacer(1, 1*mm))
        for g in gainers:
            sym   = g.get("symbol", "")
            tech  = tech_map.get(sym, {})
            ai_ex = ai_map.get(sym, "")
            pats  = ", ".join(tech.get("patterns", [])) or "No pattern"
            rsi   = tech.get("rsi", 50)
            vr    = tech.get("vol_ratio", 1.0)
            trend = tech.get("trend", "")
            news  = "; ".join(tech.get("news", [])[:2]) or "No announcements today"

            story.append(_p(
                f"<b>{sym}</b>  "
                f"<font color='#22c55e'>+{float(g.get('pchange',0)):.2f}%</font>  "
                f"| Pattern: <b>{pats}</b>  | RSI: <b>{rsi}</b>  "
                f"| Vol: <b>{vr:.1f}×</b> avg  | Trend: {trend}",
                size=8
            ))
            if news:
                story.append(_p(f"📢 News: {news}", size=7.5, color=BLUE))
            if ai_ex:
                story.append(_p(f"🤖 {ai_ex}", size=7.5, color=LGRAY))
            story.append(Spacer(1, 1.5*mm))

        story.append(Spacer(1, 3*mm))
        story.append(_hr())

    _gainers_section("NSE — Top 10 Gainers", nse_gainers,
                     colors.HexColor("#6366f1"))
    _gainers_section("Nifty 500 — Top 10 Gainers (excl. NSE Top 10)",
                     nifty500_gainers, colors.HexColor("#f59e0b"))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(_p(
        f"Generated {datetime.now(_IST).strftime('%d %b %Y %H:%M IST')}  |  RRE Trading Bot",
        size=7, color=LGRAY, align=TA_CENTER
    ))

    doc.build(story)
    return buf.getvalue()


# ── Main entry point ──────────────────────────────────────────────────────────

async def evening_scan_and_send() -> None:
    from services.nse_service import (get_nse_overall_top_gainers, get_nifty500_ohlc,
                                       get_fii_dii_data, get_stock_announcements)
    from services.telegram_service import send_document
    from services.technical_analysis import summarise

    now      = datetime.now(_IST)
    date_str = now.strftime("%d %b %Y")
    logger.info("Evening Analysis: starting for %s", date_str)

    # ── Fetch top gainers ─────────────────────────────────────────────────────
    nse_g, nifty500_all, fii_dii = await asyncio.gather(
        get_nse_overall_top_gainers(10),
        get_nifty500_ohlc(),
        get_fii_dii_data(),
    )

    nse_syms = {g["symbol"] for g in nse_g}
    nifty500_sorted = sorted(
        [s for s in nifty500_all if s.get("pchange") and s["symbol"] not in nse_syms],
        key=lambda x: float(x.get("pchange", 0)), reverse=True
    )[:10]

    all_gainers = nse_g + nifty500_sorted
    all_syms    = [g["symbol"] for g in all_gainers]

    # ── Fetch OHLCV + announcements in parallel ────────────────────────────────
    ohlcv_map, *ann_results = await asyncio.gather(
        _fetch_ohlcv(all_syms, days=30),
        *[get_stock_announcements(s) for s in all_syms],
    )
    ann_map = {sym: ann for sym, ann in zip(all_syms, ann_results)}

    # ── Technical analysis ────────────────────────────────────────────────────
    tech_map: dict[str, dict] = {}
    for sym in all_syms:
        ohlcv = ohlcv_map.get(sym, [])
        if ohlcv:
            ta = summarise(ohlcv)
        else:
            ta = {"patterns": [], "rsi": 50, "vwma": 0, "vol_ratio": 1.0,
                  "above_vwma": False, "trend": "Unknown", "rsi_label": "Neutral"}
        ta["news"] = ann_map.get(sym, [])
        tech_map[sym] = ta

    # ── AI explanation per stock ───────────────────────────────────────────────
    ai_input = []
    for g in all_gainers:
        sym = g["symbol"]
        ai_input.append({
            "symbol":     sym,
            "pchange":    float(g.get("pchange", 0)),
            "news":       tech_map[sym].get("news", []),
            **{k: tech_map[sym].get(k) for k in
               ("patterns","rsi","rsi_label","vol_ratio","above_vwma","trend")},
        })
    ai_map = await asyncio.to_thread(_generate_stock_ai, ai_input)

    # ── Build PDF ─────────────────────────────────────────────────────────────
    pdf_bytes = await asyncio.to_thread(
        _build_evening_pdf,
        date_str, nse_g, nifty500_sorted, fii_dii, tech_map, ai_map
    )

    # ── Send to Telegram ──────────────────────────────────────────────────────
    top5 = ", ".join(g["symbol"] for g in nse_g[:5])
    caption = (
        f"📈 Evening Analysis — {date_str}\n"
        f"NSE Top Gainers: {top5}\n"
        f"Technical + AI reasons + FII/DII inside"
    )
    filename = f"RRE_Evening_{now.strftime('%Y%m%d')}.pdf"
    await send_document(pdf_bytes, filename, caption)

    try:
        from services.supabase_service import log_report_sent
        log_report_sent("evening", f"Top gainers: {top5}", {"count": len(all_gainers)})
    except Exception:
        pass

    logger.info("Evening Analysis sent — %d stocks analysed", len(all_gainers))


async def evening_report_loop() -> None:
    """Fires every weekday at 8:00 PM IST."""
    logger.info("Evening report loop started")
    await asyncio.sleep(60)
    fired_date: str | None = None
    while True:
        now = datetime.now(_IST)
        if now.weekday() < 5 and now.hour == 20 and now.minute < 5:
            today = now.strftime("%Y-%m-%d")
            if fired_date != today:
                fired_date = today
                logger.info("Evening report: firing for %s", today)
                try:
                    await evening_scan_and_send()
                except Exception as exc:
                    logger.error("Evening report failed: %s", exc)
        await asyncio.sleep(60)
