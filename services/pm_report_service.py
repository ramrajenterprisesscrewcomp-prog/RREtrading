"""
pm_report_service.py
2:45 PM Positional Report — fires Mon–Fri on every market day.
Sections:
  1. F&O Long Buildup (fresh institutional longs)
  2. Consolidation / Intraday Breakouts
  3. Pullback Bounce Setups
  4. Tomorrow's Runner Candidates
  5. AI Analysis & Top Trend-Continuation Picks
"""
import asyncio
import io
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

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


# ── Stock Screeners (run on Nifty 500 OHLC, no extra API calls) ──────────────

def scan_consolidation_breakouts(stocks: list[dict]) -> list[dict]:
    """
    Intraday breakout setup:
    - Strong bullish candle (body ≥ 50% of H-L range, close in top 30%)
    - Price change > 1.5%
    - Stock in uptrend territory (≥ 55% of 52W high)
    """
    results = []
    for s in stocks:
        try:
            o, h, l, c = s["open"], s["high"], s["low"], s["close"]
            pch = _safe_float(s.get("pchange", 0))
            hl  = h - l
            if hl < 0.01 or pch < 1.5:
                continue
            body         = c - o
            if body <= 0:
                continue
            body_pct     = body / hl
            close_to_top = (h - c) / hl          # 0 = at high, 1 = at low
            if body_pct < 0.50 or close_to_top > 0.30:
                continue
            yh = _safe_float(s.get("year_high", 0))
            if yh > 0 and c < yh * 0.55:
                continue
            results.append({
                "symbol":    s["symbol"],
                "close":     round(c, 2),
                "open":      round(o, 2),
                "high":      round(h, 2),
                "low":       round(l, 2),
                "pchange":   round(pch, 2),
                "volume":    s.get("volume", 0),
                "year_high": round(yh, 2) if yh else 0,
                "body_pct":  round(body_pct * 100, 1),
            })
        except Exception:
            continue
    results.sort(key=lambda x: -x["pchange"])
    return results[:20]


def scan_pullback_stocks(stocks: list[dict]) -> list[dict]:
    """
    Pullback bounce setup:
    - Down day (-8% to -0.5%) — healthy correction
    - Intraday recovery: close in top 45%+ of session range (dip-buying)
    - Stock still in uptrend territory (≥ 65% of 52W high)
    """
    results = []
    for s in stocks:
        try:
            o, h, l, c = s["open"], s["high"], s["low"], s["close"]
            pch = _safe_float(s.get("pchange", 0))
            hl  = h - l
            if pch < -8 or pch > -0.5:
                continue
            if hl < 0.01:
                continue
            close_pos = (c - l) / hl        # how far from low (0=at low, 1=at high)
            if close_pos < 0.45:
                continue
            yh = _safe_float(s.get("year_high", 0))
            if yh > 0 and c < yh * 0.65:
                continue
            results.append({
                "symbol":    s["symbol"],
                "close":     round(c, 2),
                "pchange":   round(pch, 2),
                "high":      round(h, 2),
                "low":       round(l, 2),
                "volume":    s.get("volume", 0),
                "year_high": round(yh, 2) if yh else 0,
                "close_pos": round(close_pos * 100, 1),
                "from_yh":   round((yh - c) / yh * 100, 1) if yh else 0,
            })
        except Exception:
            continue
    results.sort(key=lambda x: (-x["close_pos"], x["pchange"]))
    return results[:20]


# ── AI Analysis ───────────────────────────────────────────────────────────────

