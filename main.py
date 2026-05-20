import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from cachetools import TTLCache

from config import CACHE_TTL_STOCK, CACHE_TTL_SCREENER, CACHE_TTL_NEWS
from services.nse_service import (
    get_equity_quote,
    get_trade_info,
    check_is_fno,
    get_market_status,
    get_nifty_membership,
    get_sector_index_pe,
    get_option_chain,
    parse_option_chain,
    get_fno_bhavcopy_data,
    get_index_quotes,
    get_market_movers,
    get_fno_oi_buildup,
    get_nifty_oi_analysis,
    get_breakout_stocks,
    get_sector_rotation,
    get_nifty_oi_for_expiry,
    get_sector_rotation_multi,
)
from services.news_service import get_stock_news, get_policy_news, REFERENCE_SITES
from services.screener_service import scrape_screener
from services.technical_service import get_technical_data
from services.global_markets_service import get_global_markets

from services.telegram_service import send_market_summary
from services.candle_service import candle_alert_loop
from services.angel_service import get_fno_lot_and_margin_async
from utils.helpers import classify_oi_buildup, compute_max_pain, safe_float, compute_checklist

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(candle_alert_loop())
    from services.tsr_service import start_background_fetcher
    start_background_fetcher()
    from services.pm_report_service import pm_report_loop
    asyncio.create_task(pm_report_loop())
    from services.eod_report_service import eod_report_loop
    asyncio.create_task(eod_report_loop())
    from services.pivot_scanner_service import pivot_alert_loop
    asyncio.create_task(pivot_alert_loop())
    from services.weekly_report_service import weekly_report_loop
    asyncio.create_task(weekly_report_loop())
    yield


