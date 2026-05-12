"""One-shot: generate today's 2:45 PM report PDF and save to Desktop."""
import asyncio, sys
sys.path.insert(0, '.')

async def run():
    from services.nse_service import get_nifty500_ohlc, get_fno_oi_buildup
    from services.tsr_service import get_tsr_buildup
    from services.tomorrow_scanner_service import scan_tomorrow_runners
    from services.pm_report_service import (
        scan_consolidation_breakouts, scan_pullback_stocks,
        _normalize_tsr_buildup, _generate_ai_picks, _build_pm_pdf,
    )
    from datetime import datetime

    print("Fetching NSE + TSR data...")
    stocks, buildup_raw, tsr_bu = await asyncio.gather(
        get_nifty500_ohlc(),
        get_fno_oi_buildup(15),
        get_tsr_buildup(),
    )

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
    breakouts    = scan_consolidation_breakouts(stocks or [])
    pullbacks    = scan_pullback_stocks(stocks or [])

    print(f"  Stocks: {len(stocks or [])}  Long Buildup: {len(long_buildup)}  "
          f"Breakouts: {len(breakouts)}  Pullbacks: {len(pullbacks)}")

    runners = []
    try:
        print("Scanning tomorrow's runners...")
        runners = await scan_tomorrow_runners()
        print(f"  Runners: {len(runners)}")
    except Exception as e:
        print(f"  Runners skipped: {e}")

    print("Generating AI analysis...")
    ai_text = await asyncio.to_thread(
        _generate_ai_picks, long_buildup, breakouts, pullbacks, runners
    )

    print("Building PDF...")
    date_str = datetime.now().strftime("%d %b %Y")
    pdf_bytes = await asyncio.to_thread(
        _build_pm_pdf, date_str, long_buildup, breakouts, pullbacks, runners, ai_text
    )

    out_path = r"C:\Users\Admin\Desktop\RRE_Report_Today.pdf"
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)

    print(f"\nPDF saved to: {out_path}")
    print(f"Size: {len(pdf_bytes)//1024} KB")
    print(f"Sections: Long Buildup={len(long_buildup)} | Breakouts={len(breakouts)} | "
          f"Pullbacks={len(pullbacks)} | Runners={len(runners)}")

    # Try sending via Telegram
    try:
        from services.telegram_service import send_document
        caption_lines = [
            f"📊 <b>RRE 2:45 PM Positional Report — {date_str}</b>",
            "",
            f"🟢 <b>F&O Long Buildup</b>: {len(long_buildup)} stocks",
            f"  {' · '.join(r.get('symbol','') for r in long_buildup[:6])}",
            f"⚡ <b>Breakouts</b>: {len(breakouts)}  ·  🔄 <b>Pullbacks</b>: {len(pullbacks)}",
            f"🎯 <b>Tomorrow's Runners</b>: {len(runners)}",
        ]
        high_r = [r["symbol"] for r in runners if r.get("grade") == "HIGH"][:5]
        if high_r:
            caption_lines.append(f"  ⭐⭐⭐ HIGH: {' · '.join(high_r)}")
        caption_lines.append("\n🤖 AI analysis included in PDF")

        filename = f"RRE_PM_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        ok = await send_document(pdf_bytes, filename, "\n".join(caption_lines))
        print(f"Telegram send: {'OK' if ok else 'FAILED — add @open_nifty_bot to group first'}")
    except Exception as e:
        print(f"Telegram error: {e}")

asyncio.run(run())