def _generate_ai_picks(
    long_buildup: list[dict],
    breakouts:    list[dict],
    pullbacks:    list[dict],
    runners:      list[dict],
) -> str:
    try:
        from services.openai_service import _get_client

        lb_syms  = [r.get("symbol", "") for r in long_buildup[:10]]
        bo_syms  = [r["symbol"] for r in breakouts[:10]]
        pb_syms  = [r["symbol"] for r in pullbacks[:10]]
        run_high = [r["symbol"] for r in runners if r.get("grade") == "HIGH"][:5]
        run_str  = [r["symbol"] for r in runners if r.get("grade") == "STRONG"][:5]

        # Find multi-list overlaps to highlight to AI
        all_sym_lists = [set(lb_syms), set(bo_syms), set(pb_syms),
                         set(run_high + run_str)]
        all_syms = set().union(*all_sym_lists)
        overlaps = sorted(
            [(s, sum(1 for ss in all_sym_lists if s in ss)) for s in all_syms],
            key=lambda x: -x[1],
        )
        overlap_str = ", ".join(f"{s}({c}x)" for s, c in overlaps[:8] if c >= 2)

        prompt = (
            "You are a professional Indian stock market positional trader and analyst.\n"
            "It is 2:45 PM IST. Market closes in 15 minutes. Analyze the following 2:45 PM data:\n\n"
            f"F&O Long Buildup (institutional fresh longs): {', '.join(lb_syms) or 'None'}\n"
            f"Intraday Breakout Stocks (strong bullish candle): {', '.join(bo_syms) or 'None'}\n"
            f"Pullback Bounce Setups (down day, recovered intraday): {', '.join(pb_syms) or 'None'}\n"
            f"Tomorrow's HIGH-grade runners: {', '.join(run_high) or 'None'}\n"
            f"Tomorrow's STRONG-grade runners: {', '.join(run_str) or 'None'}\n"
            f"Multi-list confirmed stocks (appearing in 2+ lists): {overlap_str or 'None'}\n\n"
            "Provide a concise analysis in plain text (no markdown):\n\n"
            "MARKET TREND (2-3 sentences): Overall market momentum today and what it implies for tomorrow.\n\n"
            "TOP 5 PICKS FOR TREND CONTINUATION:\n"
            "For each, write: SYMBOL — setup type (Long Buildup/Breakout/Pullback/Runner) — specific entry strategy and target rationale.\n"
            "Prioritize stocks appearing in 2+ signal lists as they have the strongest confirmation.\n\n"
            "ENTRY STRATEGY: One paragraph on how to enter these stocks tomorrow morning.\n\n"
            "RISK FACTORS: 2 specific risks that could invalidate these setups.\n\n"
            "Keep total response under 400 words. Be specific and actionable."
        )

        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=650,
            temperature=0.35,
        )
        return resp.choices[0].message.content.strip()

    except Exception as exc:
        logger.warning("PM Report AI picks failed: %s", exc)
        return _fallback_picks(long_buildup, breakouts, pullbacks, runners)


def _fallback_picks(lb, bo, pb, runners) -> str:
    lb_set  = {r.get("symbol", "") for r in lb}
    bo_set  = {r["symbol"] for r in bo}
    pb_set  = {r["symbol"] for r in pb}
    run_set = {r["symbol"] for r in runners if r.get("grade") in ("HIGH", "STRONG")}

    all_syms = lb_set | bo_set | pb_set | run_set
    multi = []
    for sym in all_syms:
        tags = []
        if sym in lb_set:  tags.append("Long Buildup")
        if sym in bo_set:  tags.append("Breakout")
        if sym in pb_set:  tags.append("Pullback")
        if sym in run_set: tags.append("Runner")
        if len(tags) >= 2:
            multi.append((sym, tags))
    multi.sort(key=lambda x: -len(x[1]))

    lines = [
        "AI analysis unavailable. Rule-based multi-list confirmation picks:",
        "",
    ]
    if multi:
        lines.append("Multi-confirmed Picks (appear in 2+ signal lists):")
        for sym, tags in multi[:5]:
            lines.append(f"  {sym} — {' + '.join(tags)}")
    else:
        lines.append("No stocks found in multiple signal lists today.")
        top5 = list(run_set)[:5] or list(bo_set)[:5]
        if top5:
            lines.append(f"Top single-list picks: {', '.join(top5)}")

    lines.extend([
        "",
        "Entry Strategy: Buy above previous day's high with at least 1.5× average volume confirmation.",
        "Stop Loss: Below the breakout candle's low or the previous day's low, whichever is closer.",
    ])
    return "\n".join(lines)