app = FastAPI(title="RRE Stock Analysis Dashboard", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_stock_cache:    TTLCache = TTLCache(maxsize=50,  ttl=CACHE_TTL_STOCK)
_screener_cache: TTLCache = TTLCache(maxsize=50,  ttl=CACHE_TTL_SCREENER)
_news_cache:     TTLCache = TTLCache(maxsize=100, ttl=CACHE_TTL_NEWS)
_overview_cache: TTLCache = TTLCache(maxsize=1,   ttl=5)


@app.get("/", response_class=HTMLResponse)
async def serve_home():
    html_path = Path(__file__).parent / "static" / "home.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/analyze", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


async def _build_overview_data() -> dict:
    """Fetch and cache market overview data (TTL=5s). Shared by REST + SSE."""
    if "d" in _overview_cache:
        return _overview_cache["d"]

    errors: dict[str, str] = {}

    async def _safe(coro, key: str, default):
        try:
            return await coro
        except Exception as exc:
            errors[key] = str(exc)
            return default

    indices, movers, buildup, nifty_oi, mkt_status, sectors = await asyncio.gather(
        _safe(get_index_quotes(),      "indices",    {}),
        _safe(get_market_movers(7),    "movers",     {"gainers": [], "losers": []}),
        _safe(get_fno_oi_buildup(10),  "buildup",    {}),
        _safe(get_nifty_oi_analysis(), "nifty_oi",   {}),
        _safe(get_market_status(),     "mkt_status", {}),
        _safe(get_sector_rotation(),   "sectors",    []),
    )

    data = {
        "indices":        indices,
        "gainers":        movers.get("gainers", []),
        "losers":         movers.get("losers",  []),
        "buildup":        buildup,
        "nifty_oi":       nifty_oi,
        "sectors":        sectors,
        "market_is_open": mkt_status.get("is_open", False),
        "trade_date":     mkt_status.get("trade_date", ""),
        "ai_summary":     "",
        "errors":         errors,
        "timestamp":      datetime.now().isoformat(),
    }
    _overview_cache["d"] = data
    return data


@app.get("/api/market-overview")
async def market_overview():
    return JSONResponse(content=await _build_overview_data())


@app.get("/api/live-stream")
async def live_stream(request: Request):
    """SSE endpoint — pushes market overview data every 5 s when cache refreshes."""
    async def generate():
        last_hash = ""
        # Push immediately on connect
        try:
            data = await _build_overview_data()
            last_hash = hashlib.md5(data["timestamp"].encode()).hexdigest()[:8]
            yield f"data: {json.dumps(data)}\n\n"
        except Exception:
            pass

        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(5)
            if await request.is_disconnected():
                break
            try:
                data = await _build_overview_data()
                h = hashlib.md5(data["timestamp"].encode()).hexdigest()[:8]
                if h != last_hash:
                    last_hash = h
                    yield f"data: {json.dumps(data)}\n\n"
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'msg': str(exc)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/global-markets")
async def global_markets_api():
    try:
        data = await get_global_markets()
        return JSONResponse(content=data)
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@app.get("/api/analyze/{symbol}")
async def analyze_stock(symbol: str):
    symbol = symbol.upper().strip()
    if not all(c.isalnum() or c in "-&" for c in symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol format")

    if symbol in _stock_cache:
        return JSONResponse(content=_stock_cache[symbol])

    errors: dict[str, str] = {}

    async def safe(coro, key: str):
        try:
            return await coro
        except Exception as exc:
            errors[key] = str(exc)
            logger.warning("Fetch error [%s]: %s", key, exc)
            return {}

    async def safe_thread(fn, *args, key: str):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as exc:
            errors[key] = str(exc)
            logger.warning("Thread error [%s]: %s", key, exc)
            return {}

    # ── PHASE 1: parallel independent fetches ─────────────────────────────
    screener_data = _screener_cache.get(symbol)
    news_cache    = _news_cache.get(symbol)

    phase1_coros = [
        safe(get_equity_quote(symbol),   "nse_quote"),
        safe(get_trade_info(symbol),     "trade_info"),
        safe(check_is_fno(symbol),       "fno_check"),
        safe(get_market_status(),        "market_status"),
        safe(get_nifty_membership(symbol), "nifty_index"),
        safe(get_technical_data(symbol), "technical"),
    ]
    if screener_data is None:
        phase1_coros.append(safe_thread(scrape_screener, symbol, key="screener"))

    results = await asyncio.gather(*phase1_coros)

    idx = 0
    quote          = results[idx] or {}; idx += 1
    trade_resp     = results[idx] or {}; idx += 1
    is_fno         = results[idx] if isinstance(results[idx], bool) else False; idx += 1
    mkt_status     = results[idx] if isinstance(results[idx], dict) else {}; idx += 1
    market_is_open = mkt_status.get("is_open", False)
    nifty_indices  = results[idx] if isinstance(results[idx], list) else []; idx += 1
    tech_data      = results[idx] if isinstance(results[idx], dict) else {}; idx += 1

    if screener_data is None:
        screener_data = results[idx] or {}; idx += 1
        _screener_cache[symbol] = screener_data

    # ── Extract NSE quote fields ───────────────────────────────────────────
    price_info    = quote.get("priceInfo", {})
    metadata      = quote.get("metadata", {})
    industry_info = quote.get("industryInfo", {})
    info          = quote.get("info", {})
    week_hl       = price_info.get("weekHighLow", {})
    intraday_hl   = price_info.get("intraDayHighLow", {})

    ti = (
        trade_resp.get("marketDeptOrderBook", {}).get("tradeInfo", {})
        or quote.get("marketDeptOrderBook", {}).get("tradeInfo", {})
    )

    company_name = (
        metadata.get("companyName")
        or info.get("companyName")
        or info.get("symbol", symbol)
    )
    sector        = industry_info.get("sector", "")
    industry      = industry_info.get("industry", "")
    macro_sector  = industry_info.get("macro", "")
    basic_industry = industry_info.get("basicIndustry", "")
    listing_date  = metadata.get("listingDate", "")

    screener_name = screener_data.get("__company_name")
    if screener_name and (not company_name or company_name == symbol):
        company_name = screener_name

    # ── PHASE 2: F&O + news + lot/margin (concurrent) ─────────────────────
    phase2_tasks = []
    if is_fno:
        phase2_tasks.append(safe(get_option_chain(symbol),           "option_chain"))
        phase2_tasks.append(safe(get_fno_bhavcopy_data(symbol),      "bhavcopy"))
        phase2_tasks.append(safe(get_fno_lot_and_margin_async(symbol), "lot_margin"))

    if news_cache is None:
        phase2_tasks.append(safe(get_stock_news(symbol, company_name), "news"))
        phase2_tasks.append(safe(get_policy_news(sector, company_name), "policy_news"))

    phase2_results = await asyncio.gather(*phase2_tasks)

    p2 = 0
    oc_raw     = {}
    bhav       = {}
    lot_margin = {}
    if is_fno:
        oc_raw     = phase2_results[p2] or {}; p2 += 1
        bhav       = phase2_results[p2] or {}; p2 += 1
        lot_margin = phase2_results[p2] or {}; p2 += 1

    if news_cache is None:
        news_data   = phase2_results[p2] if isinstance(phase2_results[p2], list) else []; p2 += 1
        policy_news = phase2_results[p2] if isinstance(phase2_results[p2], list) else []; p2 += 1
        _news_cache[symbol] = (news_data, policy_news)
    else:
        news_data, policy_news = news_cache

    # ── Determine best F&O data source ────────────────────────────────────
    # Try live option chain first; fall back to bhavcopy if empty
    oc = parse_option_chain(oc_raw) if oc_raw else {}
    fno_data_source = "Live" if oc else ""
    # Lot size from Angel One instrument master (reliable, always available)
    lot_size = int(lot_margin.get("lot_size") or oc.get("market_lot_size") or 0)

    if not oc and bhav:
        # Use OI spurts EOD data — map to same keys as parse_option_chain
        # total_call_oi / total_put_oi are None here (OI spurts has only futures OI)
        oc = {
            "source":            bhav.get("source", "EOD"),
            "source_date":       bhav.get("source_date", ""),
            "total_call_oi":     bhav.get("total_call_oi"),   # None for EOD spurts
            "total_put_oi":      bhav.get("total_put_oi"),    # None for EOD spurts
            "total_fut_oi":      bhav.get("total_fut_oi", 0),
            "pcr_oi":            bhav.get("pcr_oi"),
            "oi_change_pct":     bhav.get("oi_change_pct", 0),
            "expiry_dates":      bhav.get("expiry_dates", []),
            "strike_prices":     [],
            "option_chain_data": [],
            "underlying_value":  bhav.get("futures_ltp", 0),
            "futures_settle":    bhav.get("futures_settle"),
        }
        fno_data_source = f"EOD - {bhav.get('source_date', '')}"

    # ── Process F&O metrics ────────────────────────────────────────────────
    oi_change_pct  = oc.get("oi_change_pct", 0)
    price_change   = price_info.get("pChange", 0) or 0
    buildup_signal = classify_oi_buildup(oi_change_pct, price_change) if (is_fno and oc) else None

    # Max pain only possible with live option chain data
    max_pain = compute_max_pain(
        oc.get("option_chain_data", []),
        oc.get("strike_prices", [])
    ) if (is_fno and oc.get("option_chain_data")) else None

    # Prefer call+put OI sum; fall back to futures OI when option chain unavailable
    call_oi = oc.get("total_call_oi") or 0
    put_oi  = oc.get("total_put_oi") or 0
    total_oi = (call_oi + put_oi) if (call_oi or put_oi) else (oc.get("total_fut_oi") or 0)

    # ── Lot size, contract value & margin (from Angel One) ────────────────
    # contract_value: use Angel One LTP-based value if available, else NSE futures price
    contract_value  = lot_margin.get("contract_value")
    margin_required = lot_margin.get("margin_required")
    if not contract_value and lot_size:
        fut_px = float(oc.get("underlying_value") or (bhav.get("futures_ltp") if bhav else 0) or price_info.get("lastPrice") or 0)
        if fut_px:
            contract_value = round(lot_size * fut_px, 2)

    # ── Screener fundamentals ──────────────────────────────────────────────
    market_cap = safe_float(screener_data.get("Market Cap"))
    pe_ratio   = safe_float(screener_data.get("Stock P/E"))
    book_value = safe_float(screener_data.get("Book Value"))
    roe        = safe_float(screener_data.get("ROE"))
    roce       = safe_float(screener_data.get("ROCE"))
    div_yield  = safe_float(screener_data.get("Dividend Yield"))
    quarterly_results = screener_data.get("quarterly_results_raw", [])

    # ── Sector valuation via NSE index PE ─────────────────────────────────
    try:
        sector_index_pe, sector_index_name = await get_sector_index_pe(sector, industry)
    except Exception as exc:
        errors["sector_pe"] = str(exc)
        sector_index_pe, sector_index_name = None, ""

    valuation_vs_sector = None
    if sector_index_pe and pe_ratio:
        ratio = pe_ratio / sector_index_pe
        valuation_vs_sector = (
            "Expensive"  if ratio > 1.3 else
            "Premium"    if ratio > 1.1 else
            "Discount"   if ratio < 0.85 else
            "Fair Value"
        )

    peers: list = []   # Screener peer table is JS-rendered; kept as empty list

    # ── Investment Checklist ───────────────────────────────────────────────
    checklist_input = {
        **screener_data,
        "market_cap":   market_cap,
        "pe_ratio":     pe_ratio,
        "book_value":   book_value,
        "roe":          roe,
        "roce":         roce,
        "last_price":   price_info.get("lastPrice"),
        "delivery_pct": ti.get("deliveryToTradedQuantity"),
        "sector_index_pe": sector_index_pe,
        "technical":    tech_data if tech_data else {},
    }
    try:
        checklist = compute_checklist(checklist_input)
    except Exception as exc:
        errors["checklist"] = str(exc)
        checklist = {}

    # ── Build response ─────────────────────────────────────────────────────
    response_data = {
        "symbol":        symbol,
        "company_name":  company_name,
        "sector":        sector,
        "industry":      industry,
        "macro_sector":  macro_sector,
        "basic_industry": basic_industry,
        "listing_date":  listing_date,
        # Price
        "last_price":   price_info.get("lastPrice"),
        "change":       price_info.get("change"),
        "pchange":      price_info.get("pChange"),
        "open":         price_info.get("open"),
        "high":         intraday_hl.get("max"),
        "low":          intraday_hl.get("min"),
        "close":        price_info.get("close"),
        "week_52_high": week_hl.get("max"),
        "week_52_low":  week_hl.get("min"),
        "week_52_high_date": week_hl.get("maxDate"),
        "week_52_low_date":  week_hl.get("minDate"),
        # Volume
        "volume":             ti.get("totalTradedVolume"),
        "delivery_pct":       ti.get("deliveryToTradedQuantity"),
        "total_traded_value": ti.get("totalTradedValue"),
        # Fundamentals
        "market_cap": market_cap,
        "pe_ratio":   pe_ratio,
        "book_value": book_value,
        "roe":        roe,
        "roce":       roce,
        "div_yield":  div_yield,
        # Market status
        "market_is_open":  market_is_open,
        "market_trade_date": mkt_status.get("trade_date", ""),
        # F&O
        "is_fno":           is_fno,
        "fno_data_source":  fno_data_source,
        "lot_size":         lot_size if is_fno else None,
        "contract_value":   contract_value if is_fno else None,
        "margin_required":  margin_required if is_fno else None,
        "fut_expiry":       lot_margin.get("expiry", "") if is_fno else None,
        "open_interest":    total_oi if is_fno else None,
        "oi_change_pct":    oi_change_pct if is_fno else None,
        "oi_change_abs":    bhav.get("oi_change") if bhav else None,
        "pcr":              oc.get("pcr_oi") if is_fno else None,
        "buildup_signal":   buildup_signal,
        "max_pain":         max_pain,
        "futures_ltp":      oc.get("underlying_value") or (bhav.get("futures_ltp") if bhav else None),
        "futures_settle":   bhav.get("futures_settle") if bhav else None,
        "option_chain_summary": {
            "expiry_date":      (oc.get("expiry_dates") or [""])[0],
            "underlying_value": oc.get("underlying_value"),
            "total_call_oi":    oc.get("total_call_oi"),
            "total_put_oi":     oc.get("total_put_oi"),
            "pcr_oi":           oc.get("pcr_oi"),
            "max_pain":         max_pain,
            "source":           fno_data_source,
        } if is_fno else None,
        # Earnings
        "quarterly_results": quarterly_results,
        # Company overview
        "about":               screener_data.get("__about", ""),
        "nifty_indices":       nifty_indices,
        # Sector valuation
        "peers":               peers,
        "sector_index_pe":     sector_index_pe,
        "sector_index_name":   sector_index_name,
        "valuation_vs_sector": valuation_vs_sector,
        # Technical indicators
        "technical": tech_data if tech_data else None,
        # Investment checklist
        "checklist": checklist,
        # News
        "news":        news_data,
        "policy_news": policy_news,
        # Meta
        "reference_sites": REFERENCE_SITES,
        "errors":          errors,
        "data_timestamp":  datetime.now().isoformat(),
    }

    response_data["ai_summary"]   = ""
    response_data["ai_sentiment"] = "Neutral"
    response_data["ai_risks"]     = []
    response_data["ai_catalysts"] = []

    _stock_cache[symbol] = response_data
    return JSONResponse(content=response_data)


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/references")
async def references():
    return {"sites": REFERENCE_SITES}


@app.get("/api/fno-list")
async def fno_list():
    from services.nse_service import get_fno_symbols
    syms = await get_fno_symbols()
    return {"count": len(syms), "symbols": syms}


@app.get("/api/nifty-oi")
async def nifty_oi_endpoint(symbol: str = "NIFTY", expiry: str = ""):
    data = await get_nifty_oi_for_expiry(symbol, expiry)
    return JSONResponse(content=data)


@app.post("/api/send-market-update")
async def trigger_market_update():
    if "d" in _overview_cache:
        await send_market_summary(_overview_cache["d"])
        return {"status": "sent"}
    return {"status": "no data cached"}


@app.post("/api/send-report")
async def trigger_daily_report():
    """Manually trigger the full EOD PDF report and send via Telegram."""
    try:
        from services.candle_service import scan_and_send
        asyncio.create_task(scan_and_send())
        return {"status": "started", "message": "Report generation started — will be sent to Telegram shortly"}
    except Exception as exc:
        return JSONResponse(content={"status": "error", "error": str(exc)}, status_code=500)



@app.get("/api/sector-rotation-multi")
async def sector_rotation_multi_endpoint():
    try:
        data = await get_sector_rotation_multi()
        return JSONResponse(content={"sectors": data, "timestamp": datetime.now().isoformat()})
    except Exception as exc:
        return JSONResponse(content={"sectors": [], "error": str(exc)}, status_code=500)


@app.get("/api/tsr-data")
async def tsr_data_endpoint():
    """Fetch TSR Pro screener data in one browser session: buildup, PCR, signals."""
    try:
        from services.tsr_service import get_tsr_all
        data = await get_tsr_all()
        return JSONResponse(content={
            "buildup": data.get("buildup", {}),
            "pcr":     data.get("pcr", []),
            "signals": data.get("signals", []),
            "source":  "TSR Pro",
        })
    except Exception as exc:
        return JSONResponse(content={"buildup": {}, "pcr": [], "signals": [], "error": str(exc)})


@app.get("/api/volume-breakout")
async def volume_breakout_endpoint():
    """Nifty 500 stocks at multi-year highs with volume buzz analysis."""
    try:
        from services.volume_breakout_service import scan_volume_breakouts
        results = await scan_volume_breakouts()
        return JSONResponse(content={"results": results, "count": len(results)})
    except Exception as exc:
        return JSONResponse(content={"results": [], "count": 0, "error": str(exc)})


@app.get("/api/sector-analysis")
async def sector_analysis_endpoint(force: bool = False):
    """5-factor sector rotation analysis: RS, RRG, ADX, Volume, Breadth."""
    try:
        from services.sector_rotation_service import get_sector_rotation_analysis, _cache as _sa_cache
        if force and "r" in _sa_cache:
            del _sa_cache["r"]
        data = await get_sector_rotation_analysis()
        return JSONResponse(content=data)
    except Exception as exc:
        return JSONResponse(content={"daily": [], "weekly": [], "monthly": [],
                                     "recommendation": {}, "error": str(exc)})


@app.get("/api/tomorrow-runners")
async def tomorrow_runners_endpoint():
    """EOD multi-factor scan: next-day high-probability runner candidates."""
    try:
        from services.tomorrow_scanner_service import scan_tomorrow_runners
        results = await scan_tomorrow_runners()
        return JSONResponse(content={"results": results, "count": len(results)})
    except Exception as exc:
        return JSONResponse(content={"results": [], "count": 0, "error": str(exc)})


@app.get("/api/pivot-signals")
async def pivot_signals_endpoint(force: bool = False):
    """Live Nifty 200 pivot breakout scanner — R1 breakout (bullish) / S1 breakdown (bearish)."""
    try:
        from services.pivot_scanner_service import scan_pivot_breakouts, _cache as _piv_cache
        if force and "r" in _piv_cache:
            del _piv_cache["r"]
        data = await scan_pivot_breakouts()
        return JSONResponse(content=data)
    except Exception as exc:
        return JSONResponse(content={
            "bullish": [], "bearish": [], "total_scanned": 0,
            "error": str(exc), "timestamp": datetime.now().isoformat(),
        })



@app.get("/api/ai-cost")
async def ai_cost_endpoint():
    """Today's estimated OpenAI spend and remaining daily budget."""
    from services.openai_service import get_daily_ai_cost
    return JSONResponse(content=get_daily_ai_cost())


@app.post("/api/pre-market-report")
async def pre_market_report_endpoint():
    """Manually trigger the 9:30 AM pre-market report — builds PDF and sends to Telegram."""
    try:
        from services.pre_market_service import pre_market_scan_and_send
        asyncio.create_task(pre_market_scan_and_send())
        return {"status": "generating", "message": "Pre-Market report started — PDF will be sent to Telegram"}
    except Exception as exc:
        return JSONResponse(content={"status": "error", "error": str(exc)}, status_code=500)


@app.post("/api/eod-report")
async def eod_report_endpoint():
    """Trigger the 4:00 PM End-of-Day report — builds PDF and sends to Telegram."""
    try:
        from services.eod_report_service import eod_scan_and_send
        asyncio.create_task(eod_scan_and_send())
        return {"status": "generating", "message": "EOD report started — PDF will be sent to Telegram"}
    except Exception as exc:
        return JSONResponse(content={"status": "error", "error": str(exc)}, status_code=500)


@app.post("/api/pm-report")
async def pm_report_endpoint():
    """Manually trigger the 2:45 PM positional report — builds PDF and sends to Telegram."""
    try:
        from services.pm_report_service import pm_scan_and_send
        asyncio.create_task(pm_scan_and_send())
        return {"status": "generating", "message": "PM report started — PDF will be sent to Telegram"}
    except Exception as exc:
        return JSONResponse(content={"status": "error", "error": str(exc)}, status_code=500)


@app.post("/api/weekly-report")
async def weekly_report_endpoint():
    """Manually trigger the Saturday weekly performance report."""
    try:
        from services.weekly_report_service import weekly_scan_and_send
        asyncio.create_task(weekly_scan_and_send())
        return {"status": "generating", "message": "Weekly report started — PDF will be sent to Telegram"}
    except Exception as exc:
        return JSONResponse(content={"status": "error", "error": str(exc)}, status_code=500)


@app.post("/api/vwma-candle-report")
async def vwma_candle_report_endpoint():
    """Trigger Doji/Hammer/Pin Bar + Bullish Engulfing scan on Nifty 500."""
    try:
        from services.vwma_candle_service import send_vwma_candle_report
        asyncio.create_task(send_vwma_candle_report())
        return {"status": "generating", "message": "Reversal+Engulfing report started — PDF will be sent to Telegram"}
    except Exception as exc:
        return JSONResponse(content={"status": "error", "error": str(exc)}, status_code=500)


# ── Portfolio endpoints ───────────────────────────────────────────────────────

@app.get("/api/portfolio")
async def portfolio_get():
    """List all portfolio holdings with live LTP and P&L."""
    from services.portfolio_service import get_portfolio_with_ltp
    holdings = await get_portfolio_with_ltp()
    total_invested = sum(h.get("invested", 0) for h in holdings)
    total_current  = sum(h.get("current",  0) for h in holdings if h.get("ltp", 0) > 0)
    total_pnl      = round(total_current - total_invested, 2) if total_current else 0
    total_pnl_pct  = round(total_pnl / total_invested * 100, 2) if total_invested else 0
    return JSONResponse(content={
        "holdings":       holdings,
        "total_invested": round(total_invested, 2),
        "total_current":  round(total_current,  2),
        "total_pnl":      total_pnl,
        "total_pnl_pct":  total_pnl_pct,
    })


@app.post("/api/portfolio")
async def portfolio_add(request: Request):
    """Add or update a holding. Body: {symbol, qty, avg_price}"""
    body = await request.json()
    symbol    = str(body.get("symbol", "")).upper().strip()
    qty       = float(body.get("qty", 0))
    avg_price = float(body.get("avg_price", 0))
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol required")
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be > 0")
    if avg_price <= 0:
        raise HTTPException(status_code=400, detail="avg_price must be > 0")
    from services.portfolio_service import add_or_update_holding
    holdings = add_or_update_holding(symbol, qty, avg_price)
    return JSONResponse(content={"status": "saved", "count": len(holdings)})


@app.delete("/api/portfolio/{symbol}")
async def portfolio_remove(symbol: str):
    """Remove a holding by symbol."""
    from services.portfolio_service import remove_holding
    holdings = remove_holding(symbol)
    return JSONResponse(content={"status": "removed", "count": len(holdings)})


@app.get("/api/test-telegram")
async def test_telegram():
    """Test Telegram bot connectivity — use after adding @open_nifty_bot to your group."""
    from services.telegram_service import send_message
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    ok = await send_message(
        f"RRE Bot Connected — Pivot alerts are active!\n"
        f"Bot: {TELEGRAM_BOT_TOKEN[:20]}...\n"
        f"Chat: {TELEGRAM_CHAT_ID}"
    )
    return {"ok": ok, "chat_id": TELEGRAM_CHAT_ID, "token_prefix": TELEGRAM_BOT_TOKEN[:20]}
