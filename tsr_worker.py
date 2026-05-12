"""
tsr_worker.py — standalone Playwright scraper, called as a subprocess.
Outputs JSON to stdout. Credentials passed as CLI args.
Run: python tsr_worker.py <email> <password>
"""
import asyncio
import json
import sys
from pathlib import Path

_SESSION_FILE = Path(".tsr_session.json")
TSR_BASE      = "https://www.topstockresearch.com"
TSR_LOGIN_URL = f"{TSR_BASE}/my/UserManagement?act=login"

_SCREENERS = {
    "long_buildup": (
        f"{TSR_BASE}/rt/Screener/FuturesAndOptions/Futures"
        "/StockFuturesAccumulation/PositionBuildUpStockNearMonthExpiryDate"
    ),
    "short_buildup": (
        f"{TSR_BASE}/rt/Screener/FuturesAndOptions/Futures"
        "/StockFuturesFreshShorts/FreshShortsStockNearMonthExpiryDate"
    ),
    "short_covering": (
        f"{TSR_BASE}/rt/Screener/FuturesAndOptions/Futures"
        "/StockFuturesShortCovering/ShortCoveringStockNearMonthExpiryDate"
    ),
    "pcr": (
        f"{TSR_BASE}/rt/Screener/FuturesAndOptions/PutCallRatio"
        "/HighPCROpenInterest/HighOIPCRNearMonthExpiryDate"
    ),
    "signals": (
        f"{TSR_BASE}/rt/Screener/ExpertScreener"
        "/PriceActionBased/Breakout/30DaysResistanceBreakout"
    ),
    "weekly_support": (
        f"{TSR_BASE}/rt/Screener/ExpertScreener"
        "/SupportAndResistance/WeeklySupport/StocksNearWeeklySupport"
    ),
    "monthly_support": (
        f"{TSR_BASE}/rt/Screener/ExpertScreener"
        "/SupportAndResistance/MonthlySupport/StocksNearMonthlySupport"
    ),
}

_EMAIL_SELS  = ['input[name="email"]', 'input[name="username"]',
                'input[name="loginid"]', 'input[type="email"]']
_PASS_SELS   = ['input[name="password"]', 'input[name="passwd"]',
                'input[type="password"]']
_SUBMIT_SELS = ['button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Login")', 'button:has-text("Sign In")']
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _is_login(url):
    return "UserManagement" in url or "login" in url.lower()


async def main(email, password):
    from playwright.async_api import async_playwright

    result = {"buildup": {}, "pcr": [], "signals": [], "weekly_support": [], "monthly_support": []}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=_UA)

        # Restore session cookies
        if _SESSION_FILE.exists():
            try:
                await ctx.add_cookies(json.loads(_SESSION_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass

        page = await ctx.new_page()

        # Check session / login
        try:
            await page.goto(list(_SCREENERS.values())[0],
                            wait_until="networkidle", timeout=20000)
        except Exception:
            pass

        if _is_login(page.url):
            # Log in
            logged_in = False
            try:
                await page.goto(TSR_LOGIN_URL, wait_until="networkidle", timeout=30000)
                for sel in _EMAIL_SELS:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(email)
                        break
                for sel in _PASS_SELS:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(password)
                        break
                for sel in _SUBMIT_SELS:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        break
                await page.wait_for_load_state("networkidle", timeout=20000)
                if not _is_login(page.url):
                    logged_in = True
                    # Save cookies
                    _SESSION_FILE.write_text(
                        json.dumps(await ctx.cookies()), encoding="utf-8"
                    )
            except Exception as e:
                print(f"LOGIN_ERROR: {e}", file=sys.stderr)

            if not logged_in:
                await browser.close()
                print(json.dumps({}))
                return

        # Scrape each screener
        for key, url in _SCREENERS.items():
            rows = []
            try:
                await page.goto(url, wait_until="networkidle", timeout=25000)
                if not _is_login(page.url):
                    data_table = None
                    for t in await page.query_selector_all("table"):
                        trows = await t.query_selector_all("tr")
                        if not trows:
                            continue
                        first = (await trows[0].inner_text()).strip().lower()
                        if any(kw in first for kw in ("code", "symbol", "name\t", "name ")):
                            data_table = t
                            break
                    if data_table:
                        all_rows = await data_table.query_selector_all("tr")
                        headers = [
                            h.replace("\n", " ").strip()
                            for h in await all_rows[0].eval_on_selector_all(
                                "td, th", "els => els.map(e => e.innerText.trim())"
                            )
                        ]
                        for tr in all_rows[1:16]:
                            cells = await tr.eval_on_selector_all(
                                "td", "els => els.map(e => e.innerText.trim())"
                            )
                            if cells and cells[0]:
                                row = {k: v for k, v in zip(headers, cells) if k}
                                if row:
                                    rows.append(row)
            except Exception as e:
                print(f"SCRAPE_ERROR {key}: {e}", file=sys.stderr)

            if key in ("long_buildup", "short_buildup", "short_covering"):
                result["buildup"][key] = rows
            elif key == "pcr":
                result["pcr"] = rows
            elif key == "weekly_support":
                result["weekly_support"] = rows
            elif key == "monthly_support":
                result["monthly_support"] = rows
            else:
                result["signals"] = rows

        await browser.close()

    print(json.dumps(result))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({}))
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
