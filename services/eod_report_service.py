"""
eod_report_service.py
4:00 PM End-of-Day Report — fires Mon–Fri after market close.

Sections:
  1. Day Summary — Nifty 50 / Bank Nifty / Midcap / Smallcap
  2. Top Gainers & Losers — Nifty 500
  3. Sector Performance
  4. Final F&O OI Analysis — Long Buildup / Short Buildup / Unwinding
  5. Delivery % Leaders — high-conviction moves
  6. Tomorrow's Watchlist — best multi-signal setups
  7. AI EOD Analysis + Tomorrow's Strategy
"""
import asyncio
import io
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _fmt_vol(n) -> str:
    n = int(n or 0)
    if n >= 10_000_000: return f"{n/10_000_000:.1f}Cr"
    if n >= 100_000:    return f"{n/100_000:.1f}L"
    if n >= 1_000:      return f"{n/1_000:.0f}K"
    return str(n) if n else "—"


def _pct_arrow(v: float) -> str:
    return f"▲{v:+.2f}%" if v >= 0 else f"▼{v:.2f}%"


# ── Screeners ─────────────────────────────────────────────────────────────────

def get_top_gainers_losers(stocks: list[dict], n: int = 15) -> tuple[list, list]:
    valid = [s for s in stocks if s.get("pchange") is not None]
    sorted_s = sorted(valid, key=lambda x: -_safe_float(x.get("pchange", 0)))
    return sorted_s[:n], sorted_s[-n:][::-1]


def get_high_delivery_stocks(stocks: list[dict], n: int = 15) -> list[dict]:
    """Stocks with delivery % > 60% — conviction moves."""
    results = []
    for s in stocks:
        dv = _safe_float(s.get("delivery_pct", s.get("deliveryToTradedQuantity", 0)))
        if dv >= 60 and _safe_float(s.get("pchange", 0)) > 0.5:
            results.append({**s, "delivery_pct": round(dv, 1)})
    results.sort(key=lambda x: (-x["delivery_pct"], -_safe_float(x.get("pchange", 0))))
    return results[:n]


def get_eod_watchlist(
    stocks: list[dict],
    long_buildup: list[dict],
    runners: list[dict],
) -> list[dict]:
    """
    Multi-signal watchlist for tomorrow:
    - In F&O Long Buildup OR top runner candidates
    - Price change > 0 today (momentum)
    - Close in top 30% of day's range
    """
    lb_syms  = {r.get("symbol", "") for r in long_buildup}
    run_syms = {r["symbol"]: r for r in runners if r.get("grade") in ("HIGH", "STRONG")}

    results = []
    seen: set[str] = set()
    for s in stocks:
        sym = s.get("symbol", "")
        if sym in seen:
            continue
        pch = _safe_float(s.get("pchange", 0))
        if pch <= 0:
            continue
        h, l, c = s.get("high", 0), s.get("low", 0), s.get("close", 0)
        hl = h - l
        close_pos = (c - l) / hl if hl > 0.01 else 0
        if close_pos < 0.70:
            continue

        in_lb     = sym in lb_syms
        runner    = run_syms.get(sym)
        score     = 0
        signals   = []

        if in_lb:
            score += 3
            signals.append("FO LB")
        if runner:
            grade_pts = {"HIGH": 4, "STRONG": 3}.get(runner.get("grade",""), 1)
            score += grade_pts
            signals.append(f"{runner['grade']}")
        if pch >= 2:
            score += 1
            signals.append(f"+{pch:.1f}%")
        if close_pos >= 0.85:
            score += 1
            signals.append("Near HOD")

        if score >= 3:
            seen.add(sym)
            results.append({
                "symbol":    sym,
                "close":     round(c, 2),
                "pchange":   round(pch, 2),
                "close_pos": round(close_pos * 100, 1),
                "score":     score,
                "signals":   " · ".join(signals),
                "grade":     runner.get("grade", "WATCH") if runner else "WATCH",
            })

    results.sort(key=lambda x: -x["score"])
    return results[:20]


# ── AI EOD Analysis ───────────────────────────────────────────────────────────