# ── PDF Builder ───────────────────────────────────────────────────────────────

def _build_pm_pdf(
    date_str:     str,
    long_buildup: list[dict],
    breakouts:    list[dict],
    pullbacks:    list[dict],
    runners:      list[dict],
    ai_text:      str,
    vwma_hits:    list[dict] | None = None,
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
    GRAY      = colors.HexColor("#6b7280")
    BORDER    = colors.HexColor("#e5e7eb")
    ALT_ROW   = colors.HexColor("#f9fafb")

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

    # ── Cover ────────────────────────────────────────────────────────────────
    story.append(Paragraph(
        "RRE Positional Report — 2:45 PM",
        sty("T", size=15, color=DARK_BLUE, align=TA_CENTER, bold=True, after=3),
    ))
    story.append(Paragraph(
        f"Nifty 500 Universe  ·  {date_str}  ·  F&O OI · Technical · AI Picks",
        sty("S", size=8, color=GRAY, align=TA_CENTER, after=2),
    ))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=DARK_BLUE))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        f"F&O Long Buildup: <b>{len(long_buildup)}</b>  ·  "
        f"Consolidation Breakouts: <b>{len(breakouts)}</b>  ·  "
        f"Pullback Setups: <b>{len(pullbacks)}</b>  ·  "
        f"Runner Candidates: <b>{len(runners)}</b>",
        sty("SUM", size=8, color=DARK_BLUE, align=TA_CENTER, after=2),
    ))
    story.append(Spacer(1, 5*mm))

    def _generic_table(rows_data, headers, col_widths, hdr_color, row_fn,
                       pch_col=None, extra_styles=None):
        rows = [headers] + [row_fn(r) for r in rows_data]
        t = Table(rows, colWidths=col_widths)
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
        for i, r in enumerate(rows_data, 1):
            ts.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            if pch_col is not None:
                val = _safe_float(r.get("pchange", r.get("pChange", 0)))
                ts.add("TEXTCOLOR", (pch_col, i), (pch_col, i), GREEN if val >= 0 else RED)
        if extra_styles:
            for cmd in extra_styles:
                ts.add(*cmd)
        t.setStyle(ts)
        return t

    def _section_header(title, subtitle, color):
        story.append(HRFlowable(width="100%", thickness=1.2, color=color))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(title, sty("SH", size=11, color=color, bold=True,
                                          align=TA_CENTER, before=2, after=2)))
        story.append(Paragraph(subtitle, sty("SD", size=7, color=GRAY, italic=True,
                                             align=TA_CENTER, before=0, after=4)))

    # ── 1. F&O Long Buildup (NSE FNO + TSR Pro merged) ───────────────────────
    if long_buildup:
        nse_count = sum(1 for s in long_buildup if "NSE" in s.get("source", ""))
        tsr_count = sum(1 for s in long_buildup if "TSR" in s.get("source", ""))
        _section_header(
            f"🟢 F&O Long Buildup  ({len(long_buildup)} stocks · NSE: {nse_count} · TSR Pro: {tsr_count})",
            "OI ↑ + Price ↑  ·  NSE = Live FNO data  ·  TSR Pro = Near-month futures screener  ·  NSE·TSR = Confirmed by both",
            GREEN,
        )

        SOURCE_CLR = {"NSE": GREEN, "TSR Pro": COBALT, "NSE·TSR": AMBER}

        lb_rows = long_buildup[:25]
        hdr_row = ["Symbol", "LTP ₹", "Chg %", "OI Chg %", "Signal", "Source"]
        data_rows = [hdr_row]
        for r in lb_rows:
            data_rows.append([
                r.get("symbol", ""),
                f"{_safe_float(r.get('lastPrice', r.get('ltp', 0))):,.2f}",
                f"{_safe_float(r.get('pChange', r.get('pchange', 0))):+.2f}%",
                f"{_safe_float(r.get('oi_chg_pct', 0)):+.2f}%",
                "Long Buildup 📈",
                r.get("source", "NSE"),
            ])

        col_w = [40*mm, 28*mm, 22*mm, 24*mm, 28*mm, 18*mm]
        t_lb = Table(data_rows, colWidths=col_w)
        ts_lb = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), GREEN),
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
        for i, r in enumerate(lb_rows, 1):
            ts_lb.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            # Chg % color
            pch_val = _safe_float(r.get("pChange", r.get("pchange", 0)))
            ts_lb.add("TEXTCOLOR", (2, i), (2, i), GREEN if pch_val >= 0 else RED)
            # Source column color — NSE=green, TSR Pro=cobalt, NSE·TSR=amber
            src = r.get("source", "NSE")
            src_clr = SOURCE_CLR.get(src, GREEN)
            ts_lb.add("TEXTCOLOR", (5, i), (5, i), src_clr)
            ts_lb.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
        t_lb.setStyle(ts_lb)
        story.append(t_lb)
        story.append(Spacer(1, 6*mm))

    # ── 2. Consolidation / Intraday Breakouts ─────────────────────────────────
    if breakouts:
        _section_header(
            f"⚡ Consolidation & Intraday Breakouts  ({len(breakouts)} stocks)",
            "Body ≥ 50% of day range  ·  Close in top 30%  ·  Price change > 1.5%  ·  Near 52W high",
            PURPLE,
        )
        story.append(_generic_table(
            breakouts,
            ["Symbol", "Close ₹", "Chg %", "Candle Body", "52W High", "Volume"],
            [42*mm, 28*mm, 22*mm, 24*mm, 28*mm, 16*mm],
            PURPLE,
            lambda r: [
                r["symbol"],
                f"{r['close']:,.2f}",
                f"{r['pchange']:+.2f}%",
                f"{r['body_pct']}%",
                f"{r['year_high']:,.2f}" if r["year_high"] else "—",
                _fmt_vol(r["volume"]),
            ],
            pch_col=2,
        ))
        story.append(Spacer(1, 6*mm))

    # ── 3. Pullback Bounce Setups ─────────────────────────────────────────────
    if pullbacks:
        _section_header(
            f"🔄 Pullback Bounce Setups  ({len(pullbacks)} stocks)",
            "Down day but recovered intraday  ·  Close in top 45%+ of session range  ·  Strong uptrend territory",
            TEAL,
        )
        story.append(_generic_table(
            pullbacks,
            ["Symbol", "Close ₹", "Chg %", "Recovery", "Off 52W High", "Volume"],
            [42*mm, 28*mm, 22*mm, 28*mm, 28*mm, 12*mm],
            TEAL,
            lambda r: [
                r["symbol"],
                f"{r['close']:,.2f}",
                f"{r['pchange']:+.2f}%",
                f"{r['close_pos']}% from low",
                f"-{r['from_yh']}%" if r["from_yh"] else "—",
                _fmt_vol(r["volume"]),
            ],
            pch_col=2,
        ))
        story.append(Spacer(1, 6*mm))

    # ── 4. Tomorrow's Runners ─────────────────────────────────────────────────
    if runners:
        GOLD = colors.HexColor("#d97706")
        GRADE_CLR = {"HIGH": GREEN, "STRONG": COBALT, "WATCH": AMBER, "MONITOR": GRAY}
        GRADE_LBL = {"HIGH": "⭐⭐⭐ HIGH", "STRONG": "⭐⭐ STRONG",
                     "WATCH": "⭐ WATCH", "MONITOR": "MONITOR"}

        _section_header(
            f"🎯 Tomorrow's Runner Candidates  ({min(len(runners), 20)} stocks)",
            "Sector RS · Breakout · Volume · Close Near High · Delivery · F&O OI — Multi-factor score",
            GOLD,
        )
        run_hdr = ["#", "Symbol", "Score", "Grade", "Close ₹", "Chg %", "Vol×", "Key Signals"]
        run_rows = [run_hdr]
        cw = [8*mm, 28*mm, 14*mm, 28*mm, 26*mm, 18*mm, 14*mm, 0]
        cw[-1] = 160*mm - sum(cw[:-1])

        for rank, r in enumerate(runners[:20], 1):
            grade = r.get("grade", "WATCH")
            sigs  = " · ".join(r.get("reasons", [])[:3])
            run_rows.append([
                str(rank), r["symbol"], str(r["score"]),
                GRADE_LBL.get(grade, grade),
                f"{r['close']:,.2f}", f"{r['pchange']:+.2f}%",
                f"{r.get('vol_ratio', 0):.1f}×", sigs,
            ])

        t_run = Table(run_rows, colWidths=cw)
        ts_run = TableStyle([
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
        for i, r in enumerate(runners[:20], 1):
            ts_run.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            ts_run.add("TEXTCOLOR", (2, i), (3, i), GRADE_CLR.get(r.get("grade", "WATCH"), GRAY))
            ts_run.add("FONTNAME",  (2, i), (3, i), "Helvetica-Bold")
            pch_clr = GREEN if r["pchange"] >= 0 else RED
            ts_run.add("TEXTCOLOR", (5, i), (5, i), pch_clr)
        t_run.setStyle(ts_run)
        story.append(t_run)
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(
            "⚡ Entry Rule: Buy above previous day's high with fresh volume confirmation  ·  "
            "Stop Loss: below breakout candle's low",
            sty("RULE", size=7, color=GOLD, align=TA_CENTER, italic=True, before=0, after=4),
        ))
        story.append(Spacer(1, 6*mm))

    # ── 5. VWMA(20) Combo Setup ───────────────────────────────────────────────
    if vwma_hits:
        TEAL2 = colors.HexColor("#0891b2")
        _section_header(
            f"VWMA(20) Combo Setup  ({len(vwma_hits)} stocks)",
            "Day N-1: Doji/Hammer/Pin Bar at VWMA(20)  +  Day N: Bullish confirmation  --  TSR universe",
            TEAL2,
        )
        v_hdr = ["#", "Symbol", "LTP", "VWMA(20)", "Dist%", "Reversal Candle", "Confirm Candle", "Chg%"]
        v_rows = [v_hdr]
        cw_v = [7*mm, 26*mm, 20*mm, 20*mm, 13*mm, 36*mm, 36*mm, 14*mm]
        for i, h in enumerate(vwma_hits[:20], 1):
            pat = " | ".join(h.get("reversal_pattern", []))
            rev = f"{pat}  C:{h.get('rev_c', '')}"
            con = f"Bullish  C:{h.get('con_c', '')}"
            v_rows.append([
                str(i), h["symbol"],
                f"{h['ltp']:,.2f}", f"{h['vwma']:,.2f}",
                f"{h['dist_pct']:.2f}%",
                rev, con,
                f"{h['pchange']:+.2f}%",
            ])
        t_v = Table(v_rows, colWidths=cw_v)
        ts_v = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), TEAL2),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (1, 1), (1, -1), "LEFT"),
            ("ALIGN",         (5, 1), (6, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 6.5),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        for i, h in enumerate(vwma_hits[:20], 1):
            ts_v.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            ts_v.add("TEXTCOLOR",  (5, i), (5, i), colors.HexColor("#d97706"))
            ts_v.add("TEXTCOLOR",  (6, i), (6, i), GREEN)
            pch_clr = GREEN if h["pchange"] >= 0 else RED
            ts_v.add("TEXTCOLOR",  (7, i), (7, i), pch_clr)
            ts_v.add("FONTNAME",   (7, i), (7, i), "Helvetica-Bold")
        t_v.setStyle(ts_v)
        story.append(t_v)
        story.append(Spacer(1, 6*mm))

    # ── 6. AI Analysis & Top Picks ────────────────────────────────────────────
    if ai_text:
        _section_header(
            "🤖 AI Analysis & Trend Continuation Picks",
            "GPT-4o · Multi-list signal confirmation · Positional entry strategy",
            COBALT,
        )
        for para in ai_text.strip().split("\n\n"):
            para = para.strip()
            if not para:
                continue
            # Bold any line starting with a known section keyword
            if any(para.upper().startswith(kw) for kw in
                   ("MARKET TREND", "TOP 5", "ENTRY STRATEGY", "RISK FACTORS",
                    "MULTI-CONFIRMED", "MARKET MOMENTUM")):
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
        f"Generated by RRE Market Scanner  ·  "
        f"{datetime.now().strftime('%d %b %Y %H:%M IST')}  ·  "
        "Data: NSE India · TSR Pro · Yahoo Finance · OpenAI",
        sty("F", size=6.5, color=GRAY, align=TA_CENTER, before=6),
    ))
    doc.build(story)
    return buf.getvalue()


