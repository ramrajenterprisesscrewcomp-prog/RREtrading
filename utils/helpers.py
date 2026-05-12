def classify_oi_buildup(oi_change_pct: float, price_change_pct: float) -> str:
    """
    Standard F&O market convention:
      OI UP   + Price UP   = Long Buildup   (fresh longs, bullish)
      OI UP   + Price DOWN = Short Buildup  (fresh shorts, bearish)
      OI DOWN + Price UP   = Short Covering (shorts exiting, mildly bullish)
      OI DOWN + Price DOWN = Long Covering  (longs exiting, mildly bearish)
    """
    OI_THRESHOLD    = 0.5
    PRICE_THRESHOLD = 0.1

    oi_up   = oi_change_pct >  OI_THRESHOLD
    oi_down = oi_change_pct < -OI_THRESHOLD
    price_up = price_change_pct >  PRICE_THRESHOLD
    price_dn = price_change_pct < -PRICE_THRESHOLD

    if oi_up and price_up:
        return "Long Buildup"
    elif oi_up and price_dn:
        return "Short Buildup"
    elif oi_down and price_up:
        return "Short Covering"
    elif oi_down and price_dn:
        return "Long Covering"
    else:
        return "Neutral / Sideways"


def compute_max_pain(option_chain_data: list, strike_prices: list) -> float | None:
    """
    For each possible expiry strike, calculate total financial pain to option buyers
    if the underlying settles there. The strike with minimum pain to option sellers
    is the max pain price.
    """
    if not option_chain_data or not strike_prices:
        return None

    pain = {}
    for strike in strike_prices:
        total = 0
        for row in option_chain_data:
            s      = row.get("strikePrice", 0)
            ce_oi  = row.get("CE", {}).get("openInterest", 0) or 0
            pe_oi  = row.get("PE", {}).get("openInterest", 0) or 0
            total += max(0, strike - s) * ce_oi   # CE writer loss if expires above strike
            total += max(0, s - strike) * pe_oi   # PE writer loss if expires below strike
        pain[strike] = total

    if not pain:
        return None
    return min(pain, key=pain.get)


def safe_float(value, default=None) -> float | None:
    if value is None:
        return default
    try:
        cleaned = str(value).replace(",", "").replace("₹", "").replace("Cr", "").replace("%", "").strip()
        return float(cleaned) if cleaned else default
    except (ValueError, TypeError):
        return default


def _cagr(start, end, years: int) -> float | None:
    """Compound Annual Growth Rate (%)."""
    try:
        s, e = float(str(start).replace(",", "")), float(str(end).replace(",", ""))
        if s <= 0 or years <= 0:
            return None
        return round(((e / s) ** (1 / years) - 1) * 100, 1)
    except Exception:
        return None


def _latest(values: list) -> float | None:
    """Last non-empty numeric value from a Screener multi-year list."""
    for v in reversed(values):
        v = str(v).replace(",", "").replace("%", "").strip()
        try:
            return float(v)
        except (ValueError, TypeError):
            continue
    return None


