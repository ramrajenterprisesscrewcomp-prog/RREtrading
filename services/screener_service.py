import requests
import logging
from bs4 import BeautifulSoup
from config import SCREENER_BASE_URL, SCREENER_HEADERS

logger = logging.getLogger(__name__)


def scrape_screener(symbol: str) -> dict:
    """
    Scrape Screener.in for fundamentals, quarterly results, about text,
    P&L multi-year, balance sheet, cash flow, and shareholding data.
    Tries consolidated view first, falls back to standalone.
    """
    symbol = symbol.upper()
    urls = [
        f"{SCREENER_BASE_URL}/{symbol}/consolidated/",
        f"{SCREENER_BASE_URL}/{symbol}/",
    ]

    soup = None
    for url in urls:
        try:
            resp = requests.get(url, headers=SCREENER_HEADERS, timeout=15)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                break
            elif resp.status_code == 404:
                continue
        except Exception as exc:
            logger.warning("Screener fetch failed for %s at %s: %s", symbol, url, exc)

    if soup is None:
        return {}

    result = {}

    # ── TOP RATIOS ─────────────────────────────────────────────────────────
    top_ratios = soup.find("ul", id="top-ratios")
    if top_ratios:
        for li in top_ratios.find_all("li"):
            name_el  = li.find("span", class_="name")
            value_el = li.find("span", class_="number")
            if name_el and value_el:
                result[name_el.get_text(strip=True)] = value_el.get_text(strip=True)

    # ── COMPANY NAME ───────────────────────────────────────────────────────
    title_tag = soup.find("h1", class_="h2")
    if title_tag:
        result["__company_name"] = title_tag.get_text(strip=True)

    # ── ABOUT / COMPANY DESCRIPTION ───────────────────────────────────────
    about_text = ""
    for selector in [
        ("section", {"id": "about"}),
        ("div",     {"class": "company-profile"}),
        ("div",     {"class": "about"}),
    ]:
        tag, attrs = selector
        about_el = soup.find(tag, attrs)
        if about_el:
            paras = about_el.find_all("p")
            if paras:
                about_text = " ".join(p.get_text(separator=" ", strip=True) for p in paras[:3])
            else:
                about_text = about_el.get_text(separator=" ", strip=True)
            about_text = " ".join(about_text.split())[:600]
            if len(about_text) > 40:
                break
    if not about_text:
        for p in soup.select(".company-links ~ p, .sub-list + p"):
            txt = p.get_text(strip=True)
            if len(txt) > 60:
                about_text = txt[:600]
                break
    result["__about"] = about_text

    # ── PROFIT & LOSS (multi-year) ─────────────────────────────────────────
    pl_section = soup.find("section", id="profit-loss")
    if pl_section:
        result["quarterly_results_raw"] = _parse_table(pl_section)
        result["__pl_years"], result["__pl_data"] = _parse_multiyear_table(pl_section)

    # quarterly results as fallback display
    if not result.get("quarterly_results_raw"):
        q_section = soup.find("section", id="quarters")
        if q_section:
            result["quarterly_results_raw"] = _parse_table(q_section)

    # ── BALANCE SHEET ──────────────────────────────────────────────────────
    bs_section = soup.find("section", id="balance-sheet")
    if bs_section:
        _, bs_data = _parse_multiyear_table(bs_section)
        result["__bs_data"] = bs_data

    # ── CASH FLOW ──────────────────────────────────────────────────────────
    cf_section = soup.find("section", id="cash-flow")
    if cf_section:
        _, cf_data = _parse_multiyear_table(cf_section)
        result["__cf_data"] = cf_data

    # ── SHAREHOLDING ───────────────────────────────────────────────────────
    sh_section = soup.find("section", id="shareholding")
    if sh_section:
        result["__shareholding"] = _parse_shareholding(sh_section)

    return result


# ── TABLE PARSERS ──────────────────────────────────────────────────────────

def _sf(text: str):
    """Safe parse a Screener number string → float or None."""
    try:
        cleaned = str(text).replace(",", "").replace("%", "").replace("₹", "").strip()
        return float(cleaned) if cleaned and cleaned not in ("-", "N/A", "") else None
    except (ValueError, TypeError):
        return None


def _parse_multiyear_table(section) -> tuple[list[str], dict[str, list]]:
    """
    Parse a Screener annual data table.
    Returns (year_headers, {row_name: [val_year0, val_year1, ...]}).
    Values are raw strings (caller converts).
    """
    table = section.find("table", class_="data-table")
    if not table:
        return [], {}
    thead = table.find("thead")
    tbody = table.find("tbody")
    if not thead or not tbody:
        return [], {}

    headers = [th.get_text(strip=True) for th in thead.find_all("th")]
    years = headers[1:] if headers else []   # skip first (row-label) column

    data: dict[str, list] = {}
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        row_name = cells[0].get_text(strip=True)
        if not row_name:
            continue
        values = [cells[i].get_text(strip=True) if i < len(cells) else "" for i in range(1, len(headers))]
        data[row_name] = values

    return years, data


def _parse_shareholding(section) -> dict:
    """Return latest quarter's promoter, FII, DII percentages."""
    table = section.find("table", class_="data-table")
    if not table:
        return {}
    out: dict[str, float | None] = {}
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        name = cells[0].get_text(strip=True).lower()
        # last column = most recent quarter
        latest = cells[-1].get_text(strip=True) if cells else ""
        val = _sf(latest)
        if "promoter" in name:
            out["promoter_pct"] = val
        elif "fii" in name or "foreign" in name:
            out["fii_pct"] = val
        elif "dii" in name or "domestic" in name:
            out["dii_pct"] = val
    return out


def _parse_table(section) -> list[dict]:
    table = section.find("table", class_="data-table")
    if not table:
        return []
    thead = table.find("thead")
    tbody = table.find("tbody")
    if not thead or not tbody:
        return []
    headers = [th.get_text(strip=True) for th in thead.find_all("th")]
    rows = []
    for tr in tbody.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if cells and len(cells) <= len(headers):
            row = {headers[i]: cells[i] for i in range(len(cells)) if i < len(headers)}
            if any(v for v in row.values()):
                rows.append(row)
    return rows
