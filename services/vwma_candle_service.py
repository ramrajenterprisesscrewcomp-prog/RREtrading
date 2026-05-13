"""
vwma_candle_service.py
2-candle reversal setup scanner — Nifty 500 daily.

  Day N-1 : Doji / Hammer / Bullish Pin Bar  (reversal candle)
  Day N   : Bullish Engulfing  (open <= prev_close AND close >= prev_open AND close > open)

OHLCV source: Yahoo Finance daily (30-day window)
"""
import asyncio
import io
import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
def _now_ist() -> datetime:
    return datetime.now(_IST)

_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

_REVERSAL_PATTERNS = {
    "Doji", "Long Legged Doji", "Hammer", "Inverted Hammer",
    "Bullish Pin Bar", "Bearish Pin Bar", "Spinning Top",
}


async def _fetch_daily(symbol: str, client: httpx.AsyncClient) -> dict | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
    try:
        r = await client.get(url, params={"interval": "1d", "range": "30d"})
        r.raise_for_status()
        data   = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        q       = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes  = [x for x in (q.get("close")  or []) if x is not None]
        volumes = [x for x in (q.get("volume") or []) if x is not None]
        highs   = [x for x in (q.get("high")   or []) if x is not None]
        lows    = [x for x in (q.get("low")    or []) if x is not None]
        opens   = [x for x in (q.get("open")   or []) if x is not None]
        n = min(len(closes), len(volumes), len(highs), len(lows), len(opens))
        if n < 3:
            return None
        return {
            "closes": closes[:n], "volumes": volumes[:n],
            "highs":  highs[:n],  "lows":    lows[:n],
            "opens":  opens[:n],
        }
    except Exception:
        return None


# ── Scanner ───────────────────────────────────────────────────────────────────

async def scan_vwma_candle() -> list[dict]:
    """
    Scan Nifty 500 for:
      Day N-1: Doji / Hammer / Pin Bar
      Day N  : Bullish Engulfing (open <= prev_close AND close >= prev_open AND close > open)
    Sorted by % change descending.
    """
    from services.candle_service import detect_pattern
    from services.nse_service import get_nifty500_ohlc

    stocks = await get_nifty500_ohlc()
    if not stocks:
        return []

    symbols = [s["symbol"] for s in stocks if s.get("symbol")]
    logger.info("Reversal scan: %d Nifty 500 symbols", len(symbols))
    sem = asyncio.Semaphore(20)

    async def _check(sym: str, client: httpx.AsyncClient) -> dict | None:
        async with sem:
            data = await _fetch_daily(sym, client)
            if not data:
                return None

            # Day N-1 (reversal candle)
            o1 = data["opens"][-2];  h1 = data["highs"][-2]
            l1 = data["lows"][-2];   c1 = data["closes"][-2]

            prev_pats = detect_pattern(o1, h1, l1, c1)
            reversal  = [p for p in prev_pats if p in _REVERSAL_PATTERNS]
            if not reversal:
                return None

            # Day N (must be bullish engulfing)
            o2 = data["opens"][-1];  h2 = data["highs"][-1]
            l2 = data["lows"][-1];   c2 = data["closes"][-1]

            if not (c2 > o2 and o2 <= c1 and c2 >= o1):
                return None

            prev2_c  = data["closes"][-3] if len(data["closes"]) >= 3 else c1
            pchange  = round((c2 - prev2_c) / prev2_c * 100, 2) if prev2_c else 0.0
            hl2      = h2 - l2
            body_pct = round((c2 - o2) / hl2 * 100, 1) if hl2 else 0.0

            return {
                "symbol":   sym,
                "ltp":      round(c2, 2),
                "patterns": reversal,
                "pchange":  pchange,
                "body_pct": body_pct,
                "rev_o": round(o1, 2), "rev_h": round(h1, 2),
                "rev_l": round(l1, 2), "rev_c": round(c1, 2),
                "eng_o": round(o2, 2), "eng_h": round(h2, 2),
                "eng_l": round(l2, 2), "eng_c": round(c2, 2),
            }

    async with httpx.AsyncClient(headers=_YF_HEADERS, timeout=15, follow_redirects=True) as client:
        raw = await asyncio.gather(*[_check(s, client) for s in symbols])

    hits = sorted([r for r in raw if r], key=lambda x: -x["pchange"])
    logger.info("Reversal scan: %d hits / %d scanned", len(hits), len(symbols))
    return hits


# ── PDF Builder ───────────────────────────────────────────────────────────────

