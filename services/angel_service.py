import asyncio
import logging
import threading
import time
import requests
import pandas as pd
import pyotp
from datetime import datetime, timedelta, date
from config import ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP_SECRET

logger = logging.getLogger(__name__)

_smart_obj = None
_instrument_df: pd.DataFrame | None = None
_login_lock = threading.Lock()      # prevents concurrent login storms
_api_lock   = threading.Semaphore(1)  # one historical-data call at a time
_CALL_GAP   = 0.35                  # seconds between Angel One data calls

INSTRUMENT_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)


def _login():
    """Login to Angel One SmartAPI using TOTP. Only one thread may login at a time."""
    global _smart_obj
    with _login_lock:
        if _smart_obj is not None:   # another thread already logged in while we waited
            return _smart_obj
        try:
            from SmartApi import SmartConnect
            obj = SmartConnect(api_key=ANGEL_API_KEY)
            totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
            data = obj.generateSession(
                clientCode=ANGEL_CLIENT_ID,
                password=ANGEL_PASSWORD,
                totp=totp,
            )
            if not data or data.get("status") is False:
                raise ValueError(f"Angel One login failed: {data.get('message', 'unknown')}")
            _smart_obj = obj
            logger.info("Angel One session established")
            return obj
        except Exception as exc:
            logger.error("Angel One login error: %s", exc)
            raise


def _get_smart_obj():
    global _smart_obj
    if _smart_obj is None:
        _login()
    return _smart_obj


def _invalidate_session():
    """Call this when a request fails due to expired/invalid session."""
    global _smart_obj
    _smart_obj = None


def _get_instrument_df() -> pd.DataFrame:
    global _instrument_df
    if _instrument_df is None:
        try:
            data = requests.get(INSTRUMENT_URL, timeout=30).json()
            _instrument_df = pd.DataFrame(data)
        except Exception as exc:
            logger.error("Failed to download Angel One instrument master: %s", exc)
            _instrument_df = pd.DataFrame()
    return _instrument_df


def get_symbol_token(symbol: str, exchange: str = "NSE") -> str:
    """Look up the numeric symbol token needed for Angel One API calls."""
    df = _get_instrument_df()
    if df.empty:
        raise ValueError("Instrument master not loaded")

    match = df[
        (df["symbol"] == f"{symbol.upper()}-EQ") &
        (df["exch_seg"] == exchange)
    ]
    if match.empty:
        # Some symbols don't have -EQ suffix
        match = df[
            (df["symbol"] == symbol.upper()) &
            (df["exch_seg"] == exchange)
        ]
    if match.empty:
        raise ValueError(f"Symbol {symbol} not found in Angel One instrument master")
    return str(match.iloc[0]["token"])


def get_historical_ohlcv(symbol: str, interval: str = "ONE_DAY", days: int = 30) -> list:
    """
    Fetch historical OHLCV candles from Angel One.
    Returns list of [datetime_str, open, high, low, close, volume].
    Rate-limited to one call at a time with a 350 ms gap.
    """
    with _api_lock:
        time.sleep(_CALL_GAP)
        try:
            obj = _get_smart_obj()
            token = get_symbol_token(symbol)

            to_date   = datetime.now().strftime("%Y-%m-%d %H:%M")
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")

            params = {
                "exchange":    "NSE",
                "symboltoken": token,
                "interval":    interval,
                "fromdate":    from_date,
                "todate":      to_date,
            }
            response = obj.getCandleData(params)
            return response.get("data", [])
        except Exception as exc:
            logger.warning("Angel One OHLCV fetch failed for %s: %s", symbol, exc)
            return []


def get_ltp(symbol: str) -> float | None:
    """Fetch last traded price from Angel One as a fallback."""
    try:
        obj = _get_smart_obj()
        token = get_symbol_token(symbol)
        data = obj.ltpData("NSE", f"{symbol.upper()}-EQ", token)
        return data.get("data", {}).get("ltp")
    except Exception as exc:
        logger.warning("Angel One LTP fetch failed for %s: %s", symbol, exc)
        return None