def _generate_eod_ai(
    gainers:      list[dict],
    losers:       list[dict],
    long_buildup: list[dict],
    runners:      list[dict],
    watchlist:    list[dict],
    sector_data:  dict,
) -> str:
    try:
        from services.openai_service import _get_client

        g_syms  = [s.get("symbol","") for s in gainers[:8]]
        l_syms  = [s.get("symbol","") for s in losers[:8]]
        lb_syms = [r.get("symbol","") for r in long_buildup[:10]]
        hi_run  = [r["symbol"] for r in runners if r.get("grade") == "HIGH"][:5]
        wl_syms = [w["symbol"] for w in watchlist[:8]]

        sector_lines = []
        for sec, val in list((sector_data or {}).items())[:8]:
            sector_lines.append(f"  {sec}: {val:+.2f}%")
        sector_str = "\n".join(sector_lines) or "  (unavailable)"

        prompt = (
            "You are a professional Indian stock market EOD analyst.\n"
            f"It is 4:00 PM IST — market has closed for {datetime.now().strftime('%A, %d %b %Y')}.\n\n"
            f"TOP GAINERS (Nifty 500): {', '.join(g_syms)}\n"
            f"TOP LOSERS  (Nifty 500): {', '.join(l_syms)}\n"
            f"F&O Long Buildup (final): {', '.join(lb_syms) or 'None'}\n"
            f"Tomorrow's HIGH-grade runner candidates: {', '.join(hi_run) or 'None'}\n"
            f"Multi-signal tomorrow watchlist: {', '.join(wl_syms) or 'None'}\n"
            f"Sector Performance:\n{sector_str}\n\n"
            "Write a concise EOD analysis in plain text (no markdown):\n\n"
            "EOD MARKET ASSESSMENT (3 sentences): How was today? Breadth, trend, key observations.\n\n"
            "SECTOR ROTATION INSIGHT: Which sectors showed strength/weakness and what it signals for tomorrow.\n\n"
            "TOP 5 PICKS FOR TOMORROW SESSION:\n"
            "For each: SYMBOL — reason (Long Buildup/Runner/Breakout close) — specific entry trigger for tomorrow morning.\n\n"
            "TOMORROW'S STRATEGY: One paragraph — gap-up/gap-down scenario planning, time-of-day entry approach.\n\n"
            "RED FLAGS TO WATCH: 2 specific risks or negative signals from today's data.\n\n"
            "Keep total under 450 words. Be specific and actionable for positional trading."
        )

        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
            temperature=0.35,
        )
        return resp.choices[0].message.content.strip()

    except Exception as exc:
        logger.warning("EOD AI analysis failed: %s", exc)
        return _fallback_eod_ai(gainers, losers, watchlist)


def _fallback_eod_ai(gainers, losers, watchlist) -> str:
    wl_top = [w["symbol"] for w in watchlist[:5]]
    g_top  = [s.get("symbol","") for s in gainers[:5]]
    lines  = [
        "AI analysis unavailable — rule-based EOD summary:",
        "",
        f"Top gainers: {', '.join(g_top) or 'None'}",
        f"Tomorrow watchlist (multi-signal): {', '.join(wl_top) or 'None'}",
        "",
        "Entry Rule: Buy above previous day high with volume confirmation.",
        "Stop Loss: Below previous day low or breakout candle low.",
    ]
    return "\n".join(lines)


# ── PDF Builder ───────────────────────────────────────────────────────────────

