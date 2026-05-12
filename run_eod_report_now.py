"""One-shot: generate today's 4:00 PM EOD report PDF and send to Telegram."""
import asyncio, sys
sys.path.insert(0, '.')

async def run():
    from services.nse_service import get_nifty500_ohlc, get_fno_oi_buildup, get_sector_rotation_multi
    from services.tsr_service import get_tsr_buildup
    from services.tomorrow_scanner_service import scan_tomorrow_runners
    from services.pm_report_service import _normalize_tsr_buildup
    from services.eod_report_service import (
        get_top_gainers_losers, get_high_delivery_stocks,
        get_eod_watchlist, _generate_eod_ai, _build_eod_pdf, _safe_float,
    )
    from datetime import datetime

    print("Fetching NSE + TSR + Runners data...")
    stocks, buildup_raw, tsr_bu, runners = await asyncio.gather(
        get_nifty500_ohlc(),
        get_fno_oi_buildup(15),
        get_tsr_buildup(),
        scan_tomorrow_runners(),
    )
    stocks  = stocks or []
    runners = runners or []

    # Merge long buildup
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

    # Sector data
    sector_data = {}
    try:
        sec_raw = await get_sector_rotation_multi()
        for item in (sec_raw or []):
            name = item.get("index", item.get("name", ""))
            pct  = _safe_float(item.get("pChange", item.get("pct", 0)))
            if name:
                sector_data[name] = pct
    except Exception as e:
        print(f"  Sector data skipped: {e}")

    gainers, losers = get_top_gainers_losers(stocks, 15)
    delivery        = get_high_delivery_stocks(stocks, 12)
    watchlist       = get_eod_watchlist(stocks, long_buildup, runners)

    print(f"  Stocks:{len(stocks)} Gainers:{len(gainers)} Losers:{len(losers)} "
          f"LB:{len(long_buildup)} Watchlist:{len(watchlist)} Runners:{len(runners)}")

    print("Generating AI EOD analysis...")
    ai_text = await asyncio.to_thread(
        _generate_eod_ai, gainers, losers, long_buildup, runners, watchlist, sector_data
    )

    print("Building PDF...")
    date_str  = datetime.now().strftime("%d %b %Y")
    pdf_bytes = await asyncio.to_thread(
        _build_eod_pdf,
        date_str, gainers, losers, long_buildup, runners,
        watchlist, delivery, sector_data, ai_text,
    )

    out_path = r"C:\Users\Admin\Desktop\RRE_EOD_Report_Today.pdf"
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)
    print(f"\nPDF saved: {out_path}  ({len(pdf_bytes)//1024} KB)")

    # Send via Telegram — try new bot first, fallback to old bot
    top_g  = [s.get("symbol","") for s in gainers[:5]]
    top_l  = [s.get("symbol","") for s in losers[:5]]
    hi_run = [r["symbol"] for r in runners if r.get("grade") == "HIGH"][:4]
    wl_top = [w["symbol"] for w in watchlist[:5]]
    caption = (
        f"<b>RRE 4:00 PM End-of-Day Report -- {date_str}</b>\n\n"
        f"TOP GAINERS ({len(gainers)}): {' . '.join(top_g)}\n"
        f"TOP LOSERS  ({len(losers)}): {' . '.join(top_l)}\n"
        f"F&O Long Buildup: {len(long_buildup)} stocks\n"
        f"Tomorrow HIGH Runners: {' . '.join(hi_run) or 'None'}\n"
        f"Tomorrow Watchlist: {' . '.join(wl_top) or 'None'}\n\n"
        "AI EOD analysis + Tomorrow strategy included in PDF"
    )
    filename = f"RRE_EOD_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"

    import httpx

    async def _send(token, chat):
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(url,
                data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                files={"document": (filename, pdf_bytes, "application/pdf")})
            return r.status_code == 200, r.text[:120]

    NEW_TOKEN = "8291092862:AAEou4Jz8OPom5uYxiFPcufZQZo_3tHIcEc"
    NEW_CHAT  = "-5073639718"
    OLD_TOKEN = "8034277441:AAFlq-BqwUswIjK08EUKZeSqKORIt_H8vFw"
    OLD_CHAT  = "-5244782085"

    print("Sending to Telegram...")
    ok, txt = await _send(NEW_TOKEN, NEW_CHAT)
    if ok:
        print("  New bot: OK")
    else:
        print(f"  New bot FAILED: {txt}")
        ok, txt = await _send(OLD_TOKEN, OLD_CHAT)
        print(f"  Old bot: {'OK' if ok else 'FAILED - ' + txt}")

asyncio.run(run())
