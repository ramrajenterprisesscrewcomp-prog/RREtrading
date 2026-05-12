import json
import logging
from openai import OpenAI
from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

_client: OpenAI | None = None
_client_key: str = ""


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
        f"EMA20: ₹{tech.get('ema20', 'N/A')} | EMA50: ₹{tech.get('ema50', 'N/A')}\n"
        f"- Supertrend(10,3): {st.get('direction', 'N/A')} at ₹{st.get('value', 'N/A')}"
    )


def generate_market_summary(overview: dict) -> str:
    """
    2-3 sentence market mood summary from OI buildup distribution + index moves.
    Falls back to a rule-based summary if OpenAI is unavailable.
    """
    buildup   = overview.get("buildup", {})
    indices   = overview.get("indices", {})
    nifty_oi  = overview.get("nifty_oi", {})

    lb = len(buildup.get("Long Buildup",  []))
    sb = len(buildup.get("Short Buildup", []))
    sc = len(buildup.get("Short Covering",[]))
    lc = len(buildup.get("Long Covering", []))

    nifty     = indices.get("nifty50",   {})
    banknifty = indices.get("banknifty", {})
    top_lb    = [s["symbol"] for s in buildup.get("Long Buildup",  [])[:3]]
    top_sb    = [s["symbol"] for s in buildup.get("Short Buildup", [])[:3]]

    weekly_exp = nifty_oi.get("weekly_expiry", "N/A")
    weekly_max_oi_strike = (
        max(nifty_oi.get("weekly_strikes", []), key=lambda x: x.get("total_oi", 0))
        if nifty_oi.get("weekly_strikes") else {}
    )

    prompt = (
        f"F&O market snapshot:\n"
        f"- Nifty 50: {nifty.get('value','N/A')} (change {nifty.get('pchange','N/A')}%)\n"
        f"- Bank Nifty: {banknifty.get('value','N/A')} (change {banknifty.get('pchange','N/A')}%)\n"
        f"- OI signals — Long Buildup: {lb}, Short Buildup: {sb}, Short Covering: {sc}, Long Covering: {lc}\n"
        f"- Top Long Buildup: {', '.join(top_lb) or 'None'}\n"
        f"- Top Short Buildup: {', '.join(top_sb) or 'None'}\n"
        f"- Nifty nearest expiry: {weekly_exp} | Max OI strike: {weekly_max_oi_strike.get('strike','N/A')}\n\n"
        "In 2-3 sentences, summarize the current Indian F&O market mood and dominant trend. "
        "Be specific about support/resistance levels from OI data. No disclaimers."
    )
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.35,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.warning("Market AI summary failed: %s", exc)
        trend = "Bullish" if lb > sb else "Bearish" if sb > lb else "Mixed"
        return (
            f"Market shows {trend} F&O activity: {lb} Long Buildup vs {sb} Short Buildup signals. "
            f"{sc} stocks show short covering. "
            f"Nifty near {nifty.get('value','N/A')} with {nifty.get('pchange',0):+.2f}% change."
        )


def generate_policy_alert_analysis(title: str, description: str = "") -> str:
    """Analyze which sectors/stocks benefit from a government/RBI/SEBI policy news."""
    prompt = (
        f"Government/regulatory news for Indian markets:\n"
        f"Headline: {title}\n"
        f"Details: {description[:300] if description else 'N/A'}\n\n"
        "In 3-4 concise sentences:\n"
        "1. What is this policy about?\n"
        "2. Which sectors/industries benefit most?\n"
        "3. Which 2-3 specific NSE stocks could gain?\n"
        "4. Any sectors that might be negatively impacted?\n"
        "Be specific with sector names and stock symbols. No disclaimers."
    )
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.warning("Policy AI failed: %s", exc)
        return "AI analysis temporarily unavailable."


