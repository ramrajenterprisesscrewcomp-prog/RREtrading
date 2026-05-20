"""
supabase_service.py — Centralised Supabase client + all DB operations.
Falls back to local JSON files when Supabase is unavailable or unconfigured.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

_client = None


def get_db():
    """Return a Supabase client, or None if credentials not configured."""
    global _client
    if _client is not None:
        return _client
    try:
        from config import SUPABASE_URL, SUPABASE_KEY
        if not SUPABASE_URL or not SUPABASE_KEY:
            return None
        from supabase import create_client
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase: connected to %s", SUPABASE_URL)
    except Exception as exc:
        logger.warning("Supabase: init failed — %s", exc)
        return None
    return _client


# ── Portfolio ─────────────────────────────────────────────────────────────────

def load_portfolio_db() -> list[dict] | None:
    """Return all holdings from Supabase, or None on failure."""
    db = get_db()
    if db is None:
        return None
    try:
        res = db.table("portfolio").select("*").execute()
        rows = res.data or []
        return [
            {
                "symbol":     r["symbol"],
                "qty":        float(r["qty"]),
                "avg_price":  float(r["avg_price"]),
                "added_on":   r.get("added_on", ""),
                "updated_on": r.get("updated_on", ""),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Supabase load_portfolio failed: %s", exc)
        return None


def upsert_holding_db(symbol: str, qty: float, avg_price: float,
                      added_on: str = "", updated_on: str = "") -> bool:
    db = get_db()
    if db is None:
        return False
    try:
        db.table("portfolio").upsert({
            "symbol":     symbol,
            "qty":        qty,
            "avg_price":  avg_price,
            "added_on":   added_on,
            "updated_on": updated_on,
        }, on_conflict="symbol").execute()
        return True
    except Exception as exc:
        logger.warning("Supabase upsert_holding failed: %s", exc)
        return False


def delete_holding_db(symbol: str) -> bool:
    db = get_db()
    if db is None:
        return False
    try:
        db.table("portfolio").delete().eq("symbol", symbol).execute()
        return True
    except Exception as exc:
        logger.warning("Supabase delete_holding failed: %s", exc)
        return False


# ── Prev Runners ──────────────────────────────────────────────────────────────

def save_runners_db(runners: list[dict], date_str: str) -> bool:
    """Upsert today's runners into Supabase (replace previous day's rows)."""
    db = get_db()
    if db is None:
        return False
    try:
        db.table("prev_runners").delete().neq("date", date_str).execute()
        rows = [
            {
                "date":    date_str,
                "symbol":  r.get("symbol", ""),
                "pchange": float(r.get("pchange", 0)),
                "close":   float(r.get("close", 0) or r.get("ltp", 0)),
            }
            for r in runners
            if r.get("symbol")
        ]
        if rows:
            db.table("prev_runners").upsert(rows, on_conflict="date,symbol").execute()
        return True
    except Exception as exc:
        logger.warning("Supabase save_runners failed: %s", exc)
        return False


def load_runners_db() -> tuple[str, list[dict]] | None:
    """Return (date_str, runners) from Supabase, or None on failure."""
    db = get_db()
    if db is None:
        return None
    try:
        res = db.table("prev_runners").select("*").order("date", desc=True).limit(200).execute()
        rows = res.data or []
        if not rows:
            return "", []
        date_str = rows[0]["date"]
        runners = [
            {"symbol": r["symbol"], "pchange": r["pchange"], "close": r["close"]}
            for r in rows
            if r["date"] == date_str
        ]
        return date_str, runners
    except Exception as exc:
        logger.warning("Supabase load_runners failed: %s", exc)
        return None


# ── Alert Logs ────────────────────────────────────────────────────────────────

def log_report_sent(report_type: str, summary: str = "", details: dict | None = None) -> None:
    """Append a record to alert_logs. Fire-and-forget — never raises."""
    db = get_db()
    if db is None:
        return
    try:
        db.table("alert_logs").insert({
            "report_type": report_type,
            "summary":     summary,
            "details":     details or {},
        }).execute()
    except Exception as exc:
        logger.warning("Supabase log_report failed: %s", exc)
