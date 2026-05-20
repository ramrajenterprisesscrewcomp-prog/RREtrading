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
import json
import logging
import pathlib
from datetime import datetime

logger = logging.getLogger(__name__)

_DATA_DIR      = pathlib.Path(__file__).parent.parent / "data"
_RUNNERS_CACHE = _DATA_DIR / "prev_runners.json"


def _save_runners(runners: list[dict], date_str: str) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    try:
        _RUNNERS_CACHE.write_text(
            json.dumps({"date": date_str, "runners": runners}, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not save runners cache: %s", exc)
    try:
        from services.supabase_service import save_runners_db
        save_runners_db(runners, date_str, report_type="eod")
    except Exception as exc:
        logger.warning("Supabase save_runners failed: %s", exc)


def _load_prev_runners() -> tuple[str, list[dict]]:
    """Return (date_str, runners) from Supabase (primary) or local JSON (fallback)."""
    try:
        from services.supabase_service import load_runners_db
        result = load_runners_db()
        if result is not None:
            return result
    except Exception as exc:
        logger.warning("Supabase load_runners failed: %s", exc)
    try:
        if _RUNNERS_CACHE.exists():
            data = json.loads(_RUNNERS_CACHE.read_text(encoding="utf-8"))
            return data.get("date", ""), data.get("runners", [])
    except Exception as exc:
        logger.warning("Could not load runners cache: %s", exc)
    return "", []


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


# ── AI Gainer Analysis ────────────────────────────────────────────────────────

def _generate_gainer_analysis(gainers: list[dict]) -> dict:
    """
    Batch GPT-4o-mini call: returns
      {"reasons": {symbol: "30-word reason"}, "early_signal_insight": "100-word text"}
    """
    if not gainers:
        return {"reasons": {}, "early_signal_insight": ""}
    try:
        from services.openai_service import _get_client, _is_ai_hours, _under_limit, _record_cost
        if not _is_ai_hours() or not _under_limit():
            return {"reasons": {}, "early_signal_insight": ""}

        lines = []
        for s in gainers[:12]:
            sym  = s.get("symbol", "")
            pch  = _safe_float(s.get("pchange", 0))
            vol  = _fmt_vol(s.get("volume", 0))
            high = _safe_float(s.get("high", 0))
            low  = _safe_float(s.get("low", 0))
            cls  = _safe_float(s.get("close", 0))
            lines.append(f"  {sym}: +{pch:.2f}%  Vol:{vol}  H:{high}  L:{low}  C:{cls}")

        prompt = (
            "You are an Indian stock market analyst. Today's top gainers (Nifty 500):\n"
            + "\n".join(lines)
            + "\n\n"
            "Task 1 — For EACH stock above, write exactly 30 words explaining WHY it gained today "
            "(news catalyst, sector move, technical breakout, FII/DII action, earnings, etc.). "
            "Be specific — no generic phrases.\n\n"
            "Task 2 — Write 100 words total titled EARLY SIGNAL PLAYBOOK: "
            "How could a trader have spotted these winners BEFORE today's open? "
            "Mention pre-market cues, overnight gaps, volume patterns, or sector signals.\n\n"
            "Respond ONLY in this exact JSON format with no markdown:\n"
            '{"reasons": {"SYMBOL1": "30 words...", "SYMBOL2": "30 words..."}, '
            '"early_signal_insight": "100 words..."}'
        )

        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        _record_cost(resp.usage)
        import json
        data = json.loads(resp.choices[0].message.content)
        return {
            "reasons":              data.get("reasons", {}),
            "early_signal_insight": data.get("early_signal_insight", ""),
        }
    except Exception as exc:
        logger.warning("Gainer analysis AI failed: %s", exc)
        return {"reasons": {}, "early_signal_insight": ""}


# ── Nifty Day Intelligence AI ────────────────────────────────────────────────

def _generate_nifty_intelligence(
    nifty:   dict,
    bnifty:  dict,
    vix:     dict,
    oi_opin: dict,
    strikes: list[dict],
    news:    list[dict],
) -> dict:
    """
    GPT-4o-mini: narrative on Nifty day, options pricing, key news, tomorrow's levels.
    Returns {"narrative", "vix_read", "options_read", "news_summary", "tomorrow_levels"}
    """
    empty = {"narrative": "", "vix_read": "", "options_read": "", "news_summary": "", "tomorrow_levels": ""}
    if not nifty:
        return empty
    try:
        from services.openai_service import _get_client, _is_ai_hours, _under_limit, _record_cost
        if not _is_ai_hours() or not _under_limit():
            return empty

        def _f(d, k, default=0):
            try: return float(str(d.get(k, default) or default).replace(",", ""))
            except: return default

        n_o  = _f(nifty, "open");  n_h = _f(nifty, "high")
        n_l  = _f(nifty, "low");   n_c = _f(nifty, "value")
        n_ch = _f(nifty, "pchange")
        n_rng = round(n_h - n_l, 0)
        n_pos = round((n_c - n_l) / max(n_h - n_l, 1) * 100, 0)

        vix_v  = _f(vix, "value");   vix_ch = _f(vix, "pchange")
        bn_c   = _f(bnifty, "value"); bn_ch  = _f(bnifty, "pchange")

        pcr     = oi_opin.get("pcr", 0)
        senti   = oi_opin.get("sentiment", "")
        resist  = oi_opin.get("resistance", "")
        support = oi_opin.get("support", "")
        writer  = oi_opin.get("writer_bias", "")
        fr_res  = oi_opin.get("fresh_resistance", "")
        fr_sup  = oi_opin.get("fresh_support", "")

        # Top 5 strikes by total OI
        top_str = sorted(strikes or [], key=lambda x: x.get("ce_oi", 0) + x.get("pe_oi", 0), reverse=True)[:5]
        strikes_txt = "  ".join(
            f"{s['strike']}(CE:{s['ce_oi']//1000}K,PE:{s['pe_oi']//1000}K)"
            for s in top_str
        ) if top_str else "N/A"

        news_lines = "\n".join(
            f"  • {n.get('title','')}" for n in (news or [])[:8] if n.get("title")
        ) or "  (no news fetched)"

        prompt = (
            f"You are an expert Indian market analyst. Today is {datetime.now().strftime('%d %b %Y')} (market closed at 3:30 PM IST).\n\n"
            f"NIFTY 50: O={n_o:,.0f}  H={n_h:,.0f}  L={n_l:,.0f}  C={n_c:,.0f}  Chg={n_ch:+.2f}%  "
            f"Range={n_rng:.0f}pts  ClosePos={n_pos:.0f}% of range\n"
            f"BANK NIFTY: C={bn_c:,.0f}  Chg={bn_ch:+.2f}%\n"
            f"INDIA VIX: {vix_v:.2f}  Chg={vix_ch:+.2f}%\n\n"
            f"NIFTY OPTIONS (weekly expiry):\n"
            f"  PCR={pcr}  Sentiment={senti}  WriterBias={writer}\n"
            f"  MaxCE(Resistance)={resist}  MaxPE(Support)={support}\n"
            f"  FreshResistance={fr_res}  FreshSupport={fr_sup}\n"
            f"  TopStrikes(OI): {strikes_txt}\n\n"
            f"TODAY'S MARKET NEWS:\n{news_lines}\n\n"
            "Write concise analysis in this exact JSON (no markdown):\n"
            '{"narrative": "3 sentences: what drove Nifty today, breadth, key observation",'
            '"vix_read": "1 sentence: what VIX level means for market fear/confidence",'
            '"options_read": "2 sentences: what options OI and PCR signal for tomorrow, key levels to watch",'
            '"news_summary": "2 sentences: most important news and its market impact",'
            '"tomorrow_levels": "Key support and resistance for tomorrow with specific Nifty levels"}'
        )

        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        _record_cost(resp.usage)
        import json as _json
        return _json.loads(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("Nifty intelligence AI failed: %s", exc)
        return empty


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
        from services.openai_service import _get_client, _under_limit, _record_cost
        if not _under_limit():
            return ""

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
        _record_cost(resp.usage)
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
    date_str:             str,
    gainers:              list[dict],
    losers:               list[dict],
    long_buildup:         list[dict],
    runners:              list[dict],
    watchlist:            list[dict],
    delivery:             list[dict],
    sector_data:          dict,
    ai_text:              str,
    vwma_hits:            list[dict] | None = None,
    gainer_reasons:       dict | None = None,
    early_signal_insight: str = "",
    prev_runners:         list[dict] | None = None,
    prev_runners_date:    str = "",
    all_stocks:           list[dict] | None = None,
    retrace_hits:         list[dict] | None = None,
    portfolio_data:       list[dict] | None = None,
    portfolio_analysis:   dict | None = None,
    nifty_intel:          dict | None = None,
    nifty_data:           dict | None = None,
    bnifty_data:          dict | None = None,
    vix_data:             dict | None = None,
    oi_opinion:           dict | None = None,
    weekly_strikes:       list[dict] | None = None,
    market_news:          list[dict] | None = None,
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

    # ── 0. Nifty Day Intelligence ─────────────────────────────────────────────
    NAVY  = colors.HexColor("#1e3a5f")
    SLATE = colors.HexColor("#334155")

    def _fv(d, k, default=0.0):
        try: return float(str((d or {}).get(k, default) or default).replace(",", ""))
        except: return default

    # Always render this section (shows "—" if no data)
    _section_header(
        "Nifty Day Intelligence  —  Options & Market Pulse",
        f"EOD snapshot  |  {date_str}  |  Market closed 3:30 PM IST",
        NAVY,
    )

    # ── Row 1: Index Day Summary ───────────────────────────────────────────
    n   = nifty_data  or {}
    bn  = bnifty_data or {}
    vix = vix_data    or {}

    n_o  = _fv(n, "open");  n_h = _fv(n, "high"); n_l = _fv(n, "low"); n_c = _fv(n, "value")
    n_ch = _fv(n, "pchange"); n_chabs = _fv(n, "change")
    n_rng = round(n_h - n_l, 0) if n_h and n_l else 0
    n_pos = round((n_c - n_l) / max(n_h - n_l, 1) * 100, 0) if n_h != n_l else 0

    idx_rows = [["Index", "Open", "High", "Low", "Close", "Chg %", "Range", "Close pos"]]
    idx_rows.append([
        "NIFTY 50",
        f"{n_o:,.0f}" if n_o else "—", f"{n_h:,.0f}" if n_h else "—",
        f"{n_l:,.0f}" if n_l else "—", f"{n_c:,.0f}" if n_c else "—",
        f"{n_ch:+.2f}%" if n_ch else "—",
        f"{n_rng:.0f} pts" if n_rng else "—",
        f"{n_pos:.0f}% of range" if n_pos else "—",
    ])
    bn_c = _fv(bn, "value"); bn_ch = _fv(bn, "pchange")
    bn_h = _fv(bn, "high");  bn_l  = _fv(bn, "low"); bn_o = _fv(bn, "open")
    idx_rows.append([
        "BANK NIFTY",
        f"{bn_o:,.0f}" if bn_o else "—", f"{bn_h:,.0f}" if bn_h else "—",
        f"{bn_l:,.0f}" if bn_l else "—", f"{bn_c:,.0f}" if bn_c else "—",
        f"{bn_ch:+.2f}%" if bn_ch else "—", "—", "—",
    ])
    vix_v = _fv(vix, "value"); vix_ch = _fv(vix, "pchange")
    idx_rows.append([
        "INDIA VIX",
        "—", "—", "—", f"{vix_v:.2f}" if vix_v else "—",
        f"{vix_ch:+.2f}%" if vix_ch else "—", "—",
        "Fear↑" if vix_ch > 0 else ("Fear↓" if vix_ch < 0 else "—"),
    ])

    cw_idx = [28*mm, 22*mm, 22*mm, 22*mm, 22*mm, 18*mm, 18*mm, 28*mm]
    t_idx, ts_idx = _simple_table(idx_rows, cw_idx, NAVY)
    ts_idx.add("ALIGN", (1, 1), (-1, -1), "RIGHT")
    ts_idx.add("ALIGN", (0, 1), (0, -1), "LEFT")
    for i, row in enumerate(idx_rows[1:], 1):
        pch_val = _fv(n if i == 1 else (bn if i == 2 else vix), "pchange")
        clr = GREEN if pch_val >= 0 else RED
        ts_idx.add("TEXTCOLOR", (5, i), (5, i), clr)
        ts_idx.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
    t_idx.setStyle(ts_idx)
    story.append(t_idx)
    story.append(Spacer(1, 4*mm))

    # ── Row 2: Options Snapshot ────────────────────────────────────────────
    opin = oi_opinion or {}
    pcr      = opin.get("pcr", 0)
    senti    = opin.get("sentiment", "—")
    resist   = opin.get("resistance", "—")
    support_lvl = opin.get("support", "—")
    writer   = opin.get("writer_bias", "—")
    fr_res   = opin.get("fresh_resistance") or "—"
    fr_sup   = opin.get("fresh_support")    or "—"

    SENTI_CLR = {
        "Bullish": GREEN, "Mildly Bullish": colors.HexColor("#16a34a"),
        "Bearish": RED,   "Mildly Bearish": colors.HexColor("#dc2626"),
        "Neutral": GRAY,
    }
    senti_clr = SENTI_CLR.get(senti, GRAY)

    opt_rows = [["PCR", "Sentiment", "Max CE (Resistance)", "Max PE (Support)", "Fresh Resistance", "Fresh Support", "Writer Bias"]]
    opt_rows.append([
        f"{pcr:.2f}" if pcr else "—", senti,
        str(resist), str(support_lvl), str(fr_res), str(fr_sup), writer,
    ])
    cw_opt = [16*mm, 24*mm, 26*mm, 26*mm, 22*mm, 22*mm, 0]
    cw_opt[-1] = 160*mm - sum(cw_opt[:-1])
    t_opt, ts_opt = _simple_table(opt_rows, cw_opt, SLATE)
    ts_opt.add("TEXTCOLOR", (1, 1), (1, 1), senti_clr)
    ts_opt.add("FONTNAME",  (1, 1), (1, 1), "Helvetica-Bold")
    ts_opt.add("TEXTCOLOR", (2, 1), (2, 1), RED)    # resistance = danger
    ts_opt.add("TEXTCOLOR", (3, 1), (3, 1), GREEN)  # support = floor
    ts_opt.add("FONTNAME",  (2, 1), (3, 1), "Helvetica-Bold")
    t_opt.setStyle(ts_opt)
    story.append(t_opt)
    story.append(Spacer(1, 3*mm))

    # ── Row 3: Top CE/PE strikes table ────────────────────────────────────
    underlying = n_c  # Nifty spot price for Support/Resistance classification
    strikes = weekly_strikes or []
    if strikes:
        top_s = sorted(strikes, key=lambda x: x.get("ce_oi", 0) + x.get("pe_oi", 0), reverse=True)[:8]
        def _ok(n): return f"{n//1000}K" if abs(n) >= 1000 else str(n)

        # Only show change columns when intraday OI change data is available
        has_chg = any(s.get("ce_chg", 0) != 0 or s.get("pe_chg", 0) != 0 for s in top_s)

        if has_chg:
            str_hdr = ["Strike", "CE OI", "CE Chg", "PE OI", "PE Chg", "Net (PE-CE)"]
            cw_str  = [22*mm, 22*mm, 22*mm, 22*mm, 22*mm, 26*mm]
        else:
            str_hdr = ["Strike", "CE OI (total)", "PE OI (total)", "Net PE-CE", "Position"]
            cw_str  = [22*mm, 30*mm, 30*mm, 28*mm, 50*mm]

        str_rows = [str_hdr]
        for s in top_s:
            ce_oi = int(s.get("ce_oi", 0))
            pe_oi = int(s.get("pe_oi", 0))
            net   = pe_oi - ce_oi
            strike_f = float(s.get("strike", 0))
            pos = "Resistance" if strike_f >= (underlying or 0) else "Support"
            if has_chg:
                ce_chg = int(s.get("ce_chg", 0))
                pe_chg = int(s.get("pe_chg", 0))
                str_rows.append([
                    str(s.get("strike", "")),
                    _ok(ce_oi), f"{'+'if ce_chg>=0 else ''}{_ok(ce_chg)}",
                    _ok(pe_oi), f"{'+'if pe_chg>=0 else ''}{_ok(pe_chg)}",
                    f"{'+'if net>=0 else ''}{_ok(net)}",
                ])
            else:
                str_rows.append([
                    str(s.get("strike", "")),
                    _ok(ce_oi), _ok(pe_oi),
                    f"{'+'if net>=0 else ''}{_ok(net)}",
                    pos,
                ])

        t_str, ts_str = _simple_table(str_rows, cw_str, SLATE)
        ts_str.add("ALIGN", (1, 1), (-1, -1), "RIGHT")
        for i, s in enumerate(top_s, 1):
            net = int(s.get("pe_oi", 0)) - int(s.get("ce_oi", 0))
            if has_chg:
                ce_chg = int(s.get("ce_chg", 0))
                pe_chg = int(s.get("pe_chg", 0))
                ts_str.add("TEXTCOLOR", (2, i), (2, i), RED   if ce_chg > 0 else GREEN)
                ts_str.add("TEXTCOLOR", (4, i), (4, i), GREEN if pe_chg > 0 else RED)
                ts_str.add("TEXTCOLOR", (5, i), (5, i), GREEN if net > 0 else RED)
                ts_str.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
            else:
                ts_str.add("TEXTCOLOR", (3, i), (3, i), GREEN if net > 0 else RED)
                ts_str.add("FONTNAME",  (3, i), (3, i), "Helvetica-Bold")
            # Highlight resistance (red tint) and support (green tint) rows
            if str(s.get("strike")) == str(resist):
                ts_str.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fef2f2"))
            if str(s.get("strike")) == str(support_lvl):
                ts_str.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f0fdf4"))
        t_str.setStyle(ts_str)
        lbl = "Weekly Expiry — Top Strikes by OI  (EOD data · intraday changes unavailable)" if not has_chg else \
              "Weekly Expiry — Top Strikes by OI  (live intraday)"
        story.append(Paragraph(lbl, sty("STR_H", size=7.5, color=SLATE, bold=True, before=2, after=2)))
        story.append(t_str)
        story.append(Spacer(1, 3*mm))

    # ── Row 4: AI narrative ────────────────────────────────────────────────
    intel = nifty_intel or {}
    ai_parts = [
        ("Market Narrative", intel.get("narrative", "")),
        ("India VIX Signal", intel.get("vix_read", "")),
        ("Options Outlook for Tomorrow", intel.get("options_read", "")),
        ("Tomorrow's Key Levels", intel.get("tomorrow_levels", "")),
    ]

    for label, text in ai_parts:
        if text:
            story.append(Paragraph(
                label.upper(),
                sty(f"NI_{label[:4]}", size=8, color=NAVY, bold=True, before=3, after=1),
            ))
            story.append(Paragraph(
                text,
                sty(f"NI_P{label[:4]}", size=8.5, color=DARK_BLUE, before=0, after=3, align=TA_JUSTIFY),
            ))
    story.append(Spacer(1, 6*mm))

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
        story.append(Spacer(1, 4*mm))

        # ── 2b. Gainer Insights (AI 30-word reasons) ──────────────────────────
        if gainer_reasons:
            _section_header(
                "Gainer Insights -- AI Analysis",
                "Why each stock moved today",
                GREEN,
            )
            _rsn = ParagraphStyle("RSN", fontName="Helvetica", fontSize=7,
                                  textColor=DARK_BLUE, leading=9)
            gi_rows = [["#", "Symbol", "Gain%", "Why It Moved Today (AI)"]]
            cw_gi   = [7*mm, 30*mm, 18*mm, 125*mm]
            for idx, s in enumerate(gainers[:12], 1):
                sym    = s.get("symbol", "")
                reason = (gainer_reasons or {}).get(sym, "")
                if not reason:
                    continue
                pch = _safe_float(s.get("pchange", 0))
                gi_rows.append([str(idx), sym, f"+{pch:.2f}%",
                                Paragraph(reason, _rsn)])
            if len(gi_rows) > 1:
                t_gi = Table(gi_rows, colWidths=cw_gi)
                ts_gi = TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, 0), GREEN),
                    ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
                    ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
                    ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                    ("ALIGN",         (1, 1), (1, -1), "LEFT"),
                    ("ALIGN",         (3, 0), (3, -1), "LEFT"),
                    ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                    ("FONTSIZE",      (0, 1), (2, -1), 7),
                    ("FONTNAME",      (0, 1), (2, -1), "Helvetica"),
                    ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
                    ("TOPPADDING",    (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ])
                for ii in range(1, len(gi_rows)):
                    ts_gi.add("BACKGROUND", (0, ii), (-1, ii),
                              ALT_ROW if ii % 2 == 0 else colors.white)
                    ts_gi.add("TEXTCOLOR", (2, ii), (2, ii), GREEN)
                    ts_gi.add("FONTNAME",  (2, ii), (2, ii), "Helvetica-Bold")
                t_gi.setStyle(ts_gi)
                story.append(t_gi)
            story.append(Spacer(1, 3*mm))

        # ── 2c. Early Signal Playbook ─────────────────────────────────────────
        if early_signal_insight:
            story.append(Paragraph(
                "EARLY SIGNAL PLAYBOOK -- How to Spot Tomorrow's Winners Today",
                sty("ESP_H", size=9, color=GREEN, bold=True, before=2, after=2),
            ))
            story.append(Paragraph(
                early_signal_insight,
                sty("ESP_P", size=8.5, color=DARK_BLUE, before=0, after=4, align=TA_JUSTIFY),
            ))
        story.append(Spacer(1, 4*mm))

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

    # ── 6b. Yesterday's Runner Candidates vs Today's Performance ─────────────
    if prev_runners:
        # Use all 500 stocks so runners outside top-15 get correct pchange
        _pch_universe = all_stocks if all_stocks else (gainers + losers)
        today_pch: dict[str, float] = {
            s.get("symbol", ""): _safe_float(s.get("pchange", 0))
            for s in _pch_universe
            if s.get("symbol")
        }
        gainer_syms = {s.get("symbol", "") for s in gainers}
        GOLD2       = colors.HexColor("#b45309")
        label       = f"  (recommended {prev_runners_date})" if prev_runners_date else ""
        _section_header(
            f"Yesterday's Runner Candidates vs Today's Performance  ({len(prev_runners)} runners{label})",
            "Stocks flagged as runners yesterday -- how did they actually perform today?",
            GOLD2,
        )
        rc_rows = [["#", "Symbol", "Grade", "Yesterday Score", "Today Chg%", "Result"]]
        cw_rc   = [8*mm, 30*mm, 22*mm, 30*mm, 22*mm, 41*mm]
        runners_sorted = sorted(
            prev_runners,
            key=lambda r: -today_pch.get(r.get("symbol", ""), 0.0),
        )
        for rank, r in enumerate(runners_sorted[:20], 1):
            sym    = r.get("symbol", "")
            pch    = today_pch.get(sym, 0.0)
            grade  = r.get("grade", "WATCH")
            score  = r.get("score", "")
            result = "TOP GAINER" if sym in gainer_syms else ("Positive" if pch > 0 else "Missed")
            rc_rows.append([str(rank), sym, grade, str(score), f"{pch:+.2f}%", result])
        t_rc = Table(rc_rows, colWidths=cw_rc)
        ts_rc = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), GOLD2),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (1, 1), (1, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        GRADE_CLR2 = {"HIGH": GREEN, "STRONG": COBALT, "WATCH": AMBER}
        for i, r in enumerate(runners_sorted[:20], 1):
            sym   = r.get("symbol", "")
            pch   = today_pch.get(sym, 0.0)
            grade = r.get("grade", "WATCH")
            ts_rc.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            ts_rc.add("TEXTCOLOR",  (2, i), (2, i), GRADE_CLR2.get(grade, GRAY))
            ts_rc.add("FONTNAME",   (2, i), (2, i), "Helvetica-Bold")
            if sym in gainer_syms:
                ts_rc.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fef9c3"))
                ts_rc.add("TEXTCOLOR",  (5, i), (5, i), GOLD2)
                ts_rc.add("FONTNAME",   (5, i), (5, i), "Helvetica-Bold")
            elif pch > 0:
                ts_rc.add("TEXTCOLOR",  (4, i), (4, i), GREEN)
                ts_rc.add("TEXTCOLOR",  (5, i), (5, i), GREEN)
            else:
                ts_rc.add("TEXTCOLOR",  (4, i), (4, i), RED)
                ts_rc.add("TEXTCOLOR",  (5, i), (5, i), RED)
        t_rc.setStyle(ts_rc)
        story.append(t_rc)
        story.append(Spacer(1, 3*mm))
        hit_count = sum(1 for r in prev_runners if r.get("symbol", "") in gainer_syms)
        story.append(Paragraph(
            f"Yesterday's runner accuracy: <b>{hit_count}</b> of <b>{len(prev_runners)}</b> became today's top gainers  "
            f"({hit_count * 100 // len(prev_runners) if prev_runners else 0}% hit rate)  |  "
            "Yellow rows = recommended yesterday and also a top gainer today",
            sty("RC_NOTE", size=7, color=GRAY, align=TA_CENTER, italic=True),
        ))
        story.append(Spacer(1, 6*mm))

    # ── 6c. VWMA(20) Daily Retrace Setups ────────────────────────────────────
    TEAL3 = colors.HexColor("#0d9488")
    _section_header(
        f"VWMA(20) Daily Retrace Setups  ({len(retrace_hits or [])} stocks)  --  Daily Timeframe",
        "Low touched VWMA(20), prev 2 days closed above, reversal candle (Hammer / Doji / Pin Bar / Engulfing)",
        TEAL3,
    )
    if not retrace_hits:
        story.append(Paragraph(
            "No VWMA(20) retrace setups found at EOD scan.",
            sty("NO_R", size=8, color=GRAY, align=TA_CENTER, italic=True),
        ))
    else:
        rt_hdr = ["#", "Symbol", "Pattern", "LTP", "VWMA(20)", "Touch%", "Body%", "Wick%"]
        cw_rt  = [7*mm, 25*mm, 42*mm, 20*mm, 20*mm, 18*mm, 15*mm, 13*mm]
        rt_rows = [rt_hdr]
        for i, h in enumerate(retrace_hits[:25], 1):
            pat = " | ".join(h.get("patterns", []))
            rt_rows.append([
                str(i), h["symbol"],
                pat,
                f"{h['ltp']:,.2f}",
                f"{h['vwma']:,.2f}",
                f"{h.get('touch_pct', 0):+.2f}%",
                f"{h.get('body_pct', 0):.1f}%",
                f"{h.get('wick_pct', 0):.1f}%",
            ])
        t_rt, ts_rt = _simple_table(rt_rows, cw_rt, TEAL3)
        ts_rt.add("ALIGN", (1, 1), (2, -1), "LEFT")
        for i, h in enumerate(retrace_hits[:25], 1):
            ts_rt.add("TEXTCOLOR", (2, i), (2, i), TEAL3)
            ts_rt.add("FONTNAME",  (2, i), (2, i), "Helvetica-Bold")
            pch_clr = GREEN if h.get("pchange", 0) >= 0 else RED
            ts_rt.add("TEXTCOLOR", (5, i), (5, i), AMBER)
        t_rt.setStyle(ts_rt)
        story.append(t_rt)
    story.append(Spacer(1, 6*mm))

    # ── 7. Reversal + Engulfing Setup ─────────────────────────────────────────
    if vwma_hits:
        TEAL2 = colors.HexColor("#0891b2")
        _section_header(
            f"Doji / Hammer / Pin Bar + Bullish Engulfing  ({len(vwma_hits)} stocks)",
            "Day N-1: Reversal candle  |  Day N: Bullish Engulfing  --  Nifty 500 daily",
            TEAL2,
        )
        v_hdr = ["#", "Symbol", "Reversal (N-1)", "Engulfing (N)", "LTP", "Chg%", "Body%"]
        cw_v  = [7*mm, 24*mm, 46*mm, 46*mm, 20*mm, 15*mm, 13*mm]
        v_rows = [v_hdr]
        for i, h in enumerate(vwma_hits[:20], 1):
            pat = " | ".join(h.get("patterns", []))
            v_rows.append([
                str(i), h["symbol"],
                f"{pat}  O:{h.get('rev_o','')}  C:{h.get('rev_c','')}",
                f"Engulfing  O:{h.get('eng_o','')}  C:{h.get('eng_c','')}",
                f"{h['ltp']:,.2f}",
                f"{h['pchange']:+.2f}%",
                f"{h.get('body_pct', 0):.0f}%",
            ])
        t_v, ts_v = _simple_table(v_rows, cw_v, TEAL2)
        ts_v.add("ALIGN", (1, 1), (3, -1), "LEFT")
        for i, h in enumerate(vwma_hits[:20], 1):
            ts_v.add("TEXTCOLOR", (2, i), (2, i), AMBER)
            ts_v.add("FONTNAME",  (2, i), (2, i), "Helvetica-Bold")
            ts_v.add("TEXTCOLOR", (3, i), (3, i), GREEN)
            pch_clr = GREEN if h["pchange"] >= 0 else RED
            ts_v.add("TEXTCOLOR", (5, i), (5, i), pch_clr)
            ts_v.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
        t_v.setStyle(ts_v)
        story.append(t_v)
        story.append(Spacer(1, 6*mm))

    # ── 7b. My Portfolio Analysis ─────────────────────────────────────────────
    if portfolio_data:
        INDIGO = colors.HexColor("#4f46e5")
        _section_header(
            f"My Portfolio  ({len(portfolio_data)} stocks)  --  EOD P&L & AI Advice",
            "Your holdings vs today's close — AI-generated action recommendation",
            INDIGO,
        )
        total_inv = sum(h.get("invested", 0) for h in portfolio_data)
        total_cur = sum(h.get("current",  0) for h in portfolio_data if h.get("ltp", 0) > 0)
        total_pnl = total_cur - total_inv
        total_pct = total_pnl / total_inv * 100 if total_inv else 0
        pnl_clr   = GREEN if total_pnl >= 0 else RED

        story.append(Paragraph(
            f"Total Invested: <b>₹{total_inv:,.0f}</b>  |  "
            f"Current Value: <b>₹{total_cur:,.0f}</b>  |  "
            f"Overall P&L: <b>{'+'if total_pnl>=0 else ''}₹{total_pnl:,.0f}  "
            f"({total_pct:+.1f}%)</b>",
            sty("PF_SUM", size=9, color=pnl_clr, bold=True, align=TA_CENTER, before=2, after=4),
        ))

        _ai_rsn = ParagraphStyle("PF_RSN", fontName="Helvetica", fontSize=7,
                                  textColor=DARK_BLUE, leading=9)
        pf_hdr  = ["#", "Symbol", "Qty", "Avg Cost", "LTP", "P&L", "P&L %", "AI Suggestion"]
        cw_pf   = [7*mm, 22*mm, 12*mm, 20*mm, 20*mm, 22*mm, 16*mm, 0]
        cw_pf[-1] = 160*mm - sum(cw_pf[:-1])
        pf_rows = [pf_hdr]
        _ai_stocks = (portfolio_analysis or {}).get("stocks", {})
        for idx, h in enumerate(portfolio_data, 1):
            sym     = h["symbol"]
            pnl_h   = h.get("pnl", 0)
            pnlp_h  = h.get("pnl_pct", 0)
            ltp_h   = h.get("ltp", 0)
            advice  = _ai_stocks.get(sym, "—")
            pf_rows.append([
                str(idx), sym,
                str(int(h.get("qty", 0))),
                f"₹{h.get('avg_price', 0):,.2f}",
                f"₹{ltp_h:,.2f}" if ltp_h > 0 else "—",
                f"{'+'if pnl_h>=0 else ''}₹{pnl_h:,.0f}",
                f"{pnlp_h:+.1f}%",
                Paragraph(advice, _ai_rsn),
            ])

        t_pf = Table(pf_rows, colWidths=cw_pf)
        ts_pf = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), INDIGO),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (1, 1), (1, -1), "LEFT"),
            ("ALIGN",         (7, 0), (7, -1), "LEFT"),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE",      (0, 1), (6, -1), 7),
            ("FONTNAME",      (0, 1), (6, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
        for i, h in enumerate(portfolio_data, 1):
            ts_pf.add("BACKGROUND", (0, i), (-1, i), ALT_ROW if i % 2 == 0 else colors.white)
            pnl_h = h.get("pnl", 0)
            clr   = GREEN if pnl_h >= 0 else RED
            ts_pf.add("TEXTCOLOR", (5, i), (6, i), clr)
            ts_pf.add("FONTNAME",  (5, i), (6, i), "Helvetica-Bold")
        t_pf.setStyle(ts_pf)
        story.append(t_pf)

        # Overall portfolio AI advice paragraph
        overall = (portfolio_analysis or {}).get("overall", "")
        if overall:
            story.append(Spacer(1, 3*mm))
            story.append(Paragraph(
                "PORTFOLIO ASSESSMENT (AI)",
                sty("PF_AH", size=9, color=INDIGO, bold=True, before=2, after=2),
            ))
            story.append(Paragraph(
                overall,
                sty("PF_AP", size=8.5, color=DARK_BLUE, before=0, after=4, align=TA_JUSTIFY),
            ))
        story.append(Spacer(1, 6*mm))

    # ── 8. AI EOD Analysis ────────────────────────────────────────────────────
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
    from services.vwma_candle_service import scan_vwma_candle
    from services.vwma_retrace_service import scan_vwma_retraces
    from services.portfolio_service import (
        get_portfolio_with_ltp, generate_portfolio_analysis,
    )
    from services.nse_service import get_index_quotes, get_nifty_oi_analysis
    from services.news_service import get_policy_news

    logger.info("EOD Report: starting 4:00 PM scan")
    date_str = datetime.now().strftime("%d %b %Y")
    prev_runners_date, prev_runners = _load_prev_runners()

    try:
        stocks, buildup_raw, tsr_bu, runners, indices, nifty_oi, market_news = await asyncio.gather(
            get_nifty500_ohlc(),
            get_fno_oi_buildup(15),
            get_tsr_buildup(),
            scan_tomorrow_runners(),
            get_index_quotes(),
            get_nifty_oi_analysis("NIFTY"),
            get_policy_news("stock market Nifty BSE NSE", "India market"),
        )

        stocks  = stocks or []
        runners = runners or []

        # VWMA scans — isolated so a failure never blocks the main report
        vwma_hits    = []
        retrace_hits = []
        try:
            vwma_hits = await scan_vwma_candle()
            logger.info("EOD VWMA candle scan: %d hits", len(vwma_hits))
        except Exception as e:
            logger.warning("EOD VWMA candle scan failed: %s", e)
        try:
            retrace_hits = await scan_vwma_retraces(stocks or [])
            logger.info("EOD VWMA retrace scan: %d hits", len(retrace_hits))
        except Exception as e:
            logger.warning("EOD VWMA retrace scan failed: %s", e)

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

        # Sector data — multi returns pchange_1d, use short name for display
        sector_data = {}
        try:
            sec_raw = await get_sector_rotation_multi()
            for item in (sec_raw or []):
                name = item.get("short", item.get("name", ""))
                pct  = _safe_float(item.get("pchange_1d", item.get("pChange", item.get("pct", 0))))
                if name:
                    sector_data[name] = pct
        except Exception as e:
            logger.warning("Sector data failed: %s", e)

        gainers, losers = get_top_gainers_losers(stocks, 15)
        delivery        = get_high_delivery_stocks(stocks, 12)
        watchlist       = get_eod_watchlist(stocks, long_buildup, runners)

        # Extract Nifty / VIX / options data
        nifty_data    = (indices or {}).get("nifty50",    {})
        bnifty_data   = (indices or {}).get("banknifty",  {})
        vix_data      = (indices or {}).get("indiavix",   {})
        oi_opinion    = (nifty_oi or {}).get("oi_opinion", {})
        weekly_strikes = (nifty_oi or {}).get("weekly_strikes", [])

        # Load portfolio and fetch live LTPs in parallel with AI calls
        portfolio_with_ltp = await get_portfolio_with_ltp()

        sector_ctx = ", ".join(
            f"{k}: {v:+.1f}%" for k, v in list(sector_data.items())[:6]
        ) if sector_data else ""

        ai_text, gainer_analysis, portfolio_analysis, nifty_intel = await asyncio.gather(
            asyncio.to_thread(
                _generate_eod_ai, gainers, losers, long_buildup, runners, watchlist, sector_data
            ),
            asyncio.to_thread(_generate_gainer_analysis, gainers),
            asyncio.to_thread(generate_portfolio_analysis, portfolio_with_ltp, sector_ctx),
            asyncio.to_thread(
                _generate_nifty_intelligence,
                nifty_data, bnifty_data, vix_data, oi_opinion, weekly_strikes, market_news,
            ),
        )
        gainer_reasons       = gainer_analysis.get("reasons", {})
        early_signal_insight = gainer_analysis.get("early_signal_insight", "")

        pdf_bytes = await asyncio.to_thread(
            _build_eod_pdf,
            date_str, gainers, losers, long_buildup, runners,
            watchlist, delivery, sector_data, ai_text,
            vwma_hits or [], gainer_reasons, early_signal_insight,
            prev_runners, prev_runners_date,
            stocks or [],             # all_stocks — full 500 for runner pchange lookup
            retrace_hits or [],       # VWMA daily retrace hits
            portfolio_with_ltp or [], # My portfolio holdings with live LTP
            portfolio_analysis or {}, # AI advice per stock + overall
            nifty_intel or {},        # Nifty day intelligence AI
            nifty_data  or {},        # Nifty 50 OHLC
            bnifty_data or {},        # Bank Nifty OHLC
            vix_data    or {},        # India VIX
            oi_opinion  or {},        # Options PCR / sentiment / levels
            weekly_strikes or [],     # Top option chain strikes
            market_news or [],        # Today's market news
        )

        # Save today's runners so tomorrow's report can compare against them
        _save_runners(runners, date_str)

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
            f"VWMA(20) Combo Setups: {len(vwma_hits or [])} stocks  (Doji/Hammer/Pin Bar + Bullish)",
            "AI EOD analysis + Tomorrow strategy included in PDF",
        ]

        filename = f"RRE_EOD_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        await send_document(pdf_bytes, filename, "\n".join(caption_lines))
        logger.info(
            "EOD Report sent — %d gainers, %d losers, %d LB, %d watchlist, %d runners",
            len(gainers), len(losers), len(long_buildup), len(watchlist), len(runners),
        )
        try:
            from services.supabase_service import log_report_sent
            log_report_sent("eod", f"{len(gainers)}G {len(losers)}L {len(long_buildup)}LB {len(runners)}runners",
                            {"gainers": len(gainers), "losers": len(losers),
                             "buildup": len(long_buildup), "runners": len(runners)})
        except Exception:
            pass

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
    from datetime import timezone, timedelta
    _IST = timezone(timedelta(hours=5, minutes=30))
    logger.info("EOD report loop started")
    fired_today: str = ""
    await asyncio.sleep(60)

    while True:
        now = datetime.now(_IST)
        today_key = now.strftime("%Y-%m-%d")
        if (now.weekday() < 5
                and now.hour == 16
                and now.minute < 10
                and fired_today != today_key):
            fired_today = today_key
            await eod_scan_and_send()
        await asyncio.sleep(60)