def compute_checklist(data: dict) -> dict:
    """
    Evaluate 7-stage investment checklist and return scored results.
    Each item: {"pass": True/False/None, "value": display_str, "threshold": str}
    None = N/A (not scored).
    """
    sf = safe_float

    # ── helpers from screener multi-year data ─────────────────────────────
    pl   = data.get("__pl_data", {})
    bs   = data.get("__bs_data", {})
    cf   = data.get("__cf_data", {})
    sh   = data.get("__shareholding", {})

    def pct_latest(key: str) -> float | None:
        vals = pl.get(key, [])
        v = _latest(vals)
        return v   # already a percentage string like "17%"

    def num_latest(section: dict, key: str) -> float | None:
        vals = section.get(key, [])
        return _latest(vals)

    # ── Stage 1: Safety ───────────────────────────────────────────────────
    mkt_cap = sf(data.get("market_cap"))

    # D/E from balance sheet
    equity   = (num_latest(bs, "Equity Capital") or 0) + (num_latest(bs, "Reserves") or 0)
    debt     = num_latest(bs, "Borrowings+") or num_latest(bs, "Borrowings") or 0
    de_ratio = round(debt / equity, 2) if equity > 0 else None

    # Interest coverage from P&L
    op_profit  = num_latest(pl, "Operating Profit")
    interest   = num_latest(pl, "Interest")
    int_cov    = round(op_profit / interest, 1) if (op_profit and interest and interest > 0) else None

    s1_items = {
        "market_cap_ok":         _item(mkt_cap and mkt_cap > 1000,
                                       f"₹{mkt_cap:,.0f} Cr" if mkt_cap else "N/A",
                                       "> ₹1,000 Cr"),
        "promoter_pledge_ok":    _item(None, "N/A (data unavailable)", "< 10%"),
        "de_ratio_ok":           _item(de_ratio is not None and de_ratio < 1,
                                       str(de_ratio) if de_ratio is not None else "N/A",
                                       "< 1"),
        "interest_coverage_ok":  _item(int_cov is not None and int_cov > 3,
                                       f"{int_cov}x" if int_cov is not None else "N/A",
                                       "> 3x"),
    }

    # ── Stage 2: Profit Quality ───────────────────────────────────────────
    roce     = sf(data.get("roce"))
    roe      = sf(data.get("roe"))

    # Net Profit Margin from P&L
    sales   = num_latest(pl, "Sales+") or num_latest(pl, "Sales")
    net_profit = num_latest(pl, "Net Profit+") or num_latest(pl, "Net Profit")
    npm = round(net_profit / sales * 100, 1) if (sales and net_profit) else None

    # OPM from P&L percentage row
    opm_vals = pl.get("OPM %", [])
    opm = _latest([str(v).replace("%", "").strip() for v in opm_vals])

    s2_items = {
        "roce_ok":   _item(roce is not None and roce >= 18,
                           f"{roce}%" if roce is not None else "N/A", "≥ 18%"),
        "roe_ok":    _item(roe is not None and roe >= 15,
                           f"{roe}%" if roe is not None else "N/A", "≥ 15%"),
        "npm_ok":    _item(npm is not None and npm >= 10,
                           f"{npm}%" if npm is not None else "N/A", "≥ 10%"),
        "opm_ok":    _item(opm is not None and opm >= 15,
                           f"{opm}%" if opm is not None else "N/A", "≥ 15%"),
    }

    # ── Stage 3: Growth (5Y CAGR) ─────────────────────────────────────────
    sales_vals = pl.get("Sales+", pl.get("Sales", []))
    np_vals    = pl.get("Net Profit+", pl.get("Net Profit", []))
    eps_vals   = pl.get("EPS in Rs", [])

    # Need at least 5 data points (5 years + current) = index 0 and last
    def cagr_from_list(vals, n=5):
        nums = []
        for v in vals:
            try:
                nums.append(float(str(v).replace(",", "").strip()))
            except Exception:
                pass
        if len(nums) < 2:
            return None
        actual_n = min(n, len(nums) - 1)
        start = nums[-(actual_n + 1)]
        end   = nums[-1]
        return _cagr(start, end, actual_n)

    rev_cagr = cagr_from_list(sales_vals)
    np_cagr  = cagr_from_list(np_vals)
    eps_cagr = cagr_from_list(eps_vals)

    s3_items = {
        "revenue_growth_ok": _item(rev_cagr is not None and rev_cagr >= 12,
                                   f"{rev_cagr}% CAGR" if rev_cagr is not None else "N/A",
                                   "≥ 12% 5Y CAGR"),
        "profit_growth_ok":  _item(np_cagr is not None and np_cagr >= 12,
                                   f"{np_cagr}% CAGR" if np_cagr is not None else "N/A",
                                   "≥ 12% 5Y CAGR"),
        "eps_growth_ok":     _item(eps_cagr is not None and eps_cagr >= 12,
                                   f"{eps_cagr}% CAGR" if eps_cagr is not None else "N/A",
                                   "≥ 12% 5Y CAGR"),
    }

    # ── Stage 4: Valuation ────────────────────────────────────────────────
    pe_ratio       = sf(data.get("pe_ratio"))
    sector_idx_pe  = data.get("sector_index_pe")
    book_value     = sf(data.get("book_value"))
    last_price     = sf(data.get("last_price"))
    pb_ratio       = round(last_price / book_value, 2) if (last_price and book_value and book_value > 0) else None

    # Price/FCF
    fcf_latest = num_latest(cf, "Free Cash Flow")
    mkt_cr     = mkt_cap  # in Cr
    # FCF in Screener is in ₹ Cr for large caps
    price_fcf  = round(mkt_cr / fcf_latest, 1) if (mkt_cr and fcf_latest and fcf_latest > 0) else None

    pe_vs_sector = (pe_ratio is not None and sector_idx_pe is not None and pe_ratio <= sector_idx_pe)
    pb_ok = (pb_ratio is not None and pb_ratio < 5)   # generic reasonable threshold

    s4_items = {
        "pe_vs_sector_ok": _item(pe_vs_sector,
                                  f"P/E {pe_ratio} vs sector {sector_idx_pe}",
                                  "Stock P/E ≤ Sector Index P/E"),
        "pb_ok":           _item(pb_ok,
                                  f"P/B {pb_ratio}" if pb_ratio is not None else "N/A",
                                  "P/B < 5"),
        "price_fcf_ok":    _item(price_fcf is not None and price_fcf < 20,
                                  f"{price_fcf}x" if price_fcf is not None else "N/A",
                                  "Price/FCF < 20"),
    }

    # ── Stage 5: Smart Money ──────────────────────────────────────────────
    promoter_pct = sh.get("promoter_pct")
    fii_pct      = sh.get("fii_pct")
    dii_pct      = sh.get("dii_pct")
    inst_combined = (fii_pct or 0) + (dii_pct or 0)

    s5_items = {
        "promoter_holding_ok":  _item(promoter_pct is not None and promoter_pct >= 40,
                                       f"{promoter_pct}%" if promoter_pct is not None else "N/A",
                                       "≥ 40%"),
        "institutional_ok":     _item(inst_combined >= 10,
                                       f"FII {fii_pct or 'N/A'}% + DII {dii_pct or 'N/A'}%",
                                       "FII + DII ≥ 10%"),
        "insider_selling_ok":   _item(None, "N/A (data unavailable)", "No heavy selling"),
    }

    # ── Stage 6: Red Flags ────────────────────────────────────────────────
    cfo_latest = num_latest(cf, "Cash from Operating Activity+") or num_latest(cf, "Cash from Operating Activity")
    cfo_ok     = (cfo_latest is not None and cfo_latest > 0)

    # Equity dilution: if equity capital grew >20% over 5Y, flag it
    eq_cap_vals = bs.get("Equity Capital", [])
    eq_cagr     = cagr_from_list(eq_cap_vals)
    no_dilution = (eq_cagr is None or eq_cagr < 5)   # < 5% CAGR = no significant dilution

    s6_items = {
        "accounting_ok":   _item(None, "N/A (qualitative check)", "No red flags"),
        "cfo_positive_ok": _item(cfo_ok,
                                  f"CFO ₹{cfo_latest:,.0f} Cr" if cfo_latest is not None else "N/A",
                                  "CFO > 0"),
        "no_dilution_ok":  _item(no_dilution,
                                  f"Equity CAGR {eq_cagr}%" if eq_cagr is not None else "Stable",
                                  "Equity dilution < 5% CAGR"),
    }

    # ── Stage 7: Technical Timing ─────────────────────────────────────────
    tech = data.get("technical") or {}
    rsi  = tech.get("rsi")
    st   = (tech.get("supertrend") or {})
    ema50 = tech.get("ema50")
    price = sf(data.get("last_price"))
    deliv = sf(data.get("delivery_pct"))

    near_support  = (ema50 and price and price >= ema50 * 0.98) or (st.get("direction") == "Bullish")
    not_overexted = (rsi is not None and rsi < 70)
    vol_confirm   = (deliv is not None and deliv >= 40)

    s7_items = {
        "near_support_ok":    _item(near_support,
                                     f"Supertrend {st.get('direction','N/A')} · EMA50 {ema50}",
                                     "Above support/EMA50"),
        "not_overextended_ok": _item(not_overexted,
                                     f"RSI {rsi}" if rsi is not None else "N/A",
                                     "RSI < 70"),
        "volume_confirm_ok":  _item(vol_confirm,
                                     f"Delivery {deliv}%" if deliv is not None else "N/A",
                                     "Delivery ≥ 40%"),
    }

    # ── Scoring ───────────────────────────────────────────────────────────
    stages = [
        ("stage1_safety",     "Safety Check",          s1_items),
        ("stage2_quality",    "Profit Quality",         s2_items),
        ("stage3_growth",     "Consistent Growth",      s3_items),
        ("stage4_valuation",  "Valuation",              s4_items),
        ("stage5_smartmoney", "Smart Money",            s5_items),
        ("stage6_redflags",   "Red Flag Scan",          s6_items),
        ("stage7_technical",  "Technical Timing",       s7_items),
    ]

    result = {}
    stages_passed = 0
    total_stages  = 0

    for key, label, items in stages:
        scored  = [(k, v) for k, v in items.items() if v["pass"] is not None]
        passed  = sum(1 for _, v in scored if v["pass"])
        total   = len(scored)
        stage_pass = (total == 0) or (passed / total >= 0.5)
        result[key] = {
            "label":  label,
            "pass":   stage_pass,
            "score":  passed,
            "total":  total,
            "items":  items,
        }
        stages_passed += stage_pass
        total_stages  += 1

    # Overall rating
    fundamental_pass = sum(
        1 for k in ("stage1_safety", "stage2_quality", "stage3_growth",
                    "stage4_valuation", "stage5_smartmoney", "stage6_redflags")
        if result[k]["pass"]
    )
    rating = (
        "Strong Buy Zone 🔥" if fundamental_pass >= 5 else
        "Watchlist ⭐"       if fundamental_pass == 4 else
        "Caution ⚠️"         if fundamental_pass == 3 else
        "Avoid ❌"
    )

    result["stages_passed"]     = stages_passed
    result["total_stages"]      = total_stages
    result["fundamental_pass"]  = fundamental_pass
    result["rating"]            = rating

    return result


def _item(passed, value: str, threshold: str) -> dict:
    return {"pass": passed, "value": value, "threshold": threshold}


def fmt_volume(n: int) -> str:
    if not n:
        return "N/A"
    if n >= 10_000_000:
        return f"{n / 10_000_000:.2f} Cr"
    if n >= 100_000:
        return f"{n / 100_000:.2f} L"
    return str(n)