def _build_eod_pdf(
    date_str:     str,
    gainers:      list[dict],
    losers:       list[dict],
    long_buildup: list[dict],
    runners:      list[dict],
    watchlist:    list[dict],
    delivery:     list[dict],
    sector_data:  dict,
    ai_text:      str,
) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer,
        Table, TableStyle, HRFlowable,
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY

    DARK_BLUE = colors.HexColor("#1e3a5f")
    GREEN     = colors.HexColor("#059669")
    RED       = colors.HexColor("#dc2626")
    AMBER     = colors.HexColor("#d97706")
    PURPLE    = colors.HexColor("#7c3aed")
    TEAL      = colors.HexColor("#0e7490")
    COBALT    = colors.HexColor("#1d4ed8")
    ORANGE    = colors.HexColor("#ea580c")
    GRAY      = colors.HexColor("#6b7280")
    BORDER    = colors.HexColor("#e5e7eb")
    ALT_ROW   = colors.HexColor("#f9fafb")
    GOLD      = colors.HexColor("#d97706")

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

    # ── Cover ─────────────────────────────────────────────────────────────────
    story.append(Paragraph(
        "RRE End-of-Day Report  4:00 PM",
        sty("T", size=15, color=DARK_BLUE, align=TA_CENTER, bold=True, after=3),
    ))
    story.append(Paragraph(
        f"Nifty 500 Universe  |  {date_str}  |  EOD Analysis & Tomorrow's Strategy",
        sty("S", size=8, color=GRAY, align=TA_CENTER, after=2),
    ))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=DARK_BLUE))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        f"Top Gainers: <b>{len(gainers)}</b>  |  "
        f"Top Losers: <b>{len(losers)}</b>  |  "
        f"F&O Long Buildup: <b>{len(long_buildup)}</b>  |  "
        f"Tomorrow Watchlist: <b>{len(watchlist)}</b>  |  "
        f"Runner Candidates: <b>{len(runners)}</b>",
        sty("SUM", size=8, color=DARK_BLUE, align=TA_CENTER, after=2),
    ))
    story.append(Spacer(1, 5*mm))

    def _section_header(title, subtitle, color):
        story.append(HRFlowable(width="100%", thickness=1.2, color=color))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(title, sty("SH", size=11, color=color, bold=True,
                                          align=TA_CENTER, before=2, after=2)))
        story.append(Paragraph(subtitle, sty("SD", size=7, color=GRAY, italic=True,
                                             align=TA_CENTER, before=0, after=4)))

    def _simple_table(data_rows, col_widths, hdr_color):
        """Returns (Table, TableStyle) so extra rules can be added before setStyle."""
        t = Table(data_rows, colWidths=col_widths)
        ts = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), hdr_color),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (0, 1), (0, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        for i in range(1, len(data_rows)):
            ts.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
        return t, ts

    # ── 1. Sector Performance ─────────────────────────────────────────────────
    if sector_data:
        _section_header(
            "Sector Performance -- End of Day",
            "Index performance vs previous close",
            COBALT,
        )
        sec_items = sorted(sector_data.items(), key=lambda x: -x[1])
        sec_rows = [["Sector / Index", "Change %", "Signal"]]
        for sec, pct in sec_items[:14]:
            sig = "Bullish" if pct >= 1 else "Bearish" if pct <= -1 else "Flat"
            sec_rows.append([sec, f"{pct:+.2f}%", sig])
        t_sec = Table(sec_rows, colWidths=[90*mm, 35*mm, 35*mm])
        ts_sec = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), COBALT),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (0, 1), (0, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        for i, (_, pct) in enumerate(sec_items[:14], 1):
            ts_sec.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            clr = GREEN if pct >= 0 else RED
            ts_sec.add("TEXTCOLOR", (1, i), (2, i), clr)
            ts_sec.add("FONTNAME",  (1, i), (2, i), "Helvetica-Bold")
        t_sec.setStyle(ts_sec)
        story.append(t_sec)
        story.append(Spacer(1, 6*mm))

    # ── 2. Top Gainers ─────────────────────────────────────────────────────────
    if gainers:
        _section_header(
            f"Top Gainers -- Nifty 500  ({len(gainers)} stocks)",
            "Sorted by price change % -- EOD",
            GREEN,
        )
        g_rows = [["Symbol", "Close Rs", "Chg %", "Open", "High", "Volume"]]
        for s in gainers:
            g_rows.append([
                s.get("symbol", ""),
                f"{_safe_float(s.get('close', 0)):,.2f}",
                f"{_safe_float(s.get('pchange', 0)):+.2f}%",
                f"{_safe_float(s.get('open', 0)):,.2f}",
                f"{_safe_float(s.get('high', 0)):,.2f}",
                _fmt_vol(s.get("volume", 0)),
            ])
        cw = [38*mm, 28*mm, 22*mm, 22*mm, 22*mm, 20*mm]
        t_g, ts_g = _simple_table(g_rows, cw, GREEN)
        for i in range(1, len(g_rows)):
            ts_g.add("TEXTCOLOR", (2, i), (2, i), GREEN)
            ts_g.add("FONTNAME",  (2, i), (2, i), "Helvetica-Bold")
        t_g.setStyle(ts_g)
        story.append(t_g)
        story.append(Spacer(1, 6*mm))

    # ── 3. Top Losers ─────────────────────────────────────────────────────────
    if losers:
        _section_header(
            "Top Losers -- Nifty 500  (" + str(len(losers)) + " stocks)",
            "Sorted by price change % -- EOD",
            RED,
        )
        l_rows = [["Symbol", "Close Rs", "Chg %", "Open", "Low", "Volume"]]
        for s in losers:
            l_rows.append([
                s.get("symbol", ""),
                f"{_safe_float(s.get('close', 0)):,.2f}",
                f"{_safe_float(s.get('pchange', 0)):+.2f}%",
                f"{_safe_float(s.get('open', 0)):,.2f}",
                f"{_safe_float(s.get('low', 0)):,.2f}",
                _fmt_vol(s.get("volume", 0)),
            ])
        cw = [38*mm, 28*mm, 22*mm, 22*mm, 22*mm, 20*mm]
        t_l, ts_l = _simple_table(l_rows, cw, RED)
        for i in range(1, len(l_rows)):
            ts_l.add("TEXTCOLOR", (2, i), (2, i), RED)
            ts_l.add("FONTNAME",  (2, i), (2, i), "Helvetica-Bold")
        t_l.setStyle(ts_l)
        story.append(t_l)
        story.append(Spacer(1, 6*mm))

    # ── 4. F&O Long Buildup ───────────────────────────────────────────────────
    if long_buildup:
        SOURCE_CLR = {"NSE": GREEN, "TSR Pro": COBALT, "NSE·TSR": AMBER}
        nse_count = sum(1 for s in long_buildup if "NSE" in s.get("source", ""))
        tsr_count = sum(1 for s in long_buildup if "TSR" in s.get("source", ""))
        _section_header(
            f"F&O Long Buildup -- Final EOD  ({len(long_buildup)} stocks  NSE:{nse_count} TSR:{tsr_count})",
            "OI up + Price up -- fresh institutional longs entering at EOD",
            GREEN,
        )
        lb_rows = [["Symbol", "LTP Rs", "Chg %", "OI Chg %", "Source"]]
        for r in long_buildup[:20]:
            lb_rows.append([
                r.get("symbol", ""),
                f"{_safe_float(r.get('lastPrice', r.get('ltp', 0))):,.2f}",
                f"{_safe_float(r.get('pChange', r.get('pchange', 0))):+.2f}%",
                f"{_safe_float(r.get('oi_chg_pct', 0)):+.2f}%",
                r.get("source", "NSE"),
            ])
        cw = [45*mm, 32*mm, 26*mm, 26*mm, 22*mm]
        t_lb, ts_lb = _simple_table(lb_rows, cw, GREEN)
        for i, r in enumerate(long_buildup[:20], 1):
            pch = _safe_float(r.get("pChange", r.get("pchange", 0)))
            ts_lb.add("TEXTCOLOR", (2, i), (2, i), GREEN if pch >= 0 else RED)
            src = r.get("source", "NSE")
            ts_lb.add("TEXTCOLOR", (4, i), (4, i), SOURCE_CLR.get(src, GREEN))
            ts_lb.add("FONTNAME",  (4, i), (4, i), "Helvetica-Bold")
        t_lb.setStyle(ts_lb)
        story.append(t_lb)
        story.append(Spacer(1, 6*mm))

    # ── 5. High Delivery % Stocks ─────────────────────────────────────────────
    if delivery:
        _section_header(
            f"High Delivery % Stocks  ({len(delivery)} stocks)",
            "Delivery % > 60% with positive close -- strong conviction buying",
            ORANGE,
        )
        d_rows = [["Symbol", "Close Rs", "Chg %", "Delivery %", "Volume"]]
        for s in delivery:
            d_rows.append([
                s.get("symbol", ""),
                f"{_safe_float(s.get('close', 0)):,.2f}",
                f"{_safe_float(s.get('pchange', 0)):+.2f}%",
                f"{s.get('delivery_pct', 0):.1f}%",
                _fmt_vol(s.get("volume", 0)),
            ])
        cw = [45*mm, 32*mm, 26*mm, 26*mm, 22*mm]
        t_d, ts_d = _simple_table(d_rows, cw, ORANGE)
        for i in range(1, len(d_rows)):
            ts_d.add("TEXTCOLOR", (2, i), (2, i), GREEN)
            ts_d.add("TEXTCOLOR", (3, i), (3, i), ORANGE)
            ts_d.add("FONTNAME",  (3, i), (3, i), "Helvetica-Bold")
        t_d.setStyle(ts_d)
        story.append(t_d)
        story.append(Spacer(1, 6*mm))

    # ── 6. Tomorrow's Watchlist ────────────────────────────────────────────────
    if watchlist:
        GRADE_CLR = {"HIGH": GREEN, "STRONG": COBALT, "WATCH": AMBER}
        _section_header(
            f"Tomorrow's Multi-Signal Watchlist  ({len(watchlist)} stocks)",
            "F&O Long Buildup + Runner Grade + Close near HOD + Momentum -- multi-factor",
            GOLD,
        )
        w_rows = [["#", "Symbol", "Score", "Grade", "Close Rs", "Chg %", "Near HOD", "Signals"]]
        cw = [8*mm, 28*mm, 14*mm, 22*mm, 24*mm, 18*mm, 18*mm, 0]
        cw[-1] = 160*mm - sum(cw[:-1])
        for rank, w in enumerate(watchlist, 1):
            w_rows.append([
                str(rank), w["symbol"], str(w["score"]),
                w.get("grade", "WATCH"),
                f"{w['close']:,.2f}",
                f"{w['pchange']:+.2f}%",
                f"{w['close_pos']}%",
                w.get("signals", ""),
            ])
        t_w = Table(w_rows, colWidths=cw)
        ts_w = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), GOLD),
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
        ])
        for i, w in enumerate(watchlist, 1):
            ts_w.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            ts_w.add("TEXTCOLOR", (2, i), (3, i), GRADE_CLR.get(w.get("grade","WATCH"), GRAY))
            ts_w.add("FONTNAME",  (2, i), (3, i), "Helvetica-Bold")
            pch_clr = GREEN if w["pchange"] >= 0 else RED
            ts_w.add("TEXTCOLOR", (5, i), (5, i), pch_clr)
        t_w.setStyle(ts_w)
        story.append(t_w)
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(
            "Entry Rule: Buy above today's high with volume > 1.5x avg  |  "
            "Stop: below today's low  |  Hold: 1-5 days positional",
            sty("RULE", size=7, color=GOLD, align=TA_CENTER, italic=True),
        ))
        story.append(Spacer(1, 6*mm))

    # ── 7. AI EOD Analysis ────────────────────────────────────────────────────
    if ai_text:
        _section_header(
            "AI End-of-Day Analysis & Tomorrow's Strategy",
            "GPT-4o  |  EOD signal synthesis  |  Positional outlook for next session",
            COBALT,
        )
        for para in ai_text.strip().split("\n\n"):
            para = para.strip()
            if not para:
                continue
            if any(para.upper().startswith(kw) for kw in (
                "EOD MARKET", "SECTOR ROTATION", "TOP 5", "TOMORROW",
                "RED FLAGS", "ENTRY STRATEGY", "RISK",
            )):
                story.append(Paragraph(
                    para, sty("AI_H", size=9, color=COBALT, bold=True, before=4, after=2),
                ))
            else:
                story.append(Paragraph(
                    para, sty("AI_P", size=8.5, color=DARK_BLUE, before=2, after=4, align=TA_JUSTIFY),
                ))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
    story.append(Paragraph(
        f"Generated by RRE Market Scanner  |  "
        f"{datetime.now().strftime('%d %b %Y %H:%M IST')}  |  "
        "Data: NSE India | TSR Pro | Yahoo Finance | OpenAI",
        sty("F", size=6.5, color=GRAY, align=TA_CENTER, before=6),
    ))
    doc.build(story)
    return buf.getvalue()


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def eod_scan_and_send() -> None:
    """Run all 4:00 PM EOD scans, build PDF, send via Telegram."""
    from services.nse_service import (
        get_nifty500_ohlc, get_fno_oi_buildup,
        get_sector_rotation_multi,
    )
    from services.tsr_service import get_tsr_buildup
    from services.telegram_service import send_document
    from services.tomorrow_scanner_service import scan_tomorrow_runners
    from services.pm_report_service import _normalize_tsr_buildup

    logger.info("EOD Report: starting 4:00 PM scan")
    date_str = datetime.now().strftime("%d %b %Y")

    try:
        stocks, buildup_raw, tsr_bu, runners = await asyncio.gather(
            get_nifty500_ohlc(),
            get_fno_oi_buildup(15),
            get_tsr_buildup(),
            scan_tomorrow_runners(),
        )

        stocks   = stocks or []
        runners  = runners or []

        # Merge NSE + TSR long buildup
        nse_lb = (buildup_raw or {}).get("Long Buildup", [])
        for s in nse_lb:
            s["source"] = "NSE"
        tsr_lb   = _normalize_tsr_buildup(tsr_bu or {})
        nse_syms = {s.get("symbol", "") for s in nse_lb}
        for s in tsr_lb:
            if s["symbol"] in nse_syms:
                for n in nse_lb:
                    if n.get("symbol") == s["symbol"]:
                        n["source"] = "NSE·TSR"
                        break
            else:
                nse_lb.append(s)
        long_buildup = nse_lb

        # Sector data
        sector_data = {}
        try:
            sec_raw = await get_sector_rotation_multi()
            for item in (sec_raw or []):
                name = item.get("index", item.get("name", ""))
                pct  = _safe_float(item.get("pChange", item.get("pct", 0)))
                if name:
                    sector_data[name] = pct
        except Exception as e:
            logger.warning("Sector data failed: %s", e)

        gainers, losers = get_top_gainers_losers(stocks, 15)
        delivery        = get_high_delivery_stocks(stocks, 12)
        watchlist       = get_eod_watchlist(stocks, long_buildup, runners)

        ai_text = await asyncio.to_thread(
            _generate_eod_ai, gainers, losers, long_buildup, runners, watchlist, sector_data
        )

        pdf_bytes = await asyncio.to_thread(
            _build_eod_pdf,
            date_str, gainers, losers, long_buildup, runners,
            watchlist, delivery, sector_data, ai_text,
        )

        # Telegram caption
        top_g  = [s.get("symbol","") for s in gainers[:5]]
        top_l  = [s.get("symbol","") for s in losers[:5]]
        hi_run = [r["symbol"] for r in runners if r.get("grade") == "HIGH"][:4]
        wl_top = [w["symbol"] for w in watchlist[:5]]

        caption_lines = [
            f"<b>RRE 4:00 PM End-of-Day Report -- {date_str}</b>",
            "",
            f"TOP GAINERS ({len(gainers)}): {' . '.join(top_g)}",
            f"TOP LOSERS  ({len(losers)}): {' . '.join(top_l)}",
            f"F&O Long Buildup: {len(long_buildup)} stocks",
            f"Tomorrow HIGH Runners: {' . '.join(hi_run) or 'None'}",
            f"Tomorrow Watchlist: {' . '.join(wl_top) or 'None'}",
            "",
            "AI EOD analysis + Tomorrow strategy included in PDF",
        ]

        filename = f"RRE_EOD_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        await send_document(pdf_bytes, filename, "\n".join(caption_lines))
        logger.info(
            "EOD Report sent — %d gainers, %d losers, %d LB, %d watchlist, %d runners",
            len(gainers), len(losers), len(long_buildup), len(watchlist), len(runners),
        )

        return {
            "pdf_bytes": pdf_bytes,
            "gainers":   len(gainers),
            "losers":    len(losers),
            "buildup":   len(long_buildup),
            "watchlist": len(watchlist),
            "runners":   len(runners),
        }

    except Exception as exc:
        logger.warning("EOD Report error: %s", exc, exc_info=True)
        try:
            from services.telegram_service import send_message
            await send_message(f"EOD 4 PM Report failed: {exc}")
        except Exception:
            pass
        return {}


# ── Background Loop ───────────────────────────────────────────────────────────

async def eod_report_loop() -> None:
    """Background loop: fires once at 16:00 IST on every market day (Mon-Fri)."""
    logger.info("EOD report loop started")
    fired_today: str = ""
    await asyncio.sleep(60)

    while True:
        now = datetime.now()
        today_key = now.strftime("%Y-%m-%d")
        if (now.weekday() < 5
                and now.hour == 16
                and now.minute < 10
                and fired_today != today_key):
            fired_today = today_key
            await eod_scan_and_send()
        await asyncio.sleep(60)
