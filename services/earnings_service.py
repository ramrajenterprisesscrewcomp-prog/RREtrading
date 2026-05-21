"""
earnings_service.py — Quarterly results and earnings analysis for runner candidates.
Fetches latest quarterly P&L from Yahoo Finance (incomeStatementHistoryQuarterly).
Only returns data for quarters released in the last 120 days (current-cycle results).
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_YF_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ── Single-symbol fetch ────────────────────────────────────────────────────────

async def _fetch_one(symbol: str, client: httpx.AsyncClient) -> dict | None:
    """
    Fetch latest 4 quarters of income statement + EPS estimate vs actual.
    Returns structured dict or None if data is unavailable / older than 120 days.
    """
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}.NS"
        f"?modules=incomeStatementHistoryQuarterly,earnings"
    )
    try:
        r = await client.get(url, timeout=12)
        if r.status_code != 200:
            return None
        top = r.json().get("quoteSummary", {}).get("result", [None])[0]
        if not top:
            return None

        stmts = (top.get("incomeStatementHistoryQuarterly") or {}).get("incomeStatementHistory", [])
        if not stmts:
            return None

        def _raw(obj, k):
            v = (obj or {}).get(k, {})
            return v.get("raw") if isinstance(v, dict) else v

        q0 = stmts[0]
        q1 = stmts[1] if len(stmts) > 1 else None   # prev quarter (QoQ base)
        q4 = stmts[3] if len(stmts) > 3 else None   # same quarter last year (YoY base)

        # Only include results from the last 120 days
        end_ts = _raw(q0, "endDate")
        if not end_ts:
            return None
        end_dt   = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        age_days = (datetime.now(timezone.utc) - end_dt).days
        if age_days > 120:
            return None

        quarter_str = end_dt.strftime("%b '%y")

        def _cr(v):
            """Raw INR → crores, rounded."""
            return round(v / 1e7, 0) if v else None

        def _pct(new, old):
            if new and old and old != 0:
                return round((new - old) / abs(old) * 100, 1)
            return None

        rev0 = _raw(q0, "totalRevenue");  pat0 = _raw(q0, "netIncome")
        rev1 = _raw(q1, "totalRevenue")  if q1 else None
        pat1 = _raw(q1, "netIncome")     if q1 else None
        rev4 = _raw(q4, "totalRevenue")  if q4 else None
        pat4 = _raw(q4, "netIncome")     if q4 else None

        # EPS beat / miss
        eps_actual = eps_est = None
        earn_chart = (top.get("earnings") or {}).get("earningsChart") or {}
        q_eps = earn_chart.get("quarterly", [])
        if q_eps:
            last = q_eps[-1]
            eps_actual = (_raw(last, "actual")   if isinstance(last.get("actual"),   dict) else last.get("actual"))
            eps_est    = (_raw(last, "estimate") if isinstance(last.get("estimate"), dict) else last.get("estimate"))
            # Handle nested dict
            if isinstance(eps_actual, dict):
                eps_actual = eps_actual.get("raw")
            if isinstance(eps_est, dict):
                eps_est = eps_est.get("raw")

        return {
            "quarter":    quarter_str,
            "revenue_cr": _cr(rev0),
            "pat_cr":     _cr(pat0),
            "rev_qoq":    _pct(rev0, rev1),
            "rev_yoy":    _pct(rev0, rev4),
            "pat_qoq":    _pct(pat0, pat1),
            "pat_yoy":    _pct(pat0, pat4),
            "eps_actual": round(float(eps_actual), 2) if eps_actual is not None else None,
            "eps_est":    round(float(eps_est),    2) if eps_est    is not None else None,
            "eps_beat":   (float(eps_actual) > float(eps_est))
                          if (eps_actual is not None and eps_est is not None) else None,
        }
    except Exception as exc:
        logger.debug("Earnings fetch failed for %s: %s", symbol, exc)
        return None


# ── Batch fetch ────────────────────────────────────────────────────────────────

async def get_runner_earnings(symbols: list[str], max_sym: int = 15) -> dict[str, dict]:
    """
    Batch-fetch recent quarterly results for runner candidates.
    Returns {symbol: earnings_dict} — only includes stocks with recent data.
    """
    targets = symbols[:max_sym]
    sem     = asyncio.Semaphore(5)

    async def _bounded(sym, client):
        async with sem:
            return sym, await _fetch_one(sym, client)

    async with httpx.AsyncClient(headers=_YF_HEADERS, follow_redirects=True, timeout=12) as client:
        pairs = await asyncio.gather(*[_bounded(s, client) for s in targets],
                                     return_exceptions=True)

    result = {}
    for p in pairs:
        if isinstance(p, tuple) and p[1] is not None:
            result[p[0]] = p[1]
    logger.info("Earnings fetch: %d/%d runners have recent quarterly data", len(result), len(targets))
    return result


# ── AI earnings analysis ───────────────────────────────────────────────────────

def generate_earnings_ai(runners_with_earnings: list[dict]) -> dict[str, str]:
    """
    Batch AI: 60-word earnings + technical alignment analysis per runner.
    Input: [{ symbol, grade, close, earnings: {...}, news: [...] }]
    Returns {symbol: analysis_text}.
    """
    if not runners_with_earnings:
        return {}
    try:
        from services.openai_service import _get_client, _under_limit, _record_cost
        if not _under_limit():
            return {}

        lines = []
        for r in runners_with_earnings[:10]:
            sym  = r["symbol"]
            e    = r["earnings"]
            grade = r.get("grade", "WATCH")
            close = r.get("close", 0)
            news  = r.get("news", [])

            def _g(k):
                v = e.get(k)
                return f"{v:+.1f}%" if v is not None else "N/A"

            rev_s = f"Rev:{e['revenue_cr']:.0f}Cr" if e.get("revenue_cr") else "Rev:N/A"
            pat_s = f"PAT:{e['pat_cr']:.0f}Cr"    if e.get("pat_cr")    else "PAT:N/A"
            eps_s = ""
            if e.get("eps_actual") is not None:
                if e.get("eps_est") is not None:
                    beat_s = "BEAT" if e.get("eps_beat") else "MISS"
                    eps_s  = f"EPS:{e['eps_actual']}vsEst:{e['eps_est']}({beat_s})"
                else:
                    eps_s = f"EPS:{e['eps_actual']}"
            news_s = "; ".join(news[:2]) if news else "No announcements"

            lines.append(
                f"{sym}[{grade}] CMP:₹{close:.0f} | {e.get('quarter','')} | "
                f"{rev_s} RevYoY:{_g('rev_yoy')} RevQoQ:{_g('rev_qoq')} | "
                f"{pat_s} PATYoY:{_g('pat_yoy')} PATQoQ:{_g('pat_qoq')} | "
                f"{eps_s} | News:{news_s}"
            )

        prompt = (
            "You are a senior Indian equity research analyst evaluating runner candidates "
            "based on their latest quarterly results.\n\n"
            "For EACH stock, write EXACTLY 60 words covering:\n"
            "1. Earnings quality: revenue and PAT trend (strong/weak/mixed growth)\n"
            "2. EPS beat or miss vs analyst estimates and what it signals\n"
            "3. Whether the technical breakout aligns with the fundamental trajectory\n"
            "4. Sector context, management guidance signals from news\n"
            "5. Key risk or near-term catalyst to watch\n\n"
            "Data:\n" + "\n".join(lines) + "\n\n"
            "Respond ONLY in JSON (no markdown): {\"SYMBOL\": \"60-word analysis\", ...}"
        )

        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1800,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        _record_cost(resp.usage)
        return json.loads(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("Earnings AI failed: %s", exc)
        return {}
