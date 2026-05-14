"""
openai_service.py
AI analysis helpers — only active 8:00 AM – 4:30 PM IST on weekdays.
Used exclusively by pm_report_service and eod_report_service.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from openai import OpenAI
from cachetools import TTLCache

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

_client: OpenAI | None = None
_client_key: str = ""

# Market summary refreshes every 30 minutes — shared across all callers
_summary_cache: TTLCache = TTLCache(maxsize=1, ttl=1800)


def _is_ai_hours() -> bool:
    """True only between 8:00 AM and 4:30 PM IST on weekdays."""
    now = datetime.now(_IST)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    return (h >= 8) and (h < 16 or (h == 16 and m <= 30))


def _get_client() -> OpenAI:
    global _client, _client_key
    from config import OPENAI_API_KEY as _key
    if _client is None or _client_key != _key:
        _client = OpenAI(api_key=_key)
        _client_key = _key
    return _client


def _fmt_technical(tech: dict | None) -> str:
    if not tech:
        return "- Technical data unavailable"
    st = tech.get("supertrend") or {}
    return (
        f"- RSI(14): {tech.get('rsi', 'N/A')} | "
        f"EMA20: {tech.get('ema20', 'N/A')} | EMA50: {tech.get('ema50', 'N/A')}\n"
        f"- Supertrend(10,3): {st.get('direction', 'N/A')} at {st.get('value', 'N/A')}"
    )


def generate_market_summary(overview: dict) -> str:
    """
    2-3 sentence F&O market mood summary.
    Cached 30 minutes. Returns empty string outside 8 AM – 4:30 PM IST.
    """
    if not _is_ai_hours():
        return ""
    if "s" in _summary_cache:
        return _summary_cache["s"]

    buildup  = overview.get("buildup", {})
    indices  = overview.get("indices", {})
    nifty_oi = overview.get("nifty_oi", {})

    lb = len(buildup.get("Long Buildup",  []))
    sb = len(buildup.get("Short Buildup", []))
    sc = len(buildup.get("Short Covering", []))

    nifty     = indices.get("nifty50",   {})
    banknifty = indices.get("banknifty", {})
    top_lb    = [s["symbol"] for s in buildup.get("Long Buildup",  [])[:3]]
    top_sb    = [s["symbol"] for s in buildup.get("Short Buildup", [])[:3]]

    weekly_max_oi_strike = (
        max(nifty_oi.get("weekly_strikes", []), key=lambda x: x.get("total_oi", 0))
        if nifty_oi.get("weekly_strikes") else {}
    )

    prompt = (
        f"F&O market snapshot:\n"
        f"- Nifty 50: {nifty.get('value', 'N/A')} ({nifty.get('pchange', 'N/A')}%)\n"
        f"- Bank Nifty: {banknifty.get('value', 'N/A')} ({banknifty.get('pchange', 'N/A')}%)\n"
        f"- OI: Long Buildup {lb}, Short Buildup {sb}, Short Covering {sc}\n"
        f"- Top LB: {', '.join(top_lb) or 'None'} | Top SB: {', '.join(top_sb) or 'None'}\n"
        f"- Max OI strike: {weekly_max_oi_strike.get('strike', 'N/A')}\n\n"
        "In 2-3 sentences summarize Indian F&O market mood and dominant trend. No disclaimers."
    )
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.35,
        )
        result = resp.choices[0].message.content.strip()
        _summary_cache["s"] = result
        return result
    except Exception as exc:
        logger.warning("Market AI summary failed: %s", exc)
        trend = "Bullish" if lb > sb else "Bearish" if sb > lb else "Mixed"
        return (
            f"Market: {trend} — {lb} Long Buildup vs {sb} Short Buildup. "
            f"Nifty {nifty.get('value', 'N/A')} ({nifty.get('pchange', 0):+.2f}%)."
        )


def generate_ai_analysis(data: dict) -> dict:
    """
    Stock analysis for the dashboard page.
    Returns empty result outside 8 AM – 4:30 PM IST to avoid off-hours spend.
    """
    _empty = {"summary": "", "sentiment": "Neutral", "key_risks": [], "key_catalysts": []}
    if not _is_ai_hours():
        return _empty

    news_headlines = "; ".join(
        n["title"] for n in (data.get("news") or [])[:5] if n.get("title")
    )

    fno_section = (
        f"- F&O Signal: {data.get('buildup_signal', 'N/A')}\n"
        f"- OI Change: {data.get('oi_change_pct', 'N/A')}%  PCR: {data.get('pcr', 'N/A')}"
        if data.get("is_fno") else "- Not in F&O segment"
    )

    prompt = (
        f"Indian stock analyst. Analyze {data.get('symbol')} ({data.get('company_name', '')}).\n\n"
        f"Price: {data.get('last_price', 'N/A')}  Chg: {data.get('pchange', 'N/A')}%  "
        f"52W H/L: {data.get('week_52_high', 'N/A')}/{data.get('week_52_low', 'N/A')}\n"
        f"PE: {data.get('pe_ratio', 'N/A')}  ROE: {data.get('roe', 'N/A')}%  "
        f"ROCE: {data.get('roce', 'N/A')}%  MktCap: {data.get('market_cap', 'N/A')} Cr\n"
        f"{fno_section}\n"
        f"{_fmt_technical(data.get('technical'))}\n"
        f"News: {news_headlines or 'None'}\n\n"
        "Reply in JSON only:\n"
        '{"summary":"2-3 sentences","sentiment":"Bullish|Bearish|Neutral",'
        '"key_risks":["r1","r2"],"key_catalysts":["c1","c2"]}'
    )
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=400,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("Stock AI analysis failed: %s", exc)
        return _empty