def _build_vwma_pdf(hits: list[dict], date_str: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    BLUE   = colors.HexColor("#1e3a5f")
    GREEN  = colors.HexColor("#059669")
    AMBER  = colors.HexColor("#d97706")
    GRAY   = colors.HexColor("#6b7280")
    ALTROW = colors.HexColor("#f9fafb")
    BORDER = colors.HexColor("#e5e7eb")
    TEAL   = colors.HexColor("#0891b2")

    def sty(name, font="Helvetica", size=9, color=BLUE, align=TA_LEFT, before=0, after=2, bold=False):
        return ParagraphStyle(name, fontName="Helvetica-Bold" if bold else font,
                              fontSize=size, textColor=color, alignment=align,
                              spaceBefore=before, spaceAfter=after)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=12*mm, bottomMargin=12*mm)
    story = []

    story.append(Paragraph("RRE Market Scanner", sty("T", size=15, align=TA_CENTER, bold=True, after=3)))
    story.append(Paragraph(
        "Doji / Hammer / Pin Bar  +  Bullish Engulfing  --  Daily  --  Nifty 500",
        sty("S", size=8, color=GRAY, align=TA_CENTER, after=2)))
    story.append(Paragraph(date_str, sty("D", size=8, color=GRAY, align=TA_CENTER, after=4)))
    story.append(HRFlowable(width="100%", thickness=1.5, color=TEAL))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        f"Stocks found: {len(hits)}   --   Day N-1: Reversal candle  |  Day N: Bullish Engulfing  --  Sorted by % change",
        sty("SUM", size=8, color=BLUE, before=2, after=4)))

    if not hits:
        story.append(Paragraph("No stocks found with this setup today.", sty("NF", size=9, color=GRAY)))
    else:
        hdr = ["#", "Symbol", "Reversal (N-1)", "Engulfing (N)", "LTP", "Chg%", "Body%"]
        col_w = [7*mm, 24*mm, 46*mm, 46*mm, 20*mm, 15*mm, 13*mm]
        rows = [hdr]
        for i, h in enumerate(hits, 1):
            pat = " | ".join(h["patterns"])
            rows.append([
                str(i), h["symbol"],
                f"{pat}  O:{h['rev_o']}  C:{h['rev_c']}",
                f"Engulfing  O:{h['eng_o']}  C:{h['eng_c']}",
                f"{h['ltp']:,.2f}",
                f"{h['pchange']:+.2f}%",
                f"{h['body_pct']:.0f}%",
            ])

        t = Table(rows, colWidths=col_w)
        ts = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), TEAL),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (1, 1), (3, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 6.5),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        for i, h in enumerate(hits, start=1):
            ts.add("BACKGROUND", (0, i), (-1, i), ALTROW if i % 2 == 0 else colors.white)
            ts.add("TEXTCOLOR",  (2, i), (2, i), AMBER)
            ts.add("TEXTCOLOR",  (3, i), (3, i), GREEN)
            pch_clr = GREEN if h["pchange"] >= 0 else colors.HexColor("#dc2626")
            ts.add("TEXTCOLOR",  (5, i), (5, i), pch_clr)
            ts.add("FONTNAME",   (5, i), (5, i), "Helvetica-Bold")
        t.setStyle(ts)
        story.append(t)

    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
    story.append(Paragraph(
        f"Generated by RRE Market Scanner  --  {_now_ist().strftime('%d %b %Y %H:%M IST')}  --  Nifty 500",
        sty("FT", size=6.5, color=GRAY, align=TA_CENTER, before=6)))
    doc.build(story)
    return buf.getvalue()


# ── Report Send ───────────────────────────────────────────────────────────────

async def send_vwma_candle_report() -> None:
    from services.telegram_service import send_document, send_message

    date_str = _now_ist().strftime("%d %b %Y")
    try:
        hits = await scan_vwma_candle()

        if not hits:
            await send_message(
                f"<b>Reversal + Engulfing Setup -- {date_str}</b>\n\n"
                f"No stocks found with Doji/Hammer/Pin Bar + Bullish Engulfing today."
            )
            return

        pdf_bytes = await asyncio.to_thread(_build_vwma_pdf, hits, date_str)

        pat_count: dict[str, int] = {}
        for h in hits:
            for p in h.get("patterns", []):
                pat_count[p] = pat_count.get(p, 0) + 1

        pat_summary = "  |  ".join(f"{p}: {n}" for p, n in sorted(pat_count.items(), key=lambda x: -x[1]))
        top5 = "  ".join(h["symbol"] for h in hits[:5])

        caption = (
            f"<b>Doji/Hammer/Pin Bar + Bullish Engulfing  --  {date_str}</b>\n"
            f"<i>Nifty 500 daily  --  2-candle reversal setup</i>\n\n"
            f"<b>{len(hits)}</b> stocks matched\n"
            f"<b>Patterns:</b> {pat_summary}\n\n"
            f"<b>Top picks:</b> {top5}"
        )

        filename = f"RRE_Reversal_{_now_ist().strftime('%Y%m%d')}.pdf"
        await send_document(pdf_bytes, filename, caption)
        logger.info("Reversal report sent: %d hits", len(hits))

    except Exception as exc:
        logger.warning("Reversal report error: %s", exc, exc_info=True)
        await send_message(f"Reversal setup report error: {exc}")
