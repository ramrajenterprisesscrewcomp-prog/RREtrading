"""Send already-generated RRE_Report_Today.pdf via fallback Telegram bot."""
import asyncio, httpx, sys
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

PDF_PATH   = r"C:\Users\Admin\Desktop\RRE_Report_Today.pdf"
OLD_TOKEN  = "8034277441:AAFlq-BqwUswIjK08EUKZeSqKORIt_H8vFw"
OLD_CHAT   = "-5244782085"
NEW_TOKEN  = "8291092862:AAEou4Jz8OPom5uYxiFPcufZQZo_3tHIcEc"
NEW_CHAT   = "-5073639718"

async def send_pdf(token, chat, pdf_bytes, filename, caption):
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(url, data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                             files={"document": (filename, pdf_bytes, "application/pdf")})
            print(f"  [{chat}] HTTP {r.status_code}: {r.text[:200]}")
            return r.status_code == 200
    except Exception as e:
        print(f"  Error: {e}")
        return False

async def main():
    with open(PDF_PATH, "rb") as f:
        pdf_bytes = f.read()
    print(f"PDF loaded: {len(pdf_bytes)//1024} KB")

    date_str = datetime.now().strftime("%d %b %Y")
    filename = f"RRE_PM_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    caption  = (
        f"<b>RRE 2:45 PM Positional Report -- {date_str}</b>\n\n"
        f"Long Buildup, Breakouts, Pullbacks, Tomorrow's Runners\n"
        f"AI analysis included in PDF"
    )

    print("Trying NEW bot (@open_nifty_bot)...")
    ok1 = await send_pdf(NEW_TOKEN, NEW_CHAT, pdf_bytes, filename, caption)
    print(f"  New bot: {'OK' if ok1 else 'FAILED'}")

    if not ok1:
        print("\nTrying OLD bot fallback...")
        ok2 = await send_pdf(OLD_TOKEN, OLD_CHAT, pdf_bytes, filename, caption)
        print(f"  Old bot: {'OK' if ok2 else 'FAILED'}")
        if not ok2:
            print("\nBoth failed. To fix:")
            print("  1. Open your Telegram group")
            print("  2. Add @open_nifty_bot as member")
            print("  3. Run: python run_report_now.py")

asyncio.run(main())
