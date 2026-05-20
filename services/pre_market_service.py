"""
pre_market_service.py
9:30 AM Pre-Market Analysis Report — fires Mon-Fri.

Sections:
  1. Index Snapshot       — Nifty 50 / Bank Nifty / India VIX live at 9:30 AM
  2. Options Pulse        — PCR, resistance, support, writer bias
  3. AI Market Bias       — direction + key levels + risks
  4. Runner Follow-through— did yesterday's candidates gap up today?
  5. Gap-Up Leaders       — top movers from previous close
  6. Gap-Down Alerts      — stocks under pressure (avoid list)
  7. VWMA Retrace Setups  — daily VWMA(20) bounce candidates for swing entry
  8. AI Trade Plan        — top 5 actionable stocks with entry/target/stop
  9. Portfolio Morning    — P&L check at 9:30 AM prices
"""
import asyncio
import io
import json
import logging
import pathlib
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_IST          = timezone(timedelta(hours=5, minutes=30))
_DATA_DIR     = pathlib.Path(__file__).parent.parent / "data"
_RUNNERS_FILE = _DATA_DIR / "prev_runners.json"


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
    return str(n) if n else "-"


def _load_prev_runners() -> tuple[str, list[dict]]:
    try:
        if _RUNNERS_FILE.exists():
            data = json.loads(_RUNNERS_FILE.read_text(encoding="utf-8"))
            return data.get("date", ""), data.get("runners", [])
    except Exception:
        pass
    return "", []


def _get_gap_leaders(stocks: list[dict], top_n: int = 15) -> tuple[list, list]:
    valid = [s for s in stocks if _safe_float(s.get("pchange", 0)) != 0]
    sorted_s = sorted(valid, key=lambda x: -_safe_float(x.get("pchange", 0)))
    gap_ups   = [s for s in sorted_s[:top_n] if _safe_float(s.get("pchange", 0)) > 1.5]
    gap_downs = [s for s in sorted_s[-10:][::-1] if _safe_float(s.get("pchange", 0)) < -1.5]
    return gap_ups, gap_downs


# ── AI: Pre-Market Trade Plan ─────────────────────────────────────────────────

def _generate_premarket_ai(
    gap_ups:      list[dict],
    vwma_hits:    list[dict],
    prev_runners: list[dict],
    nifty_data:   dict,
    oi_opinion:   dict,
    portfolio:    list[dict],
) -> dict:
    """Returns {market_bias, top_trades, avoid_today, key_levels}."""
    try:
        from services.openai_service import _get_client, _is_ai_hours
        if not _is_ai_hours():
            return {}

        nifty_val = _safe_float(nifty_data.get("value", 0))
        nifty_pch = _safe_float(nifty_data.get("pchange", 0))

        prompt_data = {
            "nifty":       f"{nifty_val:.0f} ({nifty_pch:+.2f}%)",
            "pcr":         round(oi_opinion.get("pcr", 0), 2),
            "sentiment":   oi_opinion.get("sentiment", ""),
            "resistance":  oi_opinion.get("resistance", 0),
            "support":     oi_opinion.get("support", 0),
            "gap_ups":     [{"s": s["symbol"], "pch": round(_safe_float(s.get("pchange", 0)), 2),
                             "open": round(_safe_float(s.get("open", 0)), 1)} for s in gap_ups[:10]],
            "vwma_setups": [{"s": s.get("symbol", ""), "vwma": round(s.get("vwma", 0), 1),
                             "ltp": round(_safe_float(s.get("ltp", 0)), 1),
                             "patterns": ", ".join(s.get("patterns", []))} for s in vwma_hits[:6]],
            "runners":     [{"s": r.get("symbol", ""), "grade": r.get("grade", "")}
                            for r in prev_runners[:8]],
            "portfolio":   [{"s": p["symbol"], "avg": round(_safe_float(p.get("avg_price", 0)), 0),
                             "ltp": round(_safe_float(p.get("ltp", 0)), 0),
                             "pnl_pct": round(_safe_float(p.get("pnl_pct", 0)), 1)}
                            for p in portfolio[:5]],
        }

        prompt = (
            "Indian stock market pre-market snapshot (9:30 AM IST). "
            f"Data: {json.dumps(prompt_data)}\n\n"
            'Return JSON only: {"market_bias": "<BULLISH|BEARISH|NEUTRAL> — 40-word explanation", '
            '"top_trades": [{"symbol": str, "action": "BUY|WATCH|AVOID", '
            '"entry": "price or range", "target": "price", "stop": "price", '
            '"reason": "max 25 words"}], '
            '"avoid_today": "30-word risk warning", '
            '"key_levels": "Nifty support X resistance Y"} '
            "Rules: top_trades = exactly 5 stocks chosen from gap_ups + vwma_setups + runners. "
            "entry/target/stop must be real price numbers based on the data given. "
            "Be specific, concise, actionable for intraday/positional Indian equities."
        )

        client = _get_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=700,
            temperature=0.3,
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:
        logger.warning("Pre-market AI failed: %s", exc)
        return {}