# ── Orchestrator ─────────────────────────────────────────────────────────────

def _normalize_tsr_buildup(tsr_bu: dict) -> list[dict]:
    """Convert TSR Pro long_buildup rows into the same shape as NSE FNO rows."""
    results = []
    seen: set[str] = set()
    for r in (tsr_bu or {}).get("long_buildup", []):
        # TSR name field is often "COMPANY NSE" — take first token
        raw_name = r.get("Name") or r.get("Code") or r.get("Symbol") or ""
        sym = raw_name.split()[0].upper().strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        results.append({
            "symbol":     sym,
            "lastPrice":  _safe_float(r.get("Spot Price") or r.get("Price") or 0),
            "pChange":    _safe_float(r.get("Fut Price Change %") or r.get("% Change") or 0),
            "oi_chg_pct": _safe_float(r.get("% Change in OI") or r.get("OI Change %") or 0),
            "source":     "TSR Pro",
        })
    return results


async def pm_scan_and_send() -> None:
    """Run all 2:45 PM scans, build PDF, send via Telegram."""
    from services.nse_service import get_nifty500_ohlc, get_fno_oi_buildup
    from services.tsr_service import get_tsr_buildup
    from services.telegram_service import send_document, send_message
    from services.tomorrow_scanner_service import scan_tomorrow_runners

    logger.info("PM Report: starting 2:45 PM scan")
    date_str = datetime.now().strftime("%d %b %Y")

    try:
        from services.vwma_candle_service import scan_vwma_candle

        # Fast parallel fetches — TSR reads from in-memory cache (non-blocking)
        stocks, buildup_raw, tsr_bu = await asyncio.gather(
            get_nifty500_ohlc(),
            get_fno_oi_buildup(15),
            get_tsr_buildup(),
        )

        # VWMA scan — isolated so a failure never blocks the main report
        vwma_hits = []
        try:
            vwma_hits = await scan_vwma_candle()
            logger.info("PM VWMA scan: %d hits", len(vwma_hits))
        except Exception as e:
            logger.warning("PM VWMA scan failed: %s", e)

        # ── Merge NSE FNO + TSR Pro long buildup with source tags ────────────
        nse_lb = (buildup_raw or {}).get("Long Buildup", [])
        for s in nse_lb:
            s["source"] = "NSE"

        tsr_lb = _normalize_tsr_buildup(tsr_bu or {})

        # Deduplicate: if same symbol in both, mark source as "NSE·TSR"
        nse_syms = {s.get("symbol", "") for s in nse_lb}
        for s in tsr_lb:
            if s["symbol"] in nse_syms:
                # Find existing NSE entry and upgrade its source tag
                for nse_s in nse_lb:
                    if nse_s.get("symbol") == s["symbol"]:
                        nse_s["source"] = "NSE·TSR"
                        break
            else:
                nse_lb.append(s)

        long_buildup = nse_lb
        logger.info("PM Report long buildup: %d NSE + %d TSR unique = %d total",
                    len(nse_syms), len([s for s in tsr_lb if s["symbol"] not in nse_syms]),
                    len(long_buildup))
        breakouts    = scan_consolidation_breakouts(stocks or [])
        pullbacks    = scan_pullback_stocks(stocks or [])

        # Runner scan (Angel One — can take a few minutes)
        runners = []
        try:
            runners = await scan_tomorrow_runners()
        except Exception as e:
            logger.warning("PM Report runners failed: %s", e)

        # AI analysis in thread (blocking OpenAI call)
        ai_text = await asyncio.to_thread(
            _generate_ai_picks, long_buildup, breakouts, pullbacks, runners
        )

        # Build PDF in thread
        pdf_bytes = await asyncio.to_thread(
            _build_pm_pdf, date_str, long_buildup, breakouts, pullbacks, runners, ai_text,
            vwma_hits or [],
        )

        # Telegram caption
        high_r  = [r["symbol"] for r in runners if r.get("grade") == "HIGH"][:5]
        str_r   = [r["symbol"] for r in runners if r.get("grade") == "STRONG"][:3]
        lb_top  = [r.get("symbol", "") for r in long_buildup[:5]]
        bo_top  = [r["symbol"] for r in breakouts[:5]]
        pb_top  = [r["symbol"] for r in pullbacks[:5]]

        caption_lines = [
            f"📊 <b>RRE 2:45 PM Positional Report — {date_str}</b>",
            "",
            f"🟢 <b>F&O Long Buildup</b>: {len(long_buildup)} stocks",
            f"  {' · '.join(lb_top) or '—'}",
            f"⚡ <b>Consolidation Breakouts</b>: {len(breakouts)} stocks",
            f"  {' · '.join(bo_top) or '—'}",
            f"🔄 <b>Pullback Setups</b>: {len(pullbacks)} stocks",
            f"  {' · '.join(pb_top) or '—'}",
            f"🎯 <b>Tomorrow's Runners</b>: {len(runners)} candidates",
        ]
        if high_r:
            caption_lines.append(f"  ⭐⭐⭐ HIGH: {' · '.join(high_r)}")
        if str_r:
            caption_lines.append(f"  ⭐⭐ STRONG: {' · '.join(str_r)}")
        caption_lines.append("\n🤖 AI analysis &amp; trend picks included in PDF")

        filename = f"RRE_PM_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        await send_document(pdf_bytes, filename, "\n".join(caption_lines))
        logger.info(
            "PM Report sent — %d LB · %d breakouts · %d pullbacks · %d runners",
            len(long_buildup), len(breakouts), len(pullbacks), len(runners),
        )

    except Exception as exc:
        logger.warning("PM Report error: %s", exc, exc_info=True)
        try:
            from services.telegram_service import send_message
            await send_message(f"⚠️ 2:45 PM Report failed: {exc}")
        except Exception:
            pass


# ── Background Loop ───────────────────────────────────────────────────────────

async def pm_report_loop() -> None:
    """Background loop: fires once at 14:45 IST on every market day (Mon–Fri)."""
    logger.info("PM report loop started")
    fired_today: str = ""
    await asyncio.sleep(30)

    while True:
        now = datetime.now()
        today_key = now.strftime("%Y-%m-%d")
        if (now.weekday() < 5               # Mon–Fri only
                and now.hour == 14
                and now.minute >= 45
                and now.minute < 55
                and fired_today != today_key):
            fired_today = today_key
            await pm_scan_and_send()
        await asyncio.sleep(60)