def _parse_angel_expiry(s: str) -> date | None:
    """Parse Angel One expiry string ('24APR2025' or '24-APR-2025') → date."""
    for fmt in ("%d%b%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(s).strip().upper(), fmt).date()
        except ValueError:
            continue
    return None


def get_fno_lot_and_margin(symbol: str) -> dict:
    """
    Returns lot size and SPAN margin for 1 lot of the near-month futures contract.
    Lot size  → Angel One instrument master (lotsize field).
    Margin    → Angel One getMarginApi (actual SPAN+Exposure from RMS).
    Returns dict with keys: lot_size, margin_required, expiry, contract_value, error.
    """
    result = {"lot_size": 0, "margin_required": None, "expiry": "", "contract_value": None, "error": ""}
    try:
        df = _get_instrument_df()
        if df.empty:
            result["error"] = "Instrument master unavailable"
            return result

        sym_upper = symbol.upper().strip()

        # ── Find near-month futures row ──────────────────────────────────
        inst_type = "FUTSTK"
        mask = (
            (df["name"].str.strip().str.upper() == sym_upper) &
            (df["exch_seg"] == "NFO") &
            (df["instrumenttype"] == inst_type)
        )
        fut_rows = df[mask].copy()

        # Fallback for index futures (NIFTY, BANKNIFTY, etc.)
        if fut_rows.empty:
            mask2 = (
                (df["name"].str.strip().str.upper() == sym_upper) &
                (df["exch_seg"] == "NFO") &
                (df["instrumenttype"] == "FUTIDX")
            )
            fut_rows = df[mask2].copy()

        if fut_rows.empty:
            result["error"] = f"No futures found for {sym_upper}"
            return result

        fut_rows["exp_dt"] = fut_rows["expiry"].apply(_parse_angel_expiry)
        fut_rows = fut_rows.dropna(subset=["exp_dt"])
        fut_rows = fut_rows[fut_rows["exp_dt"] >= date.today()].sort_values("exp_dt")

        if fut_rows.empty:
            result["error"] = "No upcoming futures expiry found"
            return result

        near = fut_rows.iloc[0]
        token    = str(near["token"]).strip()
        lot_size = int(str(near["lotsize"]).strip() or 0)
        expiry   = str(near["expiry"]).strip()

        if not token or lot_size == 0:
            result["error"] = "Invalid token or lot size"
            return result

        result["lot_size"] = lot_size
        result["expiry"]   = expiry

        # ── Fetch LTP of the futures contract ────────────────────────────
        try:
            obj = _get_smart_obj()
            ltp_resp = obj.ltpData("NFO", near["symbol"], token)
            ltp = float((ltp_resp.get("data") or {}).get("ltp") or 0)
            if ltp > 0:
                result["contract_value"] = round(ltp * lot_size, 2)
        except Exception as exc:
            logger.debug("Angel One futures LTP failed for %s: %s", sym_upper, exc)

        # ── Get actual margin from Angel One margin API ───────────────────
        try:
            obj = _get_smart_obj()
            margin_resp = obj.getMarginApi({
                "positions": [{
                    "exchange":    "NFO",
                    "qty":         str(lot_size),
                    "price":       "0",
                    "productType": "CARRYFORWARD",
                    "token":       token,
                    "tradeType":   "BUY",
                }]
            })
            mdata = (margin_resp or {}).get("data") or {}
            # Angel One returns totalMarginRequired or similar fields
            margin = (
                mdata.get("totalMarginRequired")
                or mdata.get("marginRequired")
                or mdata.get("total")
                or mdata.get("margin")
            )
            if margin is None:
                # Try nested structure
                positions = mdata.get("marginByPositions") or []
                if positions:
                    margin = positions[0].get("marginRequired") or positions[0].get("margin")
            if margin is not None:
                result["margin_required"] = float(margin)
            else:
                logger.debug("Angel One margin API response: %s", margin_resp)
        except Exception as exc:
            logger.warning("Angel One margin API failed for %s: %s", sym_upper, exc)

    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("get_fno_lot_and_margin error for %s: %s", symbol, exc)

    return result


async def get_fno_lot_and_margin_async(symbol: str) -> dict:
    """Async wrapper for get_fno_lot_and_margin."""
    import asyncio
    return await asyncio.get_running_loop().run_in_executor(None, get_fno_lot_and_margin, symbol)


async def get_angel_nifty_oi(symbol: str = "NIFTY", spot_price: float = 0.0) -> dict:
    """
    Fetch index option OI from Angel One using last settlement data.
    Works on weekends/after-hours because Angel One returns EOD OI.
    Returns a dict compatible with get_nifty_oi_analysis() structure.
    """
    loop = asyncio.get_running_loop()

    def _sync_work():
        df = _get_instrument_df()
        if df.empty:
            raise ValueError("Instrument master empty")

        # Normalise column names (some versions use 'symbol', some 'tradingsymbol')
        cols = {c.lower() for c in df.columns}
        ts_col = "tradingsymbol" if "tradingsymbol" in cols else "symbol"

        mask = (
            (df["name"].str.strip().str.upper() == symbol.upper()) &
            (df["exch_seg"].str.upper() == "NFO") &
            (df["instrumenttype"].str.upper() == "OPTIDX")
        )
        opts = df[mask].copy()
        if opts.empty:
            raise ValueError(f"No {symbol} options in instrument master")

        opts["expiry_dt"] = opts["expiry"].apply(_parse_angel_expiry)
        opts = opts.dropna(subset=["expiry_dt"])

        today = date.today()
        future = opts[opts["expiry_dt"] >= today].copy()
        if future.empty:
            raise ValueError("No future expiries found in instrument master")

        expiries = sorted(future["expiry_dt"].unique())
        weekly_exp  = expiries[0]
        monthly_exp = expiries[1] if len(expiries) > 1 else None

        _spot = spot_price if spot_price > 0 else 24000.0
        # Angel One stores strike in paise (×100); compare against paise values
        _spot_paise = _spot * 100
        lower_p = _spot_paise * 0.92
        upper_p = _spot_paise * 1.08

        # Build token → meta map for up to 25 strikes per expiry
        token_meta: dict[str, dict] = {}
        tokens_to_fetch: list[str] = []

        for exp in [e for e in [weekly_exp, monthly_exp] if e is not None]:
            strikes_num = pd.to_numeric(future["strike"], errors="coerce")
            exp_rows = future[
                (future["expiry_dt"] == exp) &
                (strikes_num.between(lower_p, upper_p))
            ].copy()
            if exp_rows.empty:
                continue
            # Sort by distance from ATM so we pick the most relevant strikes
            exp_rows["_dist"] = (pd.to_numeric(exp_rows["strike"], errors="coerce") - _spot_paise).abs()
            exp_rows = exp_rows.sort_values("_dist").head(50)

            count = 0
            for _, row in exp_rows.iterrows():
                if count >= 25:
                    break
                ts = str(row.get(ts_col, "")).upper()
                token = str(row.get("token", "")).strip()
                if not token or not ts:
                    continue
                opttype = "CE" if ts.endswith("CE") else ("PE" if ts.endswith("PE") else None)
                if not opttype:
                    continue
                try:
                    # Convert paise → rupees for the strike stored in our map
                    strike = float(row.get("strike", 0)) / 100.0
                except (ValueError, TypeError):
                    continue
                if token not in token_meta:
                    token_meta[token] = {"expiry": exp, "strike": strike, "opttype": opttype}
                    tokens_to_fetch.append(token)
                    count += 1

        if not tokens_to_fetch:
            raise ValueError("No option tokens found in ATM strike range")

        obj = _get_smart_obj()
        resp = obj.getMarketData(mode="FULL", exchangeTokens={"NFO": tokens_to_fetch})
        fetched = resp.get("data", {}).get("fetched", [])
        if not fetched:
            raise ValueError("Angel One getMarketData returned no data")

        weekly_sm:  dict[float, dict] = {}
        monthly_sm: dict[float, dict] = {}

        for item in fetched:
            token = str(item.get("symbolToken", item.get("token", ""))).strip()
            meta  = token_meta.get(token)
            if not meta:
                continue
            # Angel One v1.5.5 uses 'opnInterest'; older versions used 'openInterest'
            oi  = int(item.get("opnInterest", item.get("openInterest", 0)) or 0)
            chg = int(item.get("netChangeInOI",
                      item.get("netChangeInOpenInterest",
                      item.get("changeInOI", 0))) or 0)
            strike  = meta["strike"]
            opttype = meta["opttype"]
            sm = weekly_sm if meta["expiry"] == weekly_exp else monthly_sm
            if strike not in sm:
                sm[strike] = {"strike": strike, "ce_oi": 0, "pe_oi": 0, "ce_chg": 0, "pe_chg": 0}
            if opttype == "CE":
                sm[strike]["ce_oi"] += oi; sm[strike]["ce_chg"] += chg
            else:
                sm[strike]["pe_oi"] += oi; sm[strike]["pe_chg"] += chg

        def _top5(sm):
            lst = list(sm.values())
            for s in lst:
                s["total_oi"] = s["ce_oi"] + s["pe_oi"]
            return sorted(lst, key=lambda x: x["total_oi"], reverse=True)[:5]

        w5 = _top5(weekly_sm)
        m5 = _top5(monthly_sm)

        total_oi_sum = sum(s["total_oi"] for s in w5)
        if total_oi_sum == 0:
            raise ValueError("Angel One returned zero OI for all strikes")

        def _fmt(d):
            return d.strftime("%d-%b-%Y").upper() if d else None

        return {
            "symbol":           symbol,
            "underlying":       _spot,
            "weekly_expiry":    _fmt(weekly_exp),
            "monthly_expiry":   _fmt(monthly_exp),
            "weekly_strikes":   w5,
            "monthly_strikes":  m5,
            "all_expiry_dates": [_fmt(e) for e in expiries[:6]],
            "is_stale":         False,
            "stale_label":      "",
            "source":           "Angel One",
        }

    try:
        return await loop.run_in_executor(None, _sync_work)
    except Exception as exc:
        logger.warning("Angel One NIFTY OI fetch failed for %s: %s", symbol, exc)
        return {}
