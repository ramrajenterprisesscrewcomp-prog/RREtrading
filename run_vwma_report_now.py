"""
run_vwma_report_now.py
Trigger VWMA(20) + candle pattern report immediately.
Uses TSR stock universe (weekly/monthly support + long buildup + signals).
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main():
    from services.tsr_service import start_background_fetcher, get_tsr_all
    import time

    print("Starting TSR fetcher...")
    start_background_fetcher()

    # Wait up to 3 minutes for TSR cache to populate
    for i in range(36):
        tsr = await get_tsr_all()
        total = (
            len(tsr.get("weekly_support", [])) +
            len(tsr.get("monthly_support", [])) +
            len(tsr.get("signals", [])) +
            len(tsr.get("buildup", {}).get("long_buildup", []))
        )
        if total > 0:
            print(f"TSR data ready: {total} rows")
            break
        print(f"  Waiting for TSR... ({i+1}/36)")
        await asyncio.sleep(5)
    else:
        print("TSR data not available after 3 min — sending with empty universe")

    print("Running VWMA candle scan...")
    from services.vwma_candle_service import send_vwma_candle_report
    await send_vwma_candle_report()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
