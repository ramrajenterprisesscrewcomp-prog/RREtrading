"""
candle_service.py
Daily EOD candlestick pattern scanner for Nifty 500 stocks.
Sends PDF report via Telegram at 4:00 PM IST on market days.
"""
import asyncio
import io
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

PATTERNS = [
    "Long White Line",
    "Bullish Pin Bar",
    "Hammer",
    "Inverted Hammer",
    "Doji",
    "Long Legged Doji",
    "Spinning Top",
    "Bearish Pin Bar",
]

BULLISH_PATTERNS = {"Hammer", "Inverted Hammer", "Bullish Pin Bar", "Long White Line"}
BEARISH_PATTERNS = {"Bearish Pin Bar"}
NEUTRAL_PATTERNS = {"Doji", "Spinning Top", "Long Legged Doji"}

PATTERN_DESC = {
    "Doji":             "Open ≈ Close · Indecision, watch for reversal",
    "Hammer":           "Long lower shadow · Bullish reversal signal after downtrend",
    "Inverted Hammer":  "Long upper shadow below · Potential bullish reversal",
    "Spinning Top":     "Small body, both shadows · Market uncertainty",
    "Long Legged Doji": "Very long shadows · High volatility / indecision",
    "Bearish Pin Bar":  "Long upper shadow · Bearish rejection of higher prices",
    "Bullish Pin Bar":  "Long lower shadow · Bullish rejection of lower prices",
    "Long White Line":  "Large bullish candle · Strong buying momentum",
}


def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def detect_pattern(o: float, h: float, l: float, c: float) -> list[str]:
    """Return list of candlestick patterns matched for a single OHLC bar."""
    hl = h - l
    if hl < 0.001:
        return []

    body          = abs(c - o)
    body_pct      = body / hl
    upper_shadow  = h - max(o, c)
    lower_shadow  = min(o, c) - l

    patterns: list[str] = []

    # ── Doji family (body < 5% of range) ─────────────────────────────────
    if body_pct < 0.05:
        if upper_shadow / hl > 0.35 and lower_shadow / hl > 0.35:
            patterns.append("Long Legged Doji")
        else:
            patterns.append("Doji")
        return patterns  # doji supersedes other single-candle patterns

    # ── Spinning Top: small body, both shadows significant ────────────────
    if (body_pct < 0.30
            and upper_shadow > body * 0.5
            and lower_shadow > body * 0.5):
        patterns.append("Spinning Top")

    # ── Hammer: small body near top of bar, long lower tail ───────────────
    body_bottom = min(o, c)
    body_top    = max(o, c)
    if (body_pct < 0.35
            and lower_shadow >= 2.0 * max(body, 0.001)
            and upper_shadow <= 0.15 * hl
            and body_bottom >= l + 0.55 * hl):
        patterns.append("Hammer")

    # ── Inverted Hammer: small body near bottom, long upper tail ──────────
    if (body_pct < 0.35
            and upper_shadow >= 2.0 * max(body, 0.001)
            and lower_shadow <= 0.15 * hl
            and body_top <= l + 0.45 * hl):
        patterns.append("Inverted Hammer")

    # ── Bullish Pin Bar: long lower shadow (>= 60% range), close near high
    if (lower_shadow / hl >= 0.60
            and (h - c) / hl <= 0.20
            and "Hammer" not in patterns):
        patterns.append("Bullish Pin Bar")

    # ── Bearish Pin Bar: long upper shadow (>= 60% range), close near low
    if (upper_shadow / hl >= 0.60
            and (c - l) / hl <= 0.20):
        patterns.append("Bearish Pin Bar")

    # ── Long White Line: large bullish body (>= 60% range) ───────────────
    if c > o and body_pct >= 0.60:
        patterns.append("Long White Line")

    return patterns


