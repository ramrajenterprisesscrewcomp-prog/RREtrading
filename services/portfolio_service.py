"""
portfolio_service.py
Personal portfolio manager — store holdings, fetch live P&L, AI analysis in reports.
Supabase is primary storage; local JSON file is fallback.
"""
import json
import logging
import pathlib
from datetime import datetime

logger = logging.getLogger(__name__)

_DATA_DIR       = pathlib.Path(__file__).parent.parent / "data"
_PORTFOLIO_FILE = _DATA_DIR / "portfolio.json"


# ── Local JSON helpers (fallback) ─────────────────────────────────────────────

def _load_json() -> list[dict]:
    _DATA_DIR.mkdir(exist_ok=True)
    try:
        if _PORTFOLIO_FILE.exists():
            return json.loads(_PORTFOLIO_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not load portfolio JSON: %s", exc)
    return []


def _save_json(holdings: list[dict]) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    try:
        _PORTFOLIO_FILE.write_text(
            json.dumps(holdings, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not save portfolio JSON: %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def load_portfolio() -> list[dict]:
    from services.supabase_service import load_portfolio_db
    result = load_portfolio_db()
    if result is not None:
        _save_json(result)          # keep local copy in sync
        return result
    return _load_json()             # fallback


def save_portfolio(holdings: list[dict]) -> None:
    _save_json(holdings)


def add_or_update_holding(symbol: str, qty: float, avg_price: float) -> list[dict]:
    from services.supabase_service import upsert_holding_db
    symbol = symbol.upper().strip()
    today  = datetime.now().strftime("%d %b %Y")

    # Try Supabase first
    upsert_holding_db(symbol, qty, avg_price, added_on=today, updated_on=today)

    # Also keep JSON in sync
    holdings = _load_json()
    for h in holdings:
        if h["symbol"] == symbol:
            h["qty"]        = qty
            h["avg_price"]  = avg_price
            h["updated_on"] = today
            _save_json(holdings)
            return holdings
    holdings.append({"symbol": symbol, "qty": qty, "avg_price": avg_price, "added_on": today})
    _save_json(holdings)
    return holdings


def remove_holding(symbol: str) -> list[dict]:
    from services.supabase_service import delete_holding_db
    symbol = symbol.upper().strip()
    delete_holding_db(symbol)

    holdings = [h for h in _load_json() if h["symbol"] != symbol]
    _save_json(holdings)
    return holdings


async def get_portfolio_with_ltp(holdings: list[dict] | None = None) -> list[dict]:
    """Fetch live LTP for each holding and compute P&L."""
    if holdings is None:
        holdings = load_portfolio()
    if not holdings:
        return []

    from services.nse_service import get_live_ltps
    symbols   = [h["symbol"] for h in holdings]
    live_ltps = await get_live_ltps(symbols)

    result = []
    for h in holdings:
        sym      = h["symbol"]
        qty      = float(h.get("qty", 0))
        avg      = float(h.get("avg_price", 0))
        ltp      = live_ltps.get(sym, 0)
        invested = round(qty * avg, 2)
        current  = round(qty * ltp, 2) if ltp > 0 else 0
        pnl      = round(current - invested, 2) if ltp > 0 else 0
        pnl_pct  = round((ltp / avg - 1) * 100, 2) if (avg > 0 and ltp > 0) else 0
        result.append({
            **h,
            "ltp":      round(ltp, 2),
            "invested": invested,
            "current":  current,
            "pnl":      pnl,
            "pnl_pct":  pnl_pct,
        })
    return result


def generate_portfolio_analysis(portfolio: list[dict], sector_context: str = "") -> dict:
    """
    GPT-4o-mini analysis for each holding + overall advice.
    Returns {"stocks": {symbol: "action text"}, "overall": "text"}
    """
    if not portfolio:
        return {"stocks": {}, "overall": ""}
    try:
        from services.openai_service import _get_client, _is_ai_hours, _under_limit, _record_cost
        if not _is_ai_hours() or not _under_limit():
            return {"stocks": {}, "overall": ""}

        lines = []
        for h in portfolio:
            sym     = h["symbol"]
            qty     = h.get("qty", 0)
            avg     = h.get("avg_price", 0)
            ltp     = h.get("ltp", 0)
            pnl_pct = h.get("pnl_pct", 0)
            pnl     = h.get("pnl", 0)
            lines.append(
                f"  {sym}: Qty={qty}  AvgCost={avg}  LTP={ltp}  "
                f"P&L={pnl:+.0f} ({pnl_pct:+.1f}%)"
            )

        prompt = (
            "You are a professional Indian stock market portfolio advisor.\n"
            "Analyze this investor's portfolio at today's market close:\n\n"
            + "\n".join(lines)
            + (f"\n\nMarket context: {sector_context}" if sector_context else "")
            + "\n\n"
            "For EACH stock above give a 20-word action recommendation: "
            "HOLD / ADD / REDUCE / EXIT — with a specific reason (price level, trend, P&L position).\n\n"
            "Then write OVERALL PORTFOLIO ASSESSMENT (80 words): "
            "health, concentration risk, total P&L status, and top-priority action.\n\n"
            "Respond ONLY in this exact JSON (no markdown):\n"
            '{"stocks": {"SYMBOL1": "HOLD — 20-word reason", "SYMBOL2": "..."}, '
            '"overall": "80-word assessment"}'
        )

        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=900,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        _record_cost(resp.usage)
        data = json.loads(resp.choices[0].message.content)
        return {
            "stocks":  data.get("stocks", {}),
            "overall": data.get("overall", ""),
        }
    except Exception as exc:
        logger.warning("Portfolio AI analysis failed: %s", exc)
        return {"stocks": {}, "overall": ""}