def generate_ai_analysis(data: dict) -> dict:
    """
    Send collected stock data to GPT-4o and get structured analysis.
    Returns {"summary": str, "sentiment": str, "key_risks": list, "key_catalysts": list}
    """
    client = _get_client()

    news_headlines = "; ".join(
        n["title"] for n in (data.get("news") or [])[:5] if n.get("title")
    )
    policy_headlines = "; ".join(
        n["title"] for n in (data.get("policy_news") or [])[:3] if n.get("title")
    )

    fno_section = ""
    if data.get("is_fno"):
        fno_section = f"""
- F&O Signal: {data.get("buildup_signal", "N/A")}
- Open Interest Change: {data.get("oi_change_pct", "N/A")}%
- Put-Call Ratio (OI): {data.get("pcr", "N/A")}
- Max Pain Level: ₹{data.get("max_pain", "N/A")}"""
    else:
        fno_section = "- F&O: Stock not in F&O segment"

    prompt = f"""You are a professional Indian stock market analyst. Analyze the following data for {data.get("symbol")} and provide a comprehensive assessment.

STOCK DATA:
- Symbol: {data.get("symbol")} | Company: {data.get("company_name", "N/A")}
- Sector: {data.get("sector", "N/A")} | Industry: {data.get("industry", "N/A")}
- Current Price: ₹{data.get("last_price", "N/A")} | Change: {data.get("pchange", "N/A")}%
- Open: ₹{data.get("open", "N/A")} | High: ₹{data.get("high", "N/A")} | Low: ₹{data.get("low", "N/A")}
- 52W High: ₹{data.get("week_52_high", "N/A")} | 52W Low: ₹{data.get("week_52_low", "N/A")}
- Volume: {data.get("volume", "N/A")} | Delivery %: {data.get("delivery_pct", "N/A")}%

FUNDAMENTALS:
- Market Cap: ₹{data.get("market_cap", "N/A")} Cr
- P/E Ratio: {data.get("pe_ratio", "N/A")} | Sector Median PE: {data.get("sector_median_pe", "N/A")} | Valuation: {data.get("valuation_vs_sector", "N/A")}
- Book Value / P/B: {data.get("book_value", "N/A")}
- ROE: {data.get("roe", "N/A")}% | ROCE: {data.get("roce", "N/A")}%
- Dividend Yield: {data.get("div_yield", "N/A")}%
{fno_section}

TECHNICAL INDICATORS:
{_fmt_technical(data.get("technical"))}

NEWS (last 7 days):
{news_headlines or "No recent news available"}

POLICY/REGULATORY NEWS:
{policy_headlines or "No relevant policy news"}

Based on the above data, provide your analysis in the following JSON format ONLY (no extra text):
{{
  "summary": "3-4 paragraph analysis covering price action, technical position (relative to 52W range), fundamental health, F&O sentiment (if applicable), and news impact",
  "sentiment": "Bullish or Bearish or Neutral",
  "key_risks": ["risk1", "risk2", "risk3"],
  "key_catalysts": ["catalyst1", "catalyst2", "catalyst3"]
}}"""

    try:
        response = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=1200,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:
        logger.error("OpenAI analysis failed: %s", exc)
        return {
            "summary": f"AI analysis could not be generated at this time. Error: {exc}",
            "sentiment": "Neutral",
            "key_risks": [],
            "key_catalysts": [],
        }


def generate_news_impact_analysis(category: str, title: str, details: str = "") -> dict:
    """
    Analyze market impact of a high-impact news event.
    Returns sectors affected, focus stocks, direction, urgency.
    """
    prompt = (
        f"You are a senior Indian stock market analyst.\n"
        f"Category: {category}\n"
        f"Headline: {title}\n"
        f"Details: {details[:400] if details else 'N/A'}\n\n"
        "Analyze the impact on NSE/BSE in JSON format ONLY:\n"
        "{\n"
        '  "summary": "2-3 sentence analysis",\n'
        '  "impact_direction": "Bullish or Bearish or Neutral or Mixed",\n'
        '  "bullish_sectors": ["sector1", "sector2"],\n'
        '  "bearish_sectors": ["sector1"],\n'
        '  "focus_stocks": ["NSE_SYM1", "NSE_SYM2", "NSE_SYM3"],\n'
        '  "urgency": "Immediate or Short-Term or Watch"\n'
        "}"
    )
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=400,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("News impact AI failed: %s", exc)
        return {
            "summary": "AI analysis temporarily unavailable.",
            "impact_direction": "Neutral",
            "bullish_sectors": [],
            "bearish_sectors": [],
            "focus_stocks": [],
            "urgency": "Watch",
        }
