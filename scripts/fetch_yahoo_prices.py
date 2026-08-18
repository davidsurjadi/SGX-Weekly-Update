#!/usr/bin/env python3
"""
Top up weekly price/volume history and shares-outstanding from Yahoo Finance.

Updates (in DATA_DIR):
    yahoo_weekly_prices.csv   stock_code,week_start,close,volume   (APPEND-ONLY)
    shares_outstanding.csv    stock_code,shares_outstanding        (APPEND-ONLY)

These feed the price/volume chart, the weekly % change column, and the
market-cap normalisation in the signal classifier. The weekly SGX pipeline
never touched them, so until now they were only kept current by a scheduled
task running on a laptop.

APPEND-ONLY BY DESIGN
---------------------
Both files are historical records. This script only ever adds (ticker, week)
pairs that are missing; it never rewrites or deletes an existing row. That
means a partial or throttled run is harmless - the next run fills the gap -
and a bad run can't destroy years of accumulated history.

For each ticker it makes ONE chart-API call spanning every missing week, then
derives each week's Friday close (or last available close) and summed volume
locally. That keeps a normal weekly top-up to roughly one call per ticker.
"""

import csv
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
COOKIE_SEEDS = ["https://fc.yahoo.com", "https://finance.yahoo.com/quote/D05.SI/"]

REQUEST_DELAY = float(os.environ.get("YF_DELAY", "0.5"))
TIMEOUT = int(os.environ.get("YF_TIMEOUT", "30"))
# Only chase recent gaps; deep history was backfilled once and is stable.
MAX_WEEKS_BACK = int(os.environ.get("YF_MAX_WEEKS_BACK", "8"))


def log(msg):
    print(msg, flush=True)


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    for url in COOKIE_SEEDS:
        try:
            s.get(url, timeout=TIMEOUT)
        except requests.RequestException:
            pass
        if s.cookies:
            break
    crumb = ""
    try:
        r = s.get(CRUMB_URL, timeout=TIMEOUT)
        if r.status_code == 200 and "<" not in r.text:
            crumb = r.text.strip()
    except requests.RequestException:
        pass
    return s, crumb


def monday_of(d):
    return d - timedelta(days=d.weekday())


def load_rows(path, cols):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if all(c in r for c in cols)]


def tracked():
    """Tickers and the set of weeks the flow data covers."""
    codes, weeks = [], set()
    with open(DATA_DIR / "weekly_top10.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            c = (r.get("stock_code") or "").strip()
            w = (r.get("week_start") or "").strip()
            if c and c not in codes:
                codes.append(c)
            if w:
                weeks.add(w)
    return sorted(codes), sorted(weeks)


def fetch_prices(session, code, start_week, end_week):
    """One call per ticker covering the whole missing span."""
    try:
        p1 = int(datetime.strptime(start_week, "%Y-%m-%d").timestamp()) - 86400
        p2 = int(datetime.strptime(end_week, "%Y-%m-%d").timestamp()) + 9 * 86400
    except ValueError:
        return None
    try:
        r = session.get(CHART_URL.format(sym=f"{code}.SI"),
                        params={"period1": p1, "period2": p2, "interval": "1d"},
                        timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    chart = j.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        return None
    res = chart["result"][0]
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    vols = quote.get("volume") or []

    # week_start -> {"friday": close, "last": close, "vol": total}
    weeks = {}
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        d = datetime.utcfromtimestamp(t).date()
        wk = monday_of(d).isoformat()
        slot = weeks.setdefault(wk, {"friday": None, "last": None, "vol": 0})
        slot["vol"] += (vols[i] or 0) if i < len(vols) else 0
        slot["last"] = c
        if d.weekday() == 4:          # Friday
            slot["friday"] = c
    return weeks


def fetch_shares(session, crumb, code):
    try:
        r = session.get(SUMMARY_URL.format(sym=f"{code}.SI"),
                        params={"modules": "defaultKeyStatistics", "crumb": crumb},
                        timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        res = (r.json().get("quoteSummary", {}).get("result") or [{}])[0]
    except ValueError:
        return None
    node = ((res.get("defaultKeyStatistics") or {}).get("sharesOutstanding") or {})
    return node.get("raw")


def main():
    codes, all_weeks = tracked()
    if not all_weeks:
        raise RuntimeError("no weeks found in weekly_top10.csv")
    recent = all_weeks[-MAX_WEEKS_BACK:]
    log(f"Tickers {len(codes)} | flow weeks {len(all_weeks)} | checking last {len(recent)}")

    price_path = DATA_DIR / "yahoo_weekly_prices.csv"
    existing = load_rows(price_path, ["stock_code", "week_start"])
    have = {(r["stock_code"], r["week_start"]) for r in existing}
    log(f"Existing price rows: {len(existing)}")

    # Which ticker/week pairs are actually missing? Only chase weeks in which a
    # ticker appeared in a Top 10, since that is all the dashboard renders.
    appeared = {}
    with open(DATA_DIR / "weekly_top10.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            w, c = r.get("week_start"), r.get("stock_code")
            if w in set(recent) and c:
                appeared.setdefault(c, set()).add(w)

    todo = {c: sorted(ws - {w for (cc, w) in have if cc == c})
            for c, ws in appeared.items()}
    todo = {c: ws for c, ws in todo.items() if ws}
    log(f"Tickers needing price rows: {len(todo)}")

    session, crumb = make_session()
    added, failed = [], 0

    for i, (code, wanted) in enumerate(sorted(todo.items()), 1):
        weeks = fetch_prices(session, code, wanted[0], wanted[-1])
        if weeks is None:
            failed += 1
            time.sleep(REQUEST_DELAY)
            continue
        for wk in wanted:
            slot = weeks.get(wk)
            if not slot:
                continue
            close = slot["friday"] if slot["friday"] is not None else slot["last"]
            if close is None:
                continue
            added.append({"stock_code": code, "week_start": wk,
                          "close": f"{close:.4f}", "volume": int(slot["vol"])})
        if i % 25 == 0:
            log(f"  ...{i}/{len(todo)}")
        time.sleep(REQUEST_DELAY)

    if added:
        combined = existing + added
        combined.sort(key=lambda r: (r["stock_code"], r["week_start"]))
        with open(price_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["stock_code", "week_start", "close", "volume"])
            w.writeheader()
            w.writerows(combined)
        log(f"Added {len(added)} price rows (total {len(combined)}); {failed} tickers failed")
    else:
        log(f"No new price rows to add ({failed} tickers failed)")

    # ---- shares outstanding: only tickers we have never resolved ----
    sh_path = DATA_DIR / "shares_outstanding.csv"
    sh_rows = load_rows(sh_path, ["stock_code", "shares_outstanding"])
    known = {r["stock_code"] for r in sh_rows}
    missing = [c for c in codes if c not in known]
    log(f"Shares outstanding: {len(known)} known, {len(missing)} missing")

    new_sh = 0
    for code in missing:
        val = fetch_shares(session, crumb, code)
        if val:
            sh_rows.append({"stock_code": code, "shares_outstanding": str(int(val))})
            new_sh += 1
        time.sleep(REQUEST_DELAY)
    if new_sh:
        sh_rows.sort(key=lambda r: r["stock_code"])
        with open(sh_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["stock_code", "shares_outstanding"])
            w.writeheader()
            w.writerows(sh_rows)
        log(f"Added {new_sh} shares-outstanding rows (total {len(sh_rows)})")
    else:
        log("No new shares-outstanding rows "
            "(remaining gaps are tickers Yahoo does not cover)")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\nFAILED: {e}", flush=True)
        sys.exit(1)