def _build_pdf(matches: dict, date_str: str, total_scanned: int,
               tsr_sup: dict | None = None, vb_results: list | None = None,
               runners: list | None = None,
               vol_buzz: dict | None = None) -> bytes:
    """Generate PDF bytes using ReportLab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer,
        Table, TableStyle, HRFlowable,
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    DARK_BLUE  = colors.HexColor("#1e3a5f")
    GREEN      = colors.HexColor("#059669")
    RED        = colors.HexColor("#dc2626")
    AMBER      = colors.HexColor("#d97706")
    PURPLE     = colors.HexColor("#7c3aed")
    GRAY       = colors.HexColor("#6b7280")
    LIGHT_GRAY = colors.HexColor("#f3f4f6")
    ALT_ROW    = colors.HexColor("#f9fafb")
    BORDER     = colors.HexColor("#e5e7eb")

    PATTERN_COLOR = {
        "Long White Line":  GREEN,
        "Bullish Pin Bar":  GREEN,
        "Hammer":           GREEN,
        "Inverted Hammer":  GREEN,
        "Doji":             AMBER,
        "Long Legged Doji": PURPLE,
        "Spinning Top":     GRAY,
        "Bearish Pin Bar":  RED,
    }

    def make_style(name, font="Helvetica", size=9, color=DARK_BLUE,
                   align=TA_LEFT, before=0, after=2, bold=False):
        return ParagraphStyle(
            name, fontName=f"Helvetica-Bold" if bold else font,
            fontSize=size, textColor=color,
            alignment=align, spaceBefore=before, spaceAfter=after,
        )

    title_s    = make_style("T",  size=15, color=DARK_BLUE,  align=TA_CENTER, bold=True, after=3)
    sub_s      = make_style("S",  size=8,  color=GRAY,        align=TA_CENTER, after=2)
    section_s  = make_style("SE", size=10, color=DARK_BLUE,   bold=True, before=6, after=3)
    desc_s     = make_style("D",  size=7.5, color=GRAY,       before=0, after=3,
                             font="Helvetica-Oblique")
    footer_s   = make_style("F",  size=6.5, color=GRAY, align=TA_CENTER, before=8)
    summary_s  = make_style("SM", size=8,  color=DARK_BLUE, before=2, after=2)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=12*mm, bottomMargin=12*mm,
    )

    story = []
    story.append(Paragraph("RRE Market Scanner", title_s))
    story.append(Paragraph("EOD Candlestick Pattern Report  ·  Nifty 500 Universe", sub_s))
    story.append(Paragraph(f"Date: {date_str}", sub_s))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=DARK_BLUE))
    story.append(Spacer(1, 3*mm))

    total_signals = sum(len(v) for v in matches.values())
    active_pats   = sum(1 for v in matches.values() if v)
    runners_count = len(runners) if runners else 0
    story.append(Paragraph(
        f"Scanned <b>{total_scanned}</b> stocks  ·  "
        f"<b>{total_signals}</b> pattern signals  ·  "
        f"<b>{runners_count}</b> tomorrow's runner candidates",
        summary_s,
    ))
    story.append(Spacer(1, 4*mm))

    # ── Tomorrow's High-Probability Runners (FIRST section) ───────────────
    if runners:
        GOLD2     = colors.HexColor("#d97706")
        EMERALD   = colors.HexColor("#059669")
        COBALT    = colors.HexColor("#2563eb")
        ROSE      = colors.HexColor("#e11d48")
        GRADE_CLR = {"HIGH": EMERALD, "STRONG": COBALT, "WATCH": GOLD2, "MONITOR": GRAY}

        story.append(HRFlowable(width="100%", thickness=1.5, color=GOLD2))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(
            f"🎯 Tomorrow's High-Probability Runners  ({len(runners)} stocks)",
            make_style("RUN_HDR", size=11, color=GOLD2, bold=True,
                       align=TA_CENTER, before=2, after=2),
        ))
        story.append(Paragraph(
            "Strong Sector · Breakout · Volume · Close Near High · RS · Delivery · F&O OI",
            make_style("RUN_DESC", size=7, color=GRAY, align=TA_CENTER,
                       font="Helvetica-Oblique", before=0, after=4),
        ))

        GRADE_LABEL = {
            "HIGH":    "⭐⭐⭐ HIGH",
            "STRONG":  "⭐⭐ STRONG",
            "WATCH":   "⭐ WATCH",
            "MONITOR": "MONITOR",
        }
        run_hdr = ["#", "Symbol", "Score", "Grade", "Close ₹", "Chg %", "Vol×", "Key Signals"]
        run_rows = [run_hdr]
        col_widths_run = [8*mm, 28*mm, 16*mm, 26*mm, 26*mm, 20*mm, 18*mm, 0]
        available = 160*mm - sum(col_widths_run[:-1])
        col_widths_run[-1] = available

        for rank, r in enumerate(runners[:20], 1):
            grade = r.get("grade", "WATCH")
            sigs  = " · ".join(r.get("reasons", [])[:4])
            run_rows.append([
                str(rank),
                r["symbol"],
                str(r["score"]),
                GRADE_LABEL.get(grade, grade),
                f"{r['close']:,.2f}",
                f"{r['pchange']:+.2f}%",
                f"{r.get('vol_ratio', 0):.1f}×",
                sigs,
            ])

        t_run = Table(run_rows, colWidths=col_widths_run)
        ts_run = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), GOLD2),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (7, 1), (7, -1), "LEFT"),
            ("ALIGN",         (1, 1), (1, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        for i, r in enumerate(runners[:20], start=1):
            bg = ALT_ROW if i % 2 == 0 else colors.white
            ts_run.add("BACKGROUND", (0, i), (-1, i), bg)
            grade_clr = GRADE_CLR.get(r.get("grade", "WATCH"), GRAY)
            ts_run.add("TEXTCOLOR", (2, i), (3, i), grade_clr)
            ts_run.add("FONTNAME",  (2, i), (3, i), "Helvetica-Bold")
            pch_clr = GREEN if r["pchange"] >= 0 else RED
            ts_run.add("TEXTCOLOR", (5, i), (5, i), pch_clr)

        t_run.setStyle(ts_run)
        story.append(t_run)
        story.append(Spacer(1, 6*mm))

        # Next-day entry rule reminder
        story.append(Paragraph(
            "⚡ Entry Rule: Buy above previous day high with fresh volume confirmation  ·  "
            "Stop Loss: below breakout candle low or previous day low",
            make_style("RUN_RULE", size=7, color=GOLD2, align=TA_CENTER,
                       font="Helvetica-Oblique", before=0, after=4),
        ))
        story.append(Spacer(1, 5*mm))

    for pattern in PATTERNS:
        stocks = matches.get(pattern, [])
        if not stocks:
            continue

        pat_color = PATTERN_COLOR.get(pattern, GRAY)
        cat = ("🟢 BULLISH" if pattern in BULLISH_PATTERNS
               else "🔴 BEARISH" if pattern in BEARISH_PATTERNS
               else "🟡 NEUTRAL")

        story.append(Paragraph(
            f"{cat}  ·  <b>{pattern}</b>  ({len(stocks)} stocks)",
            section_s,
        ))
        story.append(Paragraph(PATTERN_DESC.get(pattern, ""), desc_s))

        hdr = ["Symbol", "Close ₹", "Chg %", "Volume", "F&O Signal"]
        rows = [hdr]
        for s in sorted(stocks, key=lambda x: abs(x.get("pchange", 0)), reverse=True)[:30]:
            rows.append([
                s["symbol"],
                f"{s['close']:,.2f}",
                f"{s['pchange']:+.2f}%",
                _fmt_vol(s.get("volume", 0)),
                s.get("tsr_signal", "—"),
            ])

        t = Table(rows, colWidths=[45*mm, 30*mm, 22*mm, 28*mm, 35*mm])
        ts = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), pat_color),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 8),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (0, 1), (0, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7.5),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        # Alternate row colors
        for i in range(1, len(rows)):
            bg = ALT_ROW if i % 2 == 0 else colors.white
            ts.add("BACKGROUND", (0, i), (-1, i), bg)

        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 5*mm))

    if not any(matches.values()):
        story.append(Paragraph(
            "No candlestick patterns detected in today's session.",
            desc_s,
        ))

    # ── Volume Buzz & Multi-Year Breakout Section ─────────────────────────
    if vb_results:
        GOLD   = colors.HexColor("#b45309")
        DKGREEN = colors.HexColor("#065f46")
        BLUE   = colors.HexColor("#1d4ed8")
        LGRAY  = colors.HexColor("#6b7280")

        LEVEL_COLOR = {
            "10Y": GOLD, "7Y": GOLD,
            "5Y": DKGREEN, "3Y": DKGREEN,
            "2Y": BLUE, "1Y": BLUE,
            "52W": LGRAY,
        }

        story.append(Spacer(1, 5*mm))
        story.append(HRFlowable(width="100%", thickness=1.2, color=GOLD))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(
            f"🚀 Volume Buzz · Multi-Year Breakouts  ({len(vb_results)} stocks)",
            make_style("VB_HDR", size=11, color=GOLD, bold=True, align=TA_CENTER, before=2, after=3),
        ))
        story.append(Paragraph(
            "Stocks within 1.5% of 52W high · Classified by longest breakout period · "
            "🔊 = Volume ≥ 2× 20-day average",
            make_style("VB_DESC", size=7, color=GRAY, align=TA_CENTER, before=0, after=4,
                       font="Helvetica-Oblique"),
        ))

        hdr = ["Symbol", "Close ₹", "Chg %", "Period High", "Vol Ratio", "Buzz"]
        vb_rows = [hdr]
        for r in vb_results[:50]:
            vb_rows.append([
                r["symbol"],
                f"{r['close']:,.2f}",
                f"{r['pchange']:+.2f}%",
                r["breakout_level"],
                f"{r['vol_ratio']}×",
                "🔊" if r["is_buzz"] else "—",
            ])

        col_w = [42*mm, 28*mm, 22*mm, 25*mm, 25*mm, 18*mm]
        t = Table(vb_rows, colWidths=col_w)
        ts_vb = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), GOLD),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 8),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (0, 1), (0, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7.5),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        for i, row in enumerate(vb_results[:50], start=1):
            bg = ALT_ROW if i % 2 == 0 else colors.white
            ts_vb.add("BACKGROUND", (0, i), (-1, i), bg)
            level_col = LEVEL_COLOR.get(row["breakout_level"], LGRAY)
            ts_vb.add("TEXTCOLOR", (3, i), (3, i), level_col)
            ts_vb.add("FONTNAME",  (3, i), (3, i), "Helvetica-Bold")
        t.setStyle(ts_vb)
        story.append(t)
        story.append(Spacer(1, 5*mm))

    # ── Volume Buzz Section (5× and 2× tiers) ────────────────────────────
    five_x = (vol_buzz or {}).get("5x", [])
    two_x  = (vol_buzz or {}).get("2x", [])
    if five_x or two_x:
        FIRE  = colors.HexColor("#dc2626")   # 5× — red/fire
        AMBER = colors.HexColor("#d97706")   # 2× — amber

        story.append(Spacer(1, 5*mm))
        story.append(HRFlowable(width="100%", thickness=1.2, color=FIRE))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(
            f"📊 Volume Buzz  ·  20-Day Average  "
            f"({len(five_x)} stocks at 5×  ·  {len(two_x)} stocks at 2×)",
            make_style("VBZ_HDR", size=11, color=FIRE, bold=True,
                       align=TA_CENTER, before=2, after=3),
        ))
        story.append(Paragraph(
            "Unusual volume vs 20-day average — potential institutional activity, breakout fuel or distribution",
            make_style("VBZ_DESC", size=7, color=GRAY, align=TA_CENTER,
                       font="Helvetica-Oblique", before=0, after=4),
        ))

        def _buzz_table(rows_data: list, hdr_color, tier_label: str):
            if not rows_data:
                return
            story.append(Paragraph(
                tier_label,
                make_style(f"VBZ_T_{tier_label[:2]}", size=9, color=hdr_color,
                           bold=True, before=2, after=2),
            ))
            hdr = ["Symbol", "Close ₹", "Chg %", "Volume", "Avg Vol (20D)", "Vol Ratio"]
            rows = [hdr]
            for r in rows_data[:30]:
                rows.append([
                    r["symbol"],
                    f"{r['close']:,.2f}",
                    f"{r['pchange']:+.2f}%",
                    _fmt_vol(r["volume"]),
                    _fmt_vol(r["avg_vol"]),
                    f"{r['vol_ratio']}×",
                ])
            col_w = [42*mm, 26*mm, 20*mm, 24*mm, 28*mm, 20*mm]
            t = Table(rows, colWidths=col_w)
            ts = TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0), hdr_color),
                ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
                ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE",      (0, 0), (-1, 0), 8),
                ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                ("ALIGN",         (0, 1), (0, -1), "LEFT"),
                ("FONTSIZE",      (0, 1), (-1, -1), 7.5),
                ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
                ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ])
            for i, r in enumerate(rows_data[:30], start=1):
                bg = ALT_ROW if i % 2 == 0 else colors.white
                ts.add("BACKGROUND", (0, i), (-1, i), bg)
                # Color the vol ratio column
                ratio_clr = FIRE if r["vol_ratio"] >= 5.0 else AMBER
                ts.add("TEXTCOLOR", (5, i), (5, i), ratio_clr)
                ts.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
                # Color pchange
                pch_clr = GREEN if r["pchange"] >= 0 else RED
                ts.add("TEXTCOLOR", (2, i), (2, i), pch_clr)
            t.setStyle(ts)
            story.append(t)
            story.append(Spacer(1, 4*mm))

        _buzz_table(five_x, FIRE,  f"🔥 Extreme Buzz — 5× Volume  ({len(five_x)} stocks)")
        _buzz_table(two_x,  AMBER, f"🔊 High Buzz — 2× Volume  ({len(two_x)} stocks)")

    # ── TSR Support Levels Section ─────────────────────────────────────────
    if tsr_sup:
        TEAL = colors.HexColor("#0e7490")
        INDIGO = colors.HexColor("#4338ca")

        def _render_support_table(rows_data: list, header_color, label: str):
            if not rows_data:
                return
            story.append(Spacer(1, 4*mm))
            story.append(Paragraph(label, section_s))

            # Determine columns from first row keys
            sample = rows_data[0]
            col_keys = list(sample.keys())[:6]  # cap at 6 columns
            hdr_row = [k.replace("\n", " ").title() for k in col_keys]
            table_rows = [hdr_row]
            for r in rows_data[:40]:
                table_rows.append([str(r.get(k, "—"))[:20] for k in col_keys])

            col_w = 160 * mm / max(len(col_keys), 1)
            col_widths = [col_w] * len(col_keys)

            t = Table(table_rows, colWidths=col_widths)
            ts = TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0), header_color),
                ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
                ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE",      (0, 0), (-1, 0), 8),
                ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                ("ALIGN",         (0, 1), (0, -1), "LEFT"),
                ("FONTSIZE",      (0, 1), (-1, -1), 7.5),
                ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
                ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ])
            for i in range(1, len(table_rows)):
                bg = ALT_ROW if i % 2 == 0 else colors.white
                ts.add("BACKGROUND", (0, i), (-1, i), bg)
            t.setStyle(ts)
            story.append(t)

        story.append(Spacer(1, 5*mm))
        story.append(HRFlowable(width="100%", thickness=1, color=TEAL))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph("📍 TSR Support Levels", make_style(
            "SUP_HDR", size=11, color=TEAL, bold=True, align=TA_CENTER, before=2, after=3
        )))

        _render_support_table(
            tsr_sup.get("weekly", []), TEAL,
            f"📅 Stocks Near Weekly Support  ({len(tsr_sup.get('weekly', []))} stocks)"
        )
        _render_support_table(
            tsr_sup.get("monthly", []), INDIGO,
            f"🗓 Stocks Near Monthly Support  ({len(tsr_sup.get('monthly', []))} stocks)"
        )

        if not tsr_sup.get("weekly") and not tsr_sup.get("monthly"):
            story.append(Paragraph("No TSR support level data available.", desc_s))

    story.append(Spacer(1, 5*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
    story.append(Paragraph(
        f"Generated by RRE Market Scanner  ·  "
        f"{datetime.now().strftime('%d %b %Y %H:%M IST')}  ·  "
        "Data: NSE India",
        footer_s,
    ))

    doc.build(story)
    return buf.getvalue()


def _fmt_vol(n) -> str:
    n = int(n or 0)
    if n >= 10_000_000:
        return f"{n/10_000_000:.1f}Cr"
    if n >= 100_000:
        return f"{n/100_000:.1f}L"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n) if n else "—"


async def scan_and_send() -> None:
    """Fetch Nifty 500 OHLC, detect patterns, generate PDF, send to Telegram."""
    from services.nse_service import get_nifty500_ohlc
    from services.telegram_service import send_document
    from services.tsr_service import get_tsr_buildup, get_tsr_support
    from services.volume_breakout_service import scan_volume_breakouts, scan_volume_buzz
    from services.tomorrow_scanner_service import scan_tomorrow_runners

    logger.info("Candle scanner: starting Nifty 500 EOD scan")
    try:
        # Fast fetches in parallel
        stocks, tsr_bu, tsr_sup = await asyncio.gather(
            get_nifty500_ohlc(),
            get_tsr_buildup(),
            get_tsr_support(),
        )

        # Heavy Angel One scans — sequential to respect rate limits
        vb_results = []
        vol_buzz   = {"5x": [], "2x": []}
        runners    = []
        try:
            vb_results = await scan_volume_breakouts()
        except Exception as e:
            logger.warning("Volume breakout scan failed: %s", e)
        await asyncio.sleep(8)   # let Angel One rate-limit window reset
        try:
            vol_buzz = await scan_volume_buzz(stocks or [])
        except Exception as e:
            logger.warning("Volume buzz scan failed: %s", e)
        await asyncio.sleep(8)
        try:
            runners = await scan_tomorrow_runners()
        except Exception as e:
            logger.warning("Tomorrow runner scan failed: %s", e)
        if not stocks:
            logger.warning("Candle scanner: no OHLC data from NSE")
            return

        # Build TSR signal map: symbol → label
        tsr_map: dict[str, str] = {}
        for sym in [r.get("Name", r.get("Code", "")).split()[0]
                    for r in tsr_bu.get("long_buildup", [])]:
            if sym:
                tsr_map[sym.upper()] = "LB 📈"
        for sym in [r.get("Name", r.get("Code", "")).split()[0]
                    for r in tsr_bu.get("short_buildup", [])]:
            if sym:
                tsr_map[sym.upper()] = "SB 📉"
        for sym in [r.get("Name", r.get("Code", "")).split()[0]
                    for r in tsr_bu.get("short_covering", [])]:
            if sym:
                tsr_map.setdefault(sym.upper(), "SC 🔄")

        matches: dict[str, list[dict]] = {p: [] for p in PATTERNS}
        for s in stocks:
            found = detect_pattern(s["open"], s["high"], s["low"], s["close"])
            tsr_tag = tsr_map.get(s["symbol"], "—")
            for p in found:
                if p in matches:
                    matches[p].append({**s, "tsr_signal": tsr_tag})

        date_str  = datetime.now().strftime("%d %b %Y")
        pdf_bytes = await asyncio.to_thread(
            _build_pdf, matches, date_str, len(stocks), tsr_sup, vb_results, runners, vol_buzz
        )

        total = sum(len(v) for v in matches.values())
        buzz_count = sum(1 for r in vb_results if r["is_buzz"])
        lines = [
            f"📊 <b>RRE EOD Candle Scanner — {date_str}</b>",
            f"Nifty 500 · {len(stocks)} stocks scanned · {total} signals\n",
        ]
        # Tomorrow's runners summary (top 5)
        if runners:
            high  = [r["symbol"] for r in runners if r["grade"] == "HIGH"][:5]
            strong = [r["symbol"] for r in runners if r["grade"] == "STRONG"][:3]
            lines.append(f"\n🎯 <b>Tomorrow's Runners</b>: {len(runners)} candidates")
            if high:
                lines.append(f"⭐⭐⭐ HIGH: {' · '.join(high)}")
            if strong:
                lines.append(f"⭐⭐ STRONG: {' · '.join(strong)}")
            lines.append("")
        for p in PATTERNS:
            if matches[p]:
                cat = "🟢" if p in BULLISH_PATTERNS else ("🔴" if p in BEARISH_PATTERNS else "🟡")
                lines.append(f"{cat} <b>{p}</b>: {len(matches[p])} stocks")
        if vb_results:
            lines.append(
                f"\n🚀 <b>Multi-Year Breakouts</b>: {len(vb_results)} stocks"
                f"  🔊 Volume Buzz: {buzz_count}"
            )
        five_x = vol_buzz.get("5x", [])
        two_x  = vol_buzz.get("2x", [])
        if five_x or two_x:
            lines.append(f"\n📊 <b>Volume Buzz</b>: {len(five_x)} stocks at 5×  ·  {len(two_x)} stocks at 2×")
            if five_x:
                top5 = " · ".join(r["symbol"] for r in five_x[:5])
                lines.append(f"🔥 5×+: {top5}")

        filename = f"RRE_Candle_{datetime.now().strftime('%Y%m%d')}.pdf"
        await send_document(pdf_bytes, filename, "\n".join(lines))
        logger.info("Candle scanner: PDF sent — %d signals across %d patterns", total, len(matches))
    except Exception as exc:
        logger.warning("Candle scanner error: %s", exc, exc_info=True)


async def candle_alert_loop() -> None:
    """Background loop: fires once at 16:00 IST on every market day (Mon–Fri)."""
    logger.info("Candle alert loop started")
    fired_today: str = ""
    await asyncio.sleep(30)

    while True:
        now = datetime.now()
        today_key = now.strftime("%Y-%m-%d")
        if (now.weekday() < 5
                and now.hour == 16
                and now.minute < 5
                and fired_today != today_key):
            fired_today = today_key
            await scan_and_send()
            # Also send VWMA candle setup report from TSR universe
            try:
                from services.vwma_candle_service import send_vwma_candle_report
                await send_vwma_candle_report()
            except Exception as e:
                logger.warning("VWMA candle report failed: %s", e)
        await asyncio.sleep(60)