# ── PDF Builder ───────────────────────────────────────────────────────────────

def _build_premarket_pdf(
    date_str:          str,
    time_str:          str,
    gap_ups:           list[dict],
    gap_downs:         list[dict],
    vwma_hits:         list[dict],
    prev_runners:      list[dict],
    prev_runners_date: str,
    stock_map:         dict,
    nifty_data:        dict,
    bnifty_data:       dict,
    vix_data:          dict,
    oi_opinion:        dict,
    portfolio:         list[dict],
    ai_plan:           dict,
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
    NAVY      = colors.HexColor("#0f172a")
    GREEN     = colors.HexColor("#059669")
    RED       = colors.HexColor("#dc2626")
    AMBER     = colors.HexColor("#d97706")
    COBALT    = colors.HexColor("#1d4ed8")
    TEAL      = colors.HexColor("#0e7490")
    GRAY      = colors.HexColor("#6b7280")
    BORDER    = colors.HexColor("#e5e7eb")
    ALT_ROW   = colors.HexColor("#f9fafb")
    GOLD      = colors.HexColor("#d97706")

    def sty(name, font="Helvetica", size=9, color=DARK_BLUE,
            align=TA_LEFT, before=0, after=2, bold=False, italic=False):
        fn = ("Helvetica-BoldOblique" if (bold and italic)
              else "Helvetica-Bold"   if bold
              else "Helvetica-Oblique" if italic
              else font)
        return ParagraphStyle(name, fontName=fn, fontSize=size, textColor=color,
                              alignment=align, spaceBefore=before, spaceAfter=after,
                              leading=size * 1.35)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=12*mm,  bottomMargin=12*mm,
    )
    story = []

    # ── Cover ─────────────────────────────────────────────────────────────────
    story.append(Paragraph(
        "RRE Pre-Market Analysis  9:30 AM",
        sty("T", size=15, color=DARK_BLUE, align=TA_CENTER, bold=True, after=3),
    ))
    story.append(Paragraph(
        f"Nifty 500 Universe  |  {date_str}  |  Early Movers, VWMA Setups & Trade Plan",
        sty("S", size=8, color=GRAY, align=TA_CENTER, after=2),
    ))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=DARK_BLUE))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        f"Gap-Up: <b>{len(gap_ups)}</b>  |  "
        f"Gap-Down: <b>{len(gap_downs)}</b>  |  "
        f"VWMA Setups: <b>{len(vwma_hits)}</b>  |  "
        f"Runner Candidates (prev): <b>{len(prev_runners)}</b>",
        sty("SUM", size=8, color=DARK_BLUE, align=TA_CENTER, after=2),
    ))
    story.append(Spacer(1, 5*mm))

    def _section_header(title, subtitle, clr):
        story.append(HRFlowable(width="100%", thickness=1.2, color=clr))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(title,    sty("SH",  size=11, color=clr,  bold=True,
                                              align=TA_CENTER, before=2, after=2)))
        story.append(Paragraph(subtitle, sty("SD",  size=7,  color=GRAY, italic=True,
                                              align=TA_CENTER, before=0, after=4)))

    def _simple_table(data_rows, col_widths, hdr_color):
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

    # ── 1. Index Snapshot ─────────────────────────────────────────────────────
    _section_header(
        "Index Snapshot",
        f"Live prices as of {time_str} IST  |  NIFTY 50 / Bank Nifty / India VIX",
        DARK_BLUE,
    )

    def _idx_row(label, d):
        val  = _safe_float(d.get("value", 0))
        pch  = _safe_float(d.get("pchange", 0))
        opn  = _safe_float(d.get("open", 0))
        h    = _safe_float(d.get("high", 0))
        lo   = _safe_float(d.get("low", 0))
        prev = _safe_float(d.get("prev", 0))
        rng  = round(h - lo, 1) if h and lo else 0
        arrow = "▲" if pch >= 0 else "▼"
        return [
            label,
            f"{val:,.1f}",
            f"{arrow}{abs(pch):.2f}%",
            f"{opn:,.1f}" if opn else "-",
            f"{h:,.1f}"   if h   else "-",
            f"{lo:,.1f}"  if lo  else "-",
            f"{rng:,.1f}" if rng else "-",
            f"{prev:,.1f}" if prev else "-",
        ]

    idx_rows = [["Index", "LTP", "Chg %", "Open", "High", "Low", "Range", "Prev Close"]]
    idx_rows.append(_idx_row("NIFTY 50",   nifty_data))
    idx_rows.append(_idx_row("BANK NIFTY", bnifty_data))
    idx_rows.append(_idx_row("INDIA VIX",  vix_data))
    cw_idx = [34*mm, 24*mm, 21*mm, 21*mm, 21*mm, 21*mm, 18*mm, 20*mm]
    t_idx, ts_idx = _simple_table(idx_rows, cw_idx, DARK_BLUE)
    for i, (d, is_vix) in enumerate([(nifty_data, False), (bnifty_data, False), (vix_data, True)], 1):
        pch = _safe_float(d.get("pchange", 0))
        # VIX rising = bearish (red); indices rising = bullish (green)
        clr = (RED if pch >= 0 else GREEN) if is_vix else (GREEN if pch >= 0 else RED)
        ts_idx.add("TEXTCOLOR", (2, i), (2, i), clr)
        ts_idx.add("FONTNAME",  (2, i), (2, i), "Helvetica-Bold")
    t_idx.setStyle(ts_idx)
    story.append(t_idx)
    story.append(Spacer(1, 4*mm))

    # ── 2. Options Pulse ──────────────────────────────────────────────────────
    if oi_opinion:
        _section_header(
            "Options Pulse — Nifty",
            "PCR · Resistance · Support from weekly option chain",
            COBALT,
        )
        pcr      = round(oi_opinion.get("pcr", 0), 2)
        senti    = oi_opinion.get("sentiment", "-")
        resist   = oi_opinion.get("resistance", "-")
        support  = oi_opinion.get("support", "-")
        bias     = oi_opinion.get("writer_bias", "-")
        op_rows = [
            ["PCR", "Sentiment", "CE Resistance", "PE Support", "Writer Bias"],
            [str(pcr), senti, str(resist), str(support), bias],
        ]
        cw_op = [18*mm, 30*mm, 30*mm, 30*mm, 72*mm]
        t_op, ts_op = _simple_table(op_rows, cw_op, COBALT)
        pcr_clr = GREEN if pcr >= 1.2 else (RED if pcr <= 0.8 else AMBER)
        ts_op.add("TEXTCOLOR", (0, 1), (0, 1), pcr_clr)
        ts_op.add("FONTNAME",  (0, 1), (0, 1), "Helvetica-Bold")
        t_op.setStyle(ts_op)
        story.append(t_op)
        story.append(Spacer(1, 4*mm))

    # ── 3. AI Market Bias ─────────────────────────────────────────────────────
    if ai_plan:
        story.append(HRFlowable(width="100%", thickness=1.2, color=NAVY))
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph("AI Market Bias & Trade Plan",
                               sty("AIH", size=11, color=NAVY, bold=True, align=TA_CENTER, before=2, after=4)))
        bias_text = ai_plan.get("market_bias", "")
        levels    = ai_plan.get("key_levels", "")
        avoid     = ai_plan.get("avoid_today", "")
        if bias_text:
            story.append(Paragraph("MARKET BIAS",
                                   sty("MB_H", size=8, color=DARK_BLUE, bold=True, before=2, after=1)))
            story.append(Paragraph(bias_text,
                                   sty("MB_P", size=8.5, color=DARK_BLUE, align=TA_JUSTIFY, after=3)))
        if levels:
            story.append(Paragraph("KEY NIFTY LEVELS",
                                   sty("KL_H", size=8, color=COBALT, bold=True, before=2, after=1)))
            story.append(Paragraph(levels,
                                   sty("KL_P", size=8.5, color=DARK_BLUE, after=3)))
        if avoid:
            story.append(Paragraph("RISKS / AVOID TODAY",
                                   sty("AV_H", size=8, color=RED, bold=True, before=2, after=1)))
            story.append(Paragraph(avoid,
                                   sty("AV_P", size=8.5, color=DARK_BLUE, after=3)))
        story.append(Spacer(1, 2*mm))

    # ── 4. Yesterday's Runner Follow-through ──────────────────────────────────
    if prev_runners:
        lbl = f" — from {prev_runners_date}" if prev_runners_date else ""
        _section_header(
            f"Yesterday's Runner Candidates{lbl}",
            "Are they following through at today's open?",
            COBALT,
        )
        r_rows = [["Symbol", "Grade", "Today Chg %", "Open", "Status"]]
        for r in prev_runners[:15]:
            sym   = r.get("symbol", "")
            grade = r.get("grade", "")
            sd    = stock_map.get(sym, {})
            pch   = _safe_float(sd.get("pchange", 0))
            opn   = _safe_float(sd.get("open", 0))
            if pch >= 1.5:
                status = "Following Through ✓"
            elif pch >= 0:
                status = "Flat / Wait"
            elif pch >= -1.5:
                status = "Minor Pullback"
            else:
                status = "Failed — Gap Down"
            r_rows.append([
                sym, grade, f"{pch:+.2f}%",
                f"{opn:,.2f}" if opn else "-", status,
            ])
        cw_r = [38*mm, 22*mm, 22*mm, 26*mm, 72*mm]
        t_r, ts_r = _simple_table(r_rows, cw_r, COBALT)
        for i, r in enumerate(prev_runners[:15], 1):
            sym = r.get("symbol", "")
            pch = _safe_float(stock_map.get(sym, {}).get("pchange", 0))
            clr = GREEN if pch >= 1.5 else (RED if pch < -1.5 else AMBER)
            ts_r.add("TEXTCOLOR", (2, i), (2, i), clr)
            ts_r.add("FONTNAME",  (2, i), (2, i), "Helvetica-Bold")
            ts_r.add("TEXTCOLOR", (4, i), (4, i), clr)
        t_r.setStyle(ts_r)
        story.append(t_r)
        story.append(Spacer(1, 5*mm))

    # ── 5. Gap-Up Leaders ─────────────────────────────────────────────────────
    if gap_ups:
        _section_header(
            f"Gap-Up Leaders  ({len(gap_ups)} stocks)",
            "Nifty 500 — sorted by % move from previous close  |  potential momentum plays",
            GREEN,
        )
        gu_rows = [["Symbol", "Open", "LTP", "Chg %", "High", "Volume"]]
        for s in gap_ups:
            gu_rows.append([
                s.get("symbol", ""),
                f"{_safe_float(s.get('open', 0)):,.2f}",
                f"{_safe_float(s.get('close', 0)):,.2f}",
                f"{_safe_float(s.get('pchange', 0)):+.2f}%",
                f"{_safe_float(s.get('high', 0)):,.2f}",
                _fmt_vol(s.get("volume", 0)),
            ])
        cw_gu = [38*mm, 25*mm, 25*mm, 22*mm, 25*mm, 45*mm]
        t_gu, ts_gu = _simple_table(gu_rows, cw_gu, GREEN)
        for i in range(1, len(gu_rows)):
            ts_gu.add("TEXTCOLOR", (3, i), (3, i), GREEN)
            ts_gu.add("FONTNAME",  (3, i), (3, i), "Helvetica-Bold")
        t_gu.setStyle(ts_gu)
        story.append(t_gu)
        story.append(Spacer(1, 5*mm))

    # ── 6. Gap-Down Alerts ────────────────────────────────────────────────────
    if gap_downs:
        _section_header(
            f"Gap-Down Alerts  ({len(gap_downs)} stocks)",
            "Under selling pressure at open — handle with caution or avoid",
            RED,
        )
        gd_rows = [["Symbol", "Open", "LTP", "Chg %", "Low", "Volume"]]
        for s in gap_downs:
            gd_rows.append([
                s.get("symbol", ""),
                f"{_safe_float(s.get('open', 0)):,.2f}",
                f"{_safe_float(s.get('close', 0)):,.2f}",
                f"{_safe_float(s.get('pchange', 0)):+.2f}%",
                f"{_safe_float(s.get('low', 0)):,.2f}",
                _fmt_vol(s.get("volume", 0)),
            ])
        cw_gd = [38*mm, 25*mm, 25*mm, 22*mm, 25*mm, 45*mm]
        t_gd, ts_gd = _simple_table(gd_rows, cw_gd, RED)
        for i in range(1, len(gd_rows)):
            ts_gd.add("TEXTCOLOR", (3, i), (3, i), RED)
            ts_gd.add("FONTNAME",  (3, i), (3, i), "Helvetica-Bold")
        t_gd.setStyle(ts_gd)
        story.append(t_gd)
        story.append(Spacer(1, 5*mm))

    # ── 7. VWMA Retrace Setups ────────────────────────────────────────────────
    _section_header(
        f"VWMA(20) Daily Retrace Setups  ({len(vwma_hits)} stocks)",
        "Daily candle bouncing off VWMA(20) — swing entry candidates today",
        AMBER,
    )
    if vwma_hits:
        vw_rows = [["Symbol", "LTP", "VWMA", "% to VWMA", "Candle Pattern", "Signal"]]
        for s in vwma_hits:
            ltp  = _safe_float(s.get("ltp", 0))
            vwma = _safe_float(s.get("vwma", 0))
            to_v = round((ltp - vwma) / vwma * 100, 2) if vwma > 0 else 0
            pats = ", ".join(s.get("patterns", []))
            sig  = "BUY" if to_v <= 1 else ("Near" if to_v <= 3 else "Watch")
            vw_rows.append([
                s.get("symbol", ""),
                f"{ltp:,.2f}",
                f"{vwma:,.2f}",
                f"{to_v:+.2f}%",
                pats or "-",
                sig,
            ])
        cw_vw = [38*mm, 24*mm, 24*mm, 22*mm, 52*mm, 20*mm]
        t_vw, ts_vw = _simple_table(vw_rows, cw_vw, AMBER)
        for i, s in enumerate(vwma_hits, 1):
            ltp  = _safe_float(s.get("ltp", 0))
            vwma = _safe_float(s.get("vwma", 0))
            to_v = (ltp - vwma) / vwma * 100 if vwma > 0 else 0
            sig_clr = GREEN if to_v <= 1 else AMBER
            ts_vw.add("TEXTCOLOR", (5, i), (5, i), sig_clr)
            ts_vw.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
        t_vw.setStyle(ts_vw)
        story.append(t_vw)
    else:
        story.append(Paragraph(
            "No VWMA retrace setups found today — no daily VWMA(20) touch with reversal candle in Nifty 500.",
            sty("NVW", size=8, color=GRAY, italic=True, before=2, after=4),
        ))
    story.append(Spacer(1, 5*mm))

    # ── 8. AI Trade Plan — Top 5 for Today ───────────────────────────────────
    top_trades = ai_plan.get("top_trades", []) if ai_plan else []
    if top_trades:
        _section_header(
            "AI Trade Plan — Top 5 Stocks for Today",
            "Entry · Target · Stop-Loss  (AI-generated insights, not financial advice)",
            NAVY,
        )
        _rsn = ParagraphStyle("RSN_PM", fontName="Helvetica", fontSize=7,
                              textColor=DARK_BLUE, leading=9.5)
        tp_rows = [["#", "Symbol", "Action", "Entry", "Target", "Stop", "Reason"]]
        action_bg  = {"BUY": colors.HexColor("#dcfce7"),
                      "WATCH": colors.HexColor("#fef9c3"),
                      "AVOID": colors.HexColor("#fee2e2")}
        action_clr = {"BUY": GREEN, "WATCH": AMBER, "AVOID": RED}
        for idx, trade in enumerate(top_trades[:5], 1):
            tp_rows.append([
                str(idx),
                trade.get("symbol", ""),
                trade.get("action", ""),
                trade.get("entry", "-"),
                trade.get("target", "-"),
                trade.get("stop", "-"),
                Paragraph(trade.get("reason", ""), _rsn),
            ])
        cw_tp = [7*mm, 26*mm, 16*mm, 20*mm, 20*mm, 20*mm, 71*mm]
        t_tp = Table(tp_rows, colWidths=cw_tp)
        ts_tp = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (6, 0), (6, -1), "LEFT"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("VALIGN",        (6, 0), (6, -1), "TOP"),
            ("FONTSIZE",      (0, 1), (5, -1), 7.5),
            ("FONTNAME",      (0, 1), (5, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ])
        for ii, trade in enumerate(top_trades[:5], 1):
            act = trade.get("action", "")
            bg  = action_bg.get(act, colors.white)
            clr = action_clr.get(act, GRAY)
            ts_tp.add("BACKGROUND", (0, ii), (-1, ii), bg)
            ts_tp.add("TEXTCOLOR",  (2, ii), (2, ii), clr)
            ts_tp.add("FONTNAME",   (2, ii), (2, ii), "Helvetica-Bold")
        t_tp.setStyle(ts_tp)
        story.append(t_tp)
        story.append(Spacer(1, 5*mm))

    # ── 9. Portfolio Morning Check ────────────────────────────────────────────
    if portfolio:
        _section_header(
            "Portfolio Morning Check",
            f"P&L at {time_str} IST prices",
            DARK_BLUE,
        )
        pf_rows = [["Symbol", "Qty", "Avg Cost", "LTP", "Invested", "Current", "P&L", "P&L %"]]
        total_inv = sum(_safe_float(p.get("invested", 0)) for p in portfolio)
        total_cur = sum(_safe_float(p.get("current", 0)) for p in portfolio)
        total_pnl = total_cur - total_inv
        total_pct = (total_pnl / total_inv * 100) if total_inv > 0 else 0
        for p in portfolio:
            pnl     = _safe_float(p.get("pnl", 0))
            pnl_pct = _safe_float(p.get("pnl_pct", 0))
            pf_rows.append([
                p.get("symbol", ""),
                str(int(_safe_float(p.get("qty", 0)))),
                f"{_safe_float(p.get('avg_price', 0)):,.2f}",
                f"{_safe_float(p.get('ltp', 0)):,.2f}",
                f"Rs{_safe_float(p.get('invested', 0)):,.0f}",
                f"Rs{_safe_float(p.get('current', 0)):,.0f}",
                f"{'+'if pnl>=0 else ''}Rs{pnl:,.0f}",
                f"{'+'if pnl_pct>=0 else ''}{pnl_pct:.2f}%",
            ])
        pnl_sign  = "+" if total_pnl >= 0 else ""
        pct_sign  = "+" if total_pct >= 0 else ""
        pf_rows.append([
            "TOTAL", "", "", "",
            f"Rs{total_inv:,.0f}", f"Rs{total_cur:,.0f}",
            f"{pnl_sign}Rs{total_pnl:,.0f}",
            f"{pct_sign}{total_pct:.2f}%",
        ])
        cw_pf = [30*mm, 12*mm, 22*mm, 22*mm, 24*mm, 24*mm, 24*mm, 22*mm]
        t_pf, ts_pf = _simple_table(pf_rows, cw_pf, DARK_BLUE)
        for i, p in enumerate(portfolio, 1):
            pnl_pct = _safe_float(p.get("pnl_pct", 0))
            clr = GREEN if pnl_pct >= 0 else RED
            ts_pf.add("TEXTCOLOR", (6, i), (7, i), clr)
            ts_pf.add("FONTNAME",  (6, i), (7, i), "Helvetica-Bold")
        total_i = len(pf_rows) - 1
        ts_pf.add("BACKGROUND", (0, total_i), (-1, total_i), DARK_BLUE)
        ts_pf.add("TEXTCOLOR",  (0, total_i), (-1, total_i), colors.white)
        ts_pf.add("FONTNAME",   (0, total_i), (-1, total_i), "Helvetica-Bold")
        ts_pf.add("TEXTCOLOR",  (6, total_i), (7, total_i), GOLD)
        t_pf.setStyle(ts_pf)
        story.append(t_pf)

    doc.build(story)
    return buf.getvalue()


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def pre_market_scan_and_send() -> None:
    """Run all 9:30 AM pre-market scans, build PDF, send via Telegram."""
    from services.nse_service import (
        get_nifty500_ohlc, get_index_quotes, get_nifty_oi_analysis,
    )
    from services.vwma_retrace_service import scan_vwma_retraces
    from services.telegram_service import send_document
    from services.portfolio_service import get_portfolio_with_ltp

    logger.info("Pre-Market Report: starting 9:30 AM scan")
    now_ist  = datetime.now(_IST)
    date_str = now_ist.strftime("%d %b %Y")
    time_str = now_ist.strftime("%H:%M")
    prev_runners_date, prev_runners = _load_prev_runners()

    try:
        stocks, indices, nifty_oi = await asyncio.gather(
            get_nifty500_ohlc(),
            get_index_quotes(),
            get_nifty_oi_analysis("NIFTY"),
        )
        stocks = stocks or []

        vwma_hits = []
        try:
            vwma_hits = await scan_vwma_retraces(stocks)
            logger.info("Pre-market VWMA scan: %d hits", len(vwma_hits))
        except Exception as e:
            logger.warning("Pre-market VWMA scan failed: %s", e)

        portfolio = []
        try:
            portfolio = await get_portfolio_with_ltp()
        except Exception as e:
            logger.warning("Pre-market portfolio fetch failed: %s", e)

        gap_ups, gap_downs = _get_gap_leaders(stocks, 15)
        stock_map = {s["symbol"]: s for s in stocks}

        nifty_data  = (indices or {}).get("nifty50",   {})
        bnifty_data = (indices or {}).get("banknifty", {})
        vix_data    = (indices or {}).get("indiavix",  {})
        oi_opinion  = (nifty_oi or {}).get("oi_opinion", {})

        ai_plan = await asyncio.to_thread(
            _generate_premarket_ai,
            gap_ups, vwma_hits, prev_runners, nifty_data, oi_opinion, portfolio,
        )

        pdf_bytes = _build_premarket_pdf(
            date_str, time_str,
            gap_ups, gap_downs,
            vwma_hits,
            prev_runners, prev_runners_date,
            stock_map,
            nifty_data, bnifty_data, vix_data,
            oi_opinion,
            portfolio,
            ai_plan,
        )

        filename = f"RRE_PreMarket_{now_ist.strftime('%Y%m%d_%H%M')}.pdf"
        caption  = (
            f"RRE Pre-Market Analysis | {date_str} | {time_str} IST\n"
            f"Gap-Up: {len(gap_ups)} | Gap-Down: {len(gap_downs)} | VWMA Setups: {len(vwma_hits)} | Runners: {len(prev_runners)}"
        )
        await send_document(pdf_bytes, filename=filename, caption=caption)
        logger.info("Pre-Market Report sent: %s", filename)

    except Exception as exc:
        logger.error("Pre-market report failed: %s", exc, exc_info=True)
        try:
            from services.telegram_service import send_message
            await send_message(f"Pre-Market Report failed: {exc}")
        except Exception:
            pass
