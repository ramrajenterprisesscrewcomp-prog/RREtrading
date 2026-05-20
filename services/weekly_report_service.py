"""
weekly_report_service.py — Saturday 9 AM weekly performance report.
Sections: Market Week, Runner Win Rate, R1 Alert Win Rate, Top Losers Insights, AI Commentary.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


def _week_dates() -> list[str]:
    """Return Mon-Fri date strings for the most recently completed trading week."""
    today = datetime.now(_IST)
    # On Saturday (weekday=5), go back 5 days for Monday
    days_since_mon = (today.weekday() - 0) % 7  # 0=Mon .. 6=Sun
    if today.weekday() == 5:   # Saturday
        days_since_mon = 5
    elif today.weekday() == 6: # Sunday
        days_since_mon = 6
    monday = today - timedelta(days=days_since_mon)
    return [(monday + timedelta(days=i)).strftime("%d %b %Y") for i in range(5)]


async def _fetch_stock_perf(symbols: list[str]) -> dict[str, dict[str, float]]:
    """Fetch 10-day daily OHLC from Yahoo Finance. Returns {symbol: {date_str: pchange}}."""
    result: dict[str, dict[str, float]] = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        tasks = {}
        for sym in symbols:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS"
                   f"?interval=1d&range=10d")
            tasks[sym] = client.get(url)
        responses = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for sym, resp in zip(tasks.keys(), responses):
            if isinstance(resp, Exception):
                result[sym] = {}
                continue
            try:
                data = resp.json()
                res  = data["chart"]["result"][0]
                timestamps = res["timestamp"]
                closes     = res["indicators"]["quote"][0]["close"]
                day_perf: dict[str, float] = {}
                for i in range(1, len(timestamps)):
                    if closes[i] and closes[i - 1]:
                        dt = datetime.fromtimestamp(timestamps[i],
                                                    tz=timezone.utc).astimezone(_IST)
                        dstr    = dt.strftime("%d %b %Y")
                        pchange = round((closes[i] / closes[i - 1] - 1) * 100, 2)
                        day_perf[dstr] = pchange
                result[sym] = day_perf
            except Exception:
                result[sym] = {}
    return result


def _win_rate(runners: list[dict], next_date: str,
              perf: dict[str, dict[str, float]]) -> dict:
    """Calculate win/loss/neutral counts for a set of runners on next_date."""
    wins = losses = neutral = 0
    details = []
    for r in runners:
        sym = r["symbol"]
        pc  = perf.get(sym, {}).get(next_date)
        if pc is None:
            continue
        if pc >= 1.0:
            outcome = "WIN"
            wins += 1
        elif pc <= -0.5:
            outcome = "LOSS"
            losses += 1
        else:
            outcome = "NEUTRAL"
            neutral += 1
        details.append({"symbol": sym, "pchange": pc, "outcome": outcome})
    total = wins + losses + neutral
    return {
        "wins": wins, "losses": losses, "neutral": neutral, "total": total,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "details": sorted(details, key=lambda x: x["pchange"], reverse=True),
    }


def _generate_weekly_ai(week_dates, runner_stats, r1_stats,
                         top_losers, nifty_weekly_pct) -> dict:
    """AI weekly commentary + loser analysis. Returns dict of text sections."""
    try:
        from services.openai_service import _get_client, _under_limit, _record_cost
        if not _under_limit():
            return {"commentary": "", "loser_insights": {}}

        # Runner summary
        total_w = sum(s["wins"] for s in runner_stats.values())
        total_t = sum(s["total"] for s in runner_stats.values())
        wr_str  = f"{total_w}/{total_t} ({round(total_w/total_t*100,1) if total_t else 0}%)"

        # Loser list
        loser_lines = "\n".join(
            f"  {l['symbol']}: {l.get('pchange', l.get('pchange_pct', 0)):+.2f}%  "
            f"Vol={l.get('totalTradedVolume', l.get('volume', 'N/A'))}"
            for l in top_losers[:8]
        )

        prompt = (
            f"You are a senior Indian equity research analyst.\n"
            f"Week: {week_dates[0]} to {week_dates[-1]}\n"
            f"Nifty weekly change: {nifty_weekly_pct:+.2f}%\n"
            f"Runner recommendation win rate this week: {wr_str}\n\n"
            f"Top losers this week:\n{loser_lines}\n\n"
            f"Task 1 — WEEKLY COMMENTARY (100 words): Summarise the week's market tone, "
            f"key themes, and what to watch next week.\n\n"
            f"Task 2 — LOSER INSIGHTS: For EACH loser stock above, give ONE sentence "
            f"explaining the most likely reason for the decline (sector rotation, "
            f"earnings, FII selling, technical breakdown, etc).\n\n"
            f"Respond ONLY in JSON (no markdown):\n"
            f'{{"commentary": "...", "loser_insights": {{"SYMBOL": "reason", ...}}}}'
        )
        resp = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        _record_cost(resp.usage)
        import json
        data = json.loads(resp.choices[0].message.content)
        return {
            "commentary":     data.get("commentary", ""),
            "loser_insights": data.get("loser_insights", {}),
        }
    except Exception as exc:
        logger.warning("Weekly AI failed: %s", exc)
        return {"commentary": "", "loser_insights": {}}


def _build_weekly_pdf(week_dates, runner_by_day, runner_stats,
                      r1_alerts, r1_summary,
                      top_losers, nifty_weekly_pct,
                      ai_result) -> bytes:
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                     TableStyle, HRFlowable)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    W, H  = A4
    buf   = BytesIO()
    doc   = SimpleDocTemplate(buf, pagesize=A4,
                               leftMargin=12*mm, rightMargin=12*mm,
                               topMargin=12*mm, bottomMargin=12*mm)

    GREEN  = colors.HexColor("#22c55e")
    RED    = colors.HexColor("#ef4444")
    YELLOW = colors.HexColor("#f59e0b")
    SLATE  = colors.HexColor("#1e293b")
    NAVY   = colors.HexColor("#0f172a")
    WHITE  = colors.white
    LGRAY  = colors.HexColor("#94a3b8")

    def _style(name, size=8, bold=False, color=WHITE, align=TA_LEFT):
        return ParagraphStyle(name, fontSize=size, leading=size + 3,
                               textColor=color, fontName="Helvetica-Bold" if bold else "Helvetica",
                               alignment=align)

    def _table(rows, col_widths, header_color=SLATE):
        t  = Table(rows, colWidths=col_widths, repeatRows=1)
        ts = TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), header_color),
            ("TEXTCOLOR",   (0, 0), (-1, 0), WHITE),
            ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",    (0, 0), (-1, -1), 7.5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [NAVY, colors.HexColor("#111827")]),
            ("TEXTCOLOR",   (0, 1), (-1, -1), WHITE),
            ("GRID",        (0, 0), (-1, -1), 0.3, colors.HexColor("#1e1e30")),
            ("LEFTPADDING",  (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING",   (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ])
        return t, ts

    story = []
    pg = lambda txt, **kw: Paragraph(txt, _style("x", **kw))
    hr = lambda: HRFlowable(width="100%", thickness=0.5, color=SLATE, spaceAfter=4)

    # ── Title ──────────────────────────────────────────────────────────────────
    story.append(pg(f"RRE WEEKLY PERFORMANCE REPORT", size=14, bold=True,
                    color=GREEN, align=TA_CENTER))
    story.append(pg(f"{week_dates[0]}  →  {week_dates[-1]}", size=9,
                    color=LGRAY, align=TA_CENTER))
    story.append(Spacer(1, 4*mm))
    story.append(hr())

    # ── Market Week Snapshot ───────────────────────────────────────────────────
    story.append(pg("Market Week Snapshot", size=10, bold=True, color=YELLOW))
    story.append(Spacer(1, 2*mm))
    nifty_color = "22c55e" if nifty_weekly_pct >= 0 else "ef4444"
    arrow = "▲" if nifty_weekly_pct >= 0 else "▼"
    story.append(pg(
        f"Nifty 50 weekly change: "
        f"<font color='#{nifty_color}'><b>{arrow} {nifty_weekly_pct:+.2f}%</b></font>",
        size=9
    ))
    story.append(Spacer(1, 4*mm))
    story.append(hr())

    # ── Runner Win Rate ────────────────────────────────────────────────────────
    story.append(pg("Tomorrow's Runner — Weekly Win Rate", size=10, bold=True, color=YELLOW))
    story.append(Spacer(1, 2*mm))

    # Summary row
    total_w = sum(s["wins"] for s in runner_stats.values())
    total_l = sum(s["losses"] for s in runner_stats.values())
    total_t = sum(s["total"] for s in runner_stats.values())
    overall_wr = round(total_w / total_t * 100, 1) if total_t else 0
    wr_color = GREEN if overall_wr >= 50 else (YELLOW if overall_wr >= 35 else RED)
    story.append(pg(
        f"Week total: <b>{total_w}</b> wins / <b>{total_t}</b> picks  |  "
        f"Win rate: <font color='#{('22c55e' if overall_wr>=50 else ('f59e0b' if overall_wr>=35 else 'ef4444'))}'>"
        f"<b>{overall_wr}%</b></font>  "
        f"(Win = next-day gain &gt; 1%)", size=8, color=LGRAY
    ))
    story.append(Spacer(1, 2*mm))

    # Per-day breakdown table
    wd_header = ["Date", "Runners", "Wins", "Losses", "Neutral", "Win Rate", "Best Pick"]
    wd_rows   = [wd_header]
    cw = [28*mm, 18*mm, 14*mm, 14*mm, 16*mm, 18*mm, 48*mm]
    for i, d in enumerate(week_dates[:-1]):   # Mon-Thu (Fri runners → next week)
        s    = runner_stats.get(d, {})
        best = ""
        if s.get("details"):
            b = s["details"][0]
            best = f"{b['symbol']} ({b['pchange']:+.1f}%)"
        wr   = s.get("win_rate", 0)
        wd_rows.append([
            d,
            str(s.get("total", 0)),
            str(s.get("wins", 0)),
            str(s.get("losses", 0)),
            str(s.get("neutral", 0)),
            f"{wr}%",
            best,
        ])
    # Friday row — no next-day data yet
    fri = week_dates[-1]
    fr_count = len(runner_by_day.get(fri, []))
    wd_rows.append([fri, str(fr_count), "—", "—", "—", "Pending", "Next Monday"])

    t, ts = _table(wd_rows, cw)
    for i, d in enumerate(week_dates[:-1], 1):
        wr = runner_stats.get(d, {}).get("win_rate", 0)
        c  = GREEN if wr >= 50 else (YELLOW if wr >= 35 else RED)
        ts.add("TEXTCOLOR", (5, i), (5, i), c)
        ts.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
    t.setStyle(ts)
    story.append(t)
    story.append(Spacer(1, 4*mm))

    # Top runner hits of the week
    all_hits = []
    for d in week_dates[:-1]:
        for det in runner_stats.get(d, {}).get("details", []):
            if det["outcome"] == "WIN":
                all_hits.append(det)
    if all_hits:
        all_hits.sort(key=lambda x: x["pchange"], reverse=True)
        story.append(pg("Best Runner Hits This Week", size=9, bold=True, color=GREEN))
        story.append(Spacer(1, 1*mm))
        hits_rows = [["Symbol", "Next-Day Gain", "Outcome"]]
        cw2 = [50*mm, 50*mm, 50*mm]
        for h in all_hits[:10]:
            hits_rows.append([h["symbol"], f"+{h['pchange']:.2f}%", "✓ WIN"])
        t2, ts2 = _table(hits_rows, cw2)
        for i in range(1, len(hits_rows)):
            ts2.add("TEXTCOLOR", (1, i), (2, i), GREEN)
        t2.setStyle(ts2)
        story.append(t2)
        story.append(Spacer(1, 4*mm))

    story.append(hr())

    # ── R1 Alert Win Rate ──────────────────────────────────────────────────────
    story.append(pg("R1 Breakout Alert — Win Rate", size=10, bold=True, color=YELLOW))
    story.append(Spacer(1, 2*mm))

    if r1_alerts:
        r1_wr = r1_summary.get("win_rate", 0)
        r1_w  = r1_summary.get("wins", 0)
        r1_t  = r1_summary.get("total", 0)
        story.append(pg(
            f"Week total: <b>{r1_w}</b> wins / <b>{r1_t}</b> alerts  |  "
            f"Win rate: <font color='#{('22c55e' if r1_wr>=50 else 'f59e0b')}'>"
            f"<b>{r1_wr}%</b></font>  (Win = closed above R1 price)",
            size=8, color=LGRAY
        ))
        story.append(Spacer(1, 2*mm))

        r1_header = ["Date", "Symbol", "R1 Price", "LTP at Alert", "Breakout %", "Outcome"]
        r1_rows   = [r1_header]
        cw3 = [24*mm, 28*mm, 24*mm, 28*mm, 22*mm, 24*mm]
        for a in r1_alerts[:20]:
            outcome = a.get("outcome", "—")
            r1_rows.append([
                a.get("alert_date", ""),
                a.get("symbol", ""),
                f"₹{float(a.get('r1_price', 0)):,.2f}",
                f"₹{float(a.get('ltp_at_alert', 0)):,.2f}",
                f"+{float(a.get('breakout_pct', 0)):.2f}%",
                outcome,
            ])
        t3, ts3 = _table(r1_rows, cw3)
        for i, a in enumerate(r1_alerts[:20], 1):
            oc = a.get("outcome", "—")
            c  = GREEN if oc == "WIN" else (RED if oc == "LOSS" else LGRAY)
            ts3.add("TEXTCOLOR", (5, i), (5, i), c)
            ts3.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
        t3.setStyle(ts3)
        story.append(t3)
    else:
        story.append(pg("No R1 alerts fired this week.", size=8, color=LGRAY))
    story.append(Spacer(1, 4*mm))
    story.append(hr())

    # ── Top Losers Insights ────────────────────────────────────────────────────
    story.append(pg("Top Losers — Why They Fell", size=10, bold=True, color=RED))
    story.append(Spacer(1, 2*mm))

    loser_insights = ai_result.get("loser_insights", {})
    if top_losers:
        li_header = ["Symbol", "Weekly %", "AI Insight"]
        li_rows   = [li_header]
        cw4 = [28*mm, 22*mm, 106*mm]
        for l in top_losers[:8]:
            sym     = l.get("symbol", "")
            pc      = l.get("pchange", 0)
            insight = loser_insights.get(sym, "Sector weakness / broad market selling")
            li_rows.append([sym, f"{pc:+.2f}%", insight])
        t4, ts4 = _table(li_rows, cw4)
        for i in range(1, len(li_rows)):
            ts4.add("TEXTCOLOR", (1, i), (1, i), RED)
            ts4.add("FONTNAME",  (1, i), (1, i), "Helvetica-Bold")
        t4.setStyle(ts4)
        story.append(t4)
    else:
        story.append(pg("No loser data available.", size=8, color=LGRAY))
    story.append(Spacer(1, 4*mm))
    story.append(hr())

    # ── AI Weekly Commentary ───────────────────────────────────────────────────
    commentary = ai_result.get("commentary", "")
    if commentary:
        story.append(pg("AI Weekly Commentary", size=10, bold=True, color=YELLOW))
        story.append(Spacer(1, 2*mm))
        story.append(pg(commentary, size=8, color=WHITE))
        story.append(Spacer(1, 4*mm))

    # Footer
    story.append(hr())
    story.append(pg(
        f"Generated {datetime.now(_IST).strftime('%d %b %Y %H:%M IST')}  |  RRE Trading Bot",
        size=7, color=LGRAY, align=TA_CENTER
    ))

    doc.build(story)
    return buf.getvalue()


async def weekly_scan_and_send() -> None:
    """Main entry point — runs Saturday 9 AM IST."""
    from services.telegram_service import send_document
    from services.supabase_service import load_week_runners_db, load_week_r1_alerts_db

    now = datetime.now(_IST)
    logger.info("Weekly Report: starting for week %s", now.strftime("%d %b %Y"))

    week_dates = _week_dates()
    logger.info("Week dates: %s", week_dates)

    # ── Load runners for each day ──────────────────────────────────────────────
    runner_by_day = load_week_runners_db(week_dates)
    all_runner_syms = list({r["symbol"] for runners in runner_by_day.values() for r in runners})

    # ── Fetch Nifty weekly performance ────────────────────────────────────────
    nifty_weekly_pct = 0.0
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=10d",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            d = r.json()["chart"]["result"][0]
            closes = [c for c in d["indicators"]["quote"][0]["close"] if c]
            if len(closes) >= 6:
                nifty_weekly_pct = round((closes[-1] / closes[-6] - 1) * 100, 2)
    except Exception as exc:
        logger.warning("Nifty weekly fetch failed: %s", exc)

    # ── Fetch next-day performance for all runner stocks ──────────────────────
    stock_perf: dict[str, dict[str, float]] = {}
    if all_runner_syms:
        stock_perf = await _fetch_stock_perf(all_runner_syms)

    # ── Calculate runner win rates per day ────────────────────────────────────
    runner_stats = {}
    for i, d in enumerate(week_dates[:-1]):         # Mon-Thu only
        next_d = week_dates[i + 1]
        runners = runner_by_day.get(d, [])
        if runners:
            runner_stats[d] = _win_rate(runners, next_d, stock_perf)
        else:
            runner_stats[d] = {"wins": 0, "losses": 0, "neutral": 0, "total": 0, "win_rate": 0, "details": []}

    # ── Load R1 alerts & calculate win rate ───────────────────────────────────
    r1_alerts_raw = load_week_r1_alerts_db(week_dates)
    # For win: stock needs to have closed above R1 on alert day
    r1_perf = await _fetch_stock_perf(list({a["symbol"] for a in r1_alerts_raw})) if r1_alerts_raw else {}
    r1_alerts: list[dict] = []
    r1_wins = 0
    for a in r1_alerts_raw:
        sym  = a["symbol"]
        dstr = a.get("alert_date", "")
        r1p  = float(a.get("r1_price", 0))
        # Use end-of-day pchange to determine outcome
        day_pchange = r1_perf.get(sym, {}).get(dstr, None)
        if day_pchange is not None:
            outcome = "WIN" if day_pchange >= 0.5 else "LOSS"
            if outcome == "WIN":
                r1_wins += 1
        else:
            outcome = "—"
        r1_alerts.append({**a, "outcome": outcome})
    r1_total = len([a for a in r1_alerts if a["outcome"] != "—"])
    r1_summary = {
        "wins": r1_wins,
        "total": r1_total,
        "win_rate": round(r1_wins / r1_total * 100, 1) if r1_total else 0,
    }

    # ── Get top losers (from Nifty 500 this week) ─────────────────────────────
    top_losers: list[dict] = []
    try:
        from services.nse_service import get_nifty500_ohlc
        all_stocks = await get_nifty500_ohlc()
        top_losers = sorted(
            [s for s in all_stocks if s.get("pchange") is not None],
            key=lambda x: x["pchange"]
        )[:10]
    except Exception as exc:
        logger.warning("Top losers fetch failed: %s", exc)

    # ── AI analysis ───────────────────────────────────────────────────────────
    ai_result = await asyncio.to_thread(
        _generate_weekly_ai, week_dates, runner_stats, r1_summary, top_losers, nifty_weekly_pct
    )

    # ── Build PDF ─────────────────────────────────────────────────────────────
    pdf_bytes = await asyncio.to_thread(
        _build_weekly_pdf,
        week_dates, runner_by_day, runner_stats,
        r1_alerts, r1_summary,
        top_losers, nifty_weekly_pct,
        ai_result,
    )

    # ── Send to Telegram ──────────────────────────────────────────────────────
    total_w = sum(s["wins"] for s in runner_stats.values())
    total_t = sum(s["total"] for s in runner_stats.values())
    overall_wr = round(total_w / total_t * 100, 1) if total_t else 0
    caption = (
        f"📊 Weekly Report — {week_dates[0]} to {week_dates[-1]}\n"
        f"Nifty: {nifty_weekly_pct:+.2f}%  |  "
        f"Runner Win Rate: {overall_wr}% ({total_w}/{total_t})\n"
        f"R1 Alerts: {r1_summary['wins']}/{r1_summary['total']} wins"
    )
    filename = f"RRE_Weekly_{datetime.now(_IST).strftime('%Y%m%d')}.pdf"
    await send_document(pdf_bytes, filename, caption)

    try:
        from services.supabase_service import log_report_sent
        log_report_sent("weekly", caption, {"runner_win_rate": overall_wr, "r1_win_rate": r1_summary["win_rate"]})
    except Exception:
        pass

    logger.info("Weekly report sent — Runner WR: %s%%, R1 WR: %s%%",
                overall_wr, r1_summary["win_rate"])


async def weekly_report_loop() -> None:
    """Fires every Saturday at 9:00 AM IST."""
    logger.info("Weekly report loop started")
    await asyncio.sleep(60)
    fired_week: str | None = None
    while True:
        now = datetime.now(_IST)
        if now.weekday() == 5 and now.hour == 9 and now.minute < 5:
            week_key = now.strftime("%Y-W%U")
            if fired_week != week_key:
                fired_week = week_key
                logger.info("Weekly report: firing for %s", week_key)
                try:
                    await weekly_scan_and_send()
                except Exception as exc:
                    logger.error("Weekly report failed: %s", exc)
        await asyncio.sleep(60)
