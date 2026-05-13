"""
vwma_candle_service.py
Daily scan: TSR stocks touching VWMA(20) with Doji / Pin Bar / Hammer / Bullish candle.

Stock universe: TSR weekly_support + monthly_support + long_buildup + signals (30D breakout)
OHLCV source  : Yahoo Finance (daily, 30-day window)
VWMA formula  : sum(close * volume, 20) / sum(volume, 20)  — matches TradingView default
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

VWMA_LEN       = 20
VWMA_TOUCH_PCT = 1.5   # within 1.5% of VWMA counts as "touching"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_symbol(row: dict) -> str:
    """Pull clean NSE symbol from a TSR row dict."""
    raw = row.get("Code") or row.get("Symbol") or row.get("Name") or ""
    return raw.split()[0].upper().strip()


def _calc_vwma(closes: list[float], volumes: list[float], length: int = 20) -> float:
    """VWMA = sum(close_i * volume_i, n) / sum(volume_i, n)"""
    if len(closes) < length or len(volumes) < length:
        return 0.0
    c = closes[-length:]
    v = volumes[-length:]
    denom = sum(v)
    return sum(ci * vi for ci, vi in zip(c, v)) / denom if denom else 0.0


async def _fetch_daily(symbol: str, client: httpx.AsyncClient) -> dict | None:
    """Fetch 30 days of daily OHLCV from Yahoo Finance for a NSE symbol."""
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
        if n < VWMA_LEN + 1:
            return None
        return {
            "closes":  closes[:n],
            "volumes": volumes[:n],
            "highs":   highs[:n],
            "lows":    lows[:n],
            "opens":   opens[:n],
        }
    except Exception:
        return None


# ── Scanner ───────────────────────────────────────────────────────────────────

_REVERSAL_PATTERNS = {"Doji", "Long Legged Doji", "Hammer", "Bullish Pin Bar"}


async def scan_vwma_candle() -> list[dict]:
    """
    2-candle combo setup from TSR stock universe:
      Day N-1 : Doji / Pin Bar / Hammer at or near VWMA(20)
      Day N   : Bullish confirmation candle (close > open)
      VWMA    : low or close of either candle within VWMA_TOUCH_PCT of VWMA(20)

    Returns list of hits sorted by VWMA touch distance (closest first).
    """
    from services.tsr_service import get_tsr_all
    from services.candle_service import detect_pattern

    tsr = await get_tsr_all()

    raw_syms: set[str] = set()
    for src_key in ("weekly_support", "monthly_support", "signals"):
        for row in tsr.get(src_key, []):
            s = _extract_symbol(row)
            if s:
                raw_syms.add(s)
    buildup = tsr.get("buildup", {})
    for sub in ("long_buildup", "short_buildup", "short_covering"):
        for row in buildup.get(sub, []):
            s = _extract_symbol(row)
            if s:
                raw_syms.add(s)

    symbols = sorted(raw_syms)
    if not symbols:
        # TSR cache empty — fall back to Nifty 200
        logger.warning("VWMA scan: TSR cache empty, falling back to Nifty 200")
        try:
            from services.pivot_scanner_service import _fetch_nifty200_live
            n200 = await _fetch_nifty200_live()
            symbols = sorted({s["symbol"] for s in n200 if s.get("symbol")})
            logger.info("VWMA scan fallback: %d Nifty 200 symbols", len(symbols))
        except Exception as e:
            logger.warning("VWMA scan Nifty 200 fallback failed: %s", e)
            return []
    if not symbols:
        return []

    logger.info("VWMA scan: checking %d TSR symbols", len(symbols))
    sem = asyncio.Semaphore(15)

    async def _check(sym: str, client: httpx.AsyncClient) -> dict | None:
        async with sem:
            data = await _fetch_daily(sym, client)
            if not data or len(data["closes"]) < VWMA_LEN + 1:
                return None

            vwma = _calc_vwma(data["closes"], data["volumes"], VWMA_LEN)
            if not vwma:
                return None

            # Today's candle
            o = data["opens"][-1];  h = data["highs"][-1]
            l = data["lows"][-1];   c = data["closes"][-1]

            # Must be bullish (close > open)
            if c <= o:
                return None

            # Must match at least one reversal pattern
            patterns = detect_pattern(o, h, l, c)
            matched = [p for p in patterns if p in _REVERSAL_PATTERNS]
            if not matched:
                return None

            prev_c   = data["closes"][-2] if len(data["closes"]) >= 2 else c
            pchange  = round((c - prev_c) / prev_c * 100, 2) if prev_c else 0.0
            hl       = h - l
            body_pct = round((c - o) / hl * 100, 1) if hl else 0.0

            return {
                "symbol":   sym,
                "ltp":      round(c, 2),
                "patterns": matched,
                "pchange":  pchange,
                "body_pct": body_pct,
                "open":     round(o, 2),
                "high":     round(h, 2),
                "low":      round(l, 2),
            }

    async with httpx.AsyncClient(
        headers=_YF_HEADERS, timeout=15, follow_redirects=True
    ) as client:
        raw = await asyncio.gather(*[_check(s, client) for s in symbols])

    hits = sorted([r for r in raw if r], key=lambda x: -x["pchange"])
    logger.info("VWMA candle scan: %d hits / %d symbols", len(hits), len(symbols))
    return hits


# ── PDF Builder ───────────────────────────────────────────────────────────────

def _build_vwma_pdf(hits: list[dict], date_str: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    BLUE   = colors.HexColor("#1e3a5f")
    GREEN  = colors.HexColor("#059669")
    AMBER  = colors.HexColor("#d97706")
    GRAY   = colors.HexColor("#6b7280")
    LTGRAY = colors.HexColor("#f3f4f6")
    ALTROW = colors.HexColor("#f9fafb")
    BORDER = colors.HexColor("#e5e7eb")
    TEAL   = colors.HexColor("#0891b2")

    def sty(name, font="Helvetica", size=9, color=BLUE,
            align=TA_LEFT, before=0, after=2, bold=False):
        return ParagraphStyle(
            name, fontName="Helvetica-Bold" if bold else font,
            fontSize=size, textColor=color,
            alignment=align, spaceBefore=before, spaceAfter=after,
        )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=12*mm, bottomMargin=12*mm,
    )
    story = []

    story.append(Paragraph("RRE Market Scanner", sty("T", size=15, align=TA_CENTER, bold=True, after=3)))
    story.append(Paragraph(
        "Reversal Pattern + Bullish Candle  --  Daily  --  Nifty 200",
        sty("S", size=8, color=GRAY, align=TA_CENTER, after=2),
    ))
    story.append(Paragraph(date_str, sty("D", size=8, color=GRAY, align=TA_CENTER, after=4)))
    story.append(HRFlowable(width="100%", thickness=1.5, color=TEAL))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph(
        f"Stocks found: {len(hits)}   --   Pin Bar / Hammer / Doji  AND  close > open  --  Sorted by % change",
        sty("SUM", size=8, color=BLUE, before=2, after=4),
    ))

    if not hits:
        story.append(Paragraph(
            "No stocks found with Pin Bar/Hammer/Doji bullish candle today.",
            sty("NF", size=9, color=GRAY),
        ))
    else:
        hdr = ["#", "Symbol", "Pattern", "LTP", "Open", "High", "Low", "Chg%", "Body%"]
        col_w = [7*mm, 26*mm, 38*mm, 22*mm, 22*mm, 22*mm, 22*mm, 16*mm, 14*mm]
        rows = [hdr]
        for i, h in enumerate(hits, 1):
            rows.append([
                str(i), h["symbol"],
                " | ".join(h["patterns"]),
                f"{h['ltp']:,.2f}", f"{h['open']:,.2f}",
                f"{h['high']:,.2f}", f"{h['low']:,.2f}",
                f"{h['pchange']:+.2f}%", f"{h['body_pct']:.0f}%",
            ])

        t = Table(rows, colWidths=col_w)
        ts = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), TEAL),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",         (1, 1), (2, -1), "LEFT"),
            ("FONTSIZE",      (0, 1), (-1, -1), 7),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        for i, h in enumerate(hits, start=1):
            ts.add("BACKGROUND", (0, i), (-1, i), ALTROW if i % 2 == 0 else colors.white)
            ts.add("TEXTCOLOR",  (2, i), (2, i), TEAL)
            ts.add("FONTNAME",   (2, i), (2, i), "Helvetica-Bold")
            pch_clr = GREEN if h["pchange"] >= 0 else colors.HexColor("#dc2626")
            ts.add("TEXTCOLOR",  (7, i), (7, i), pch_clr)
            ts.add("FONTNAME",   (7, i), (7, i), "Helvetica-Bold")
        t.setStyle(ts)
        story.append(t)

    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
    story.append(Paragraph(
        f"Generated by RRE Market Scanner  --  {_now_ist().strftime('%d %b %Y %H:%M IST')}  --  Nifty 200",
        sty("FT", size=6.5, color=GRAY, align=TA_CENTER, before=6),
    ))

    doc.build(story)
    return buf.getvalue()


# ── Report Send ───────────────────────────────────────────────────────────────

async def send_vwma_candle_report() -> None:
    """Scan TSR stocks, build PDF, send to Telegram."""
    from services.telegram_service import send_document, send_message

    now_str  = _now_ist().strftime("%d %b %Y %H:%M IST")
    date_str = _now_ist().strftime("%d %b %Y")

    try:
        hits = await scan_vwma_candle()

        if not hits:
            await send_message(
                f"<b>VWMA(20) Candle Setup -- {date_str}</b>\n\n"
                f"No TSR stocks found touching VWMA(20) with a pattern signal."
            )
            return

        pdf_bytes = await asyncio.to_thread(_build_vwma_pdf, hits, date_str)

        # Telegram caption
        pat_count: dict[str, int] = {}
        for h in hits:
            for p in h.get("patterns", []):
                pat_count[p] = pat_count.get(p, 0) + 1

        pat_summary = "  |  ".join(
            f"{p}: {n}" for p, n in sorted(pat_count.items(), key=lambda x: -x[1])
        )
        top5 = "  ".join(h["symbol"] for h in hits[:5])

        caption = (
            f"<b>Reversal Pattern + Bullish Candle  --  {date_str}</b>\n"
            f"<i>Pin Bar / Hammer / Doji  AND  close &gt; open  --  Nifty 200 daily</i>\n\n"
            f"<b>{len(hits)}</b> stocks matched\n"
            f"<b>Patterns:</b> {pat_summary}\n\n"
            f"<b>Top picks:</b> {top5}"
        )

        filename = f"RRE_VWMA_Candle_{_now_ist().strftime('%Y%m%d')}.pdf"
        await send_document(pdf_bytes, filename, caption)
        logger.info("VWMA candle report sent: %d hits", len(hits))

    except Exception as exc:
        logger.warning("VWMA candle report error: %s", exc, exc_info=True)
        await send_message(f"VWMA candle report error: {exc}")
