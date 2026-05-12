import httpx
import asyncio
import logging
from datetime import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


async def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a text message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{_BASE}/sendMessage", json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     text,
                "parse_mode":               parse_mode,
                "disable_web_page_preview": True,
            })
            ok = r.json().get("ok", False)
            if not ok:
                logger.warning("Telegram sendMessage failed: %s", r.text)
            return ok
    except Exception as exc:
        logger.warning("Telegram error: %s", exc)
        return False


async def send_document(
    file_bytes: bytes,
    filename: str,
    caption: str = "",
    parse_mode: str = "HTML",
) -> bool:
    """Send a document (e.g. PDF) to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{_BASE}/sendDocument",
                data={
                    "chat_id":    TELEGRAM_CHAT_ID,
                    "caption":    caption[:1024],
                    "parse_mode": parse_mode,
                },
                files={"document": (filename, file_bytes, "application/pdf")},
            )
            ok = r.json().get("ok", False)
            if not ok:
                logger.warning("Telegram sendDocument failed: %s", r.text)
            return ok
    except Exception as exc:
        logger.warning("Telegram sendDocument error: %s", exc)
        return False


async def send_market_summary(overview: dict) -> None:
    """Send daily market summary (gainers, buildup counts, index)."""
    indices = overview.get("indices", {})
    nifty   = indices.get("nifty50", {})
    bn      = indices.get("banknifty", {})
    buildup = overview.get("buildup", {})
    gainers = overview.get("gainers", [])[:3]
    losers  = overview.get("losers",  [])[:3]

    lb = len(buildup.get("Long Buildup",   []))
    sb = len(buildup.get("Short Buildup",  []))
    sc = len(buildup.get("Short Covering", []))
    lc = len(buildup.get("Long Covering",  []))

    def fmt_idx(d):
        v = d.get("value")
        p = d.get("pchange", 0)
        return f"{v:,.0f} ({'+' if p >= 0 else ''}{p:.2f}%)" if v else "N/A"

    top_g = " | ".join(
        f"{s['symbol']} {'+' if s['pChange'] >= 0 else ''}{s['pChange']:.1f}%" for s in gainers
    )
    top_l = " | ".join(f"{s['symbol']} {s['pChange']:.1f}%" for s in losers)

    msg = (
        f"<b>📊 RRE Market Summary — {datetime.now().strftime('%d %b %Y %H:%M')}</b>\n\n"
        f"<b>NIFTY 50:</b> {fmt_idx(nifty)}\n"
        f"<b>Bank Nifty:</b> {fmt_idx(bn)}\n\n"
        f"<b>F&O Signals:</b> 🟢 Long Buildup: {lb} | 🔴 Short Buildup: {sb} | "
        f"🟩 Short Covering: {sc} | 🟧 Long Covering: {lc}\n\n"
        f"<b>Top Gainers:</b> {top_g or 'N/A'}\n"
        f"<b>Top Losers:</b> {top_l or 'N/A'}"
    )
    await send_message(msg)
