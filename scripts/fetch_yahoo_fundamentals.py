#!/usr/bin/env python3
"""
Refresh the Yahoo Finance-sourced fundamentals behind the SGX dashboard.

Produces (in DATA_DIR):
    results_summaries.csv   reported revenue / net income / EPS vs estimate;
                            quarterly rows where Yahoo has quarters, else the
                            ticker's ANNUAL statements tagged period_type=annual
                            (most SGX mid/small caps report half-yearly)
    analyst_forecasts.csv   forward consensus EPS + revenue (annual periods only)
    reporting_currency.csv  each ticker's reporting currency (often NOT SGD)
    data_freshness.csv      when the above were last fetched

WHY THIS EXISTS
---------------
The weekly pipeline parses SGX's own fund-flow report. It does not touch these
four files, so without this script they go stale and the dashboard keeps showing
figures from whenever a human last refreshed them by hand.

HOW YAHOO IS REACHED
--------------------
Yahoo's quoteSummary API needs a cookie + a matching "crumb" token. The flow is:
seed cookies from fc.yahoo.com, request a crumb, then send both on every call.
This is the same handshake a browser performs; no login or private data.

Yahoo sometimes refuses datacenter IPs, which is exactly what a CI runner is. If
that happens this script must fail loudly rather than write empty files over
good data, so a coverage gate below aborts the whole run unless enough tickers
came back. A visible red build is far better than silently blanking the
dashboard.
"""

import codecs  # noqa: F401  (kept: mirrors fetch_announcements.py's toolkit)
import csv
import os
import sys
import time
from datetime import date, datetime
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

CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
COOKIE_SEEDS = ["https://fc.yahoo.com", "https://finance.yahoo.com/quote/D05.SI/"]
SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}"

REQUEST_DELAY = float(os.environ.get("YF_DELAY", "0.6"))
TIMEOUT = int(os.environ.get("YF_TIMEOUT", "30"))

# Abort rather than overwrite good data with a near-empty result.
MIN_TICKERS_OK = int(os.environ.get("YF_MIN_TICKERS", "60"))
# Analyst estimates from 1-2 contributors are noise; require a real consensus.
MIN_ANALYSTS = 3
# Drop quarters older than this - Yahoo's SGX coverage is full of stale stubs.
OLDEST_QUARTER = os.environ.get("YF_OLDEST_QUARTER", "2024-06-30")


def log(msg):
    print(msg, flush=True)


def make_session():
    """Seed cookies then fetch the crumb that must accompany every API call."""
    s = requests.Session()
    s.headers.update(HEADERS)
    for url in COOKIE_SEEDS:
        try:
            s.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            log(f"  cookie seed {url} failed ({e}) - continuing")
        if s.cookies:
            break
    r = s.get(CRUMB_URL, timeout=TIMEOUT)
    if r.status_code != 200 or not r.text or "<" in r.text:
        raise RuntimeError(
            f"could not obtain Yahoo crumb (HTTP {r.status_code}, body {r.text[:120]!r}). "
            "Yahoo commonly blocks datacenter IP ranges - if this is CI, that is the likely cause."
        )
    return s, r.text.strip()


def fetch_modules(session, crumb, code, modules):
    sym = f"{code}.SI"
    try:
        r = session.get(SUMMARY_URL.format(sym=sym),
                        params={"modules": modules, "crumb": crumb},
                        timeout=TIMEOUT)
    except requests.RequestException as e:
        return None, f"request failed ({e})"
    if r.status_code in (401, 403):
        return None, "EXPIRED"
    if r.status_code == 404:
        return {}, None          # ticker simply not covered
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        res = (r.json().get("quoteSummary", {}).get("result") or [{}])[0]
    except ValueError:
        return None, "bad JSON"
    return res, None


def raw(node, *path):
    """Safely walk Yahoo's {'raw':..,'fmt':..} nesting."""
    cur = node
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    if isinstance(cur, dict):
        cur = cur.get("raw")
    return cur


def tracked_tickers():
    path = DATA_DIR / "weekly_top10.csv"
    codes = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            c = (row.get("stock_code") or "").strip()
            if c and c not in codes:
                codes.append(c)
    return sorted(codes)


def results_announcements():
    """Map each ticker to its results filings, oldest first, for date linking."""
    path = DATA_DIR / "company_announcements.csv"
    out = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            cat = r.get("category") or ""
            title = r.get("title") or ""
            if "Financial Statements" not in cat or "Notification" in title:
                continue
            out.setdefault(r["stock_code"], []).append(r)
    for k in out:
        out[k].sort(key=lambda x: x.get("date", ""))
    return out


def pct(cur, prev):
    if cur is None or prev in (None, 0):
        return ""
    return round((cur - prev) / abs(prev) * 100, 1)


# Yahoo's SGX history is often a recent quarter followed by a years-old stub.
# Comparing across that gap produces nonsense (+8700%), so only treat the
# previous entry as "the prior quarter" when it really is one.
MAX_QUARTER_GAP_DAYS = 130


def adjacent(cur_end, prev_end):
    try:
        a = datetime.strptime(cur_end, "%Y-%m-%d").date()
        b = datetime.strptime(prev_end, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return 0 < (a - b).days <= MAX_QUARTER_GAP_DAYS


def main():
    codes = tracked_tickers()
    log(f"Tracked tickers: {len(codes)}")

    session, crumb = make_session()
    log("Crumb acquired.")

    annc = results_announcements()
    results, forecasts, currencies = [], [], {}
    ok = failed = 0

    for i, code in enumerate(codes, 1):
        res, err = fetch_modules(
            session, crumb, code,
            "incomeStatementHistoryQuarterly,incomeStatementHistory,earningsHistory,earningsTrend,financialData")
        if err == "EXPIRED":
            log("  crumb expired, renewing...")
            session, crumb = make_session()
            res, err = fetch_modules(
                session, crumb, code,
                "incomeStatementHistoryQuarterly,incomeStatementHistory,earningsHistory,earningsTrend,financialData")
        if err:
            log(f"  {code}: {err}")
            failed += 1
            time.sleep(REQUEST_DELAY)
            continue
        ok += 1

        cur = (res.get("financialData") or {}).get("financialCurrency")
        if cur:
            currencies[code] = cur

        # EPS actual vs estimate, keyed by quarter end
        surprise = {}
        for h in (res.get("earningsHistory") or {}).get("history", []) or []:
            q = raw(h, "quarter")
            qs = (h.get("quarter") or {}).get("fmt")
            act, est = raw(h, "epsActual"), raw(h, "epsEstimate")
            if qs and act is not None and est is not None:
                surprise[qs] = (act, est, raw(h, "surprisePercent"))

        # Reported quarters, newest first
        stmts = []
        for s in (res.get("incomeStatementHistoryQuarterly") or {}).get(
                "incomeStatementHistory", []) or []:
            end = (s.get("endDate") or {}).get("fmt")
            if not end:
                continue
            rev, ni = raw(s, "totalRevenue"), raw(s, "netIncome")
            # Yahoo emits 0 where it has no figure; treat that as missing.
            stmts.append({"end": end,
                          "rev": rev if rev else None,
                          "ni": ni if ni else None})
        stmts.sort(key=lambda x: x["end"], reverse=True)

        rows_before_quarters = len(results)
        for idx, q in enumerate(stmts):
            if q["end"] < OLDEST_QUARTER:
                continue
            prev = stmts[idx + 1] if idx + 1 < len(stmts) else None
            if prev and not adjacent(q["end"], prev["end"]):
                prev = None
            a, e, sp = surprise.get(q["end"], (None, None, None))
            if q["rev"] is None and q["ni"] is None and a is None:
                continue
            filing = next((x for x in annc.get(code, []) if x["date"] >= q["end"]), None)
            results.append({
                "stock_code": code,
                "period_end": q["end"],
                "period_type": "quarterly",
                "currency": cur or "",
                "revenue": q["rev"] if q["rev"] is not None else "",
                "net_income": q["ni"] if q["ni"] is not None else "",
                "revenue_qoq_pct": pct(q["rev"], prev["rev"]) if prev else "",
                "net_income_qoq_pct": pct(q["ni"], prev["ni"]) if prev else "",
                "eps_actual": a if a is not None else "",
                "eps_estimate": e if e is not None else "",
                "surprise_pct": round(sp * 100, 1) if sp is not None else "",
                "announcement_date": filing["date"] if filing else "",
                "announcement_url": filing["url"] if filing else "",
            })

        # Most SGX mid/small caps report half-yearly, so Yahoo carries no
        # quarterly income statements for them -- which is why this feed covered
        # only 24 of 138 tracked codes. For exactly those tickers, publish their
        # ANNUAL statements instead, tagged period_type=annual so no consumer
        # can mistake a year for a quarter. QoQ stays empty (a YoY figure in a
        # "vs prior qtr" column would be a lie) and no surprise attaches: the
        # earningsHistory surprises are quarterly measures.
        if len(results) == rows_before_quarters:
            annual = []
            for st in (res.get("incomeStatementHistory") or {}).get(
                    "incomeStatementHistory", []) or []:
                end = (st.get("endDate") or {}).get("fmt")
                if not end or end < OLDEST_QUARTER:
                    continue
                rev_a, ni_a = raw(st, "totalRevenue"), raw(st, "netIncome")
                # Yahoo emits 0 where it has no figure; treat that as missing.
                if not rev_a and not ni_a:
                    continue
                annual.append({"end": end,
                               "rev": rev_a if rev_a else None,
                               "ni": ni_a if ni_a else None})
            annual.sort(key=lambda x: x["end"], reverse=True)
            for y in annual:
                filing = next((x for x in annc.get(code, [])
                               if x["date"] >= y["end"]), None)
                results.append({
                    "stock_code": code,
                    "period_end": y["end"],
                    "period_type": "annual",
                    "currency": cur or "",
                    "revenue": y["rev"] if y["rev"] is not None else "",
                    "net_income": y["ni"] if y["ni"] is not None else "",
                    "revenue_qoq_pct": "",
                    "net_income_qoq_pct": "",
                    "eps_actual": "",
                    "eps_estimate": "",
                    "surprise_pct": "",
                    "announcement_date": filing["date"] if filing else "",
                    "announcement_url": filing["url"] if filing else "",
                })

        # Forward consensus - annual periods only, and only a real consensus
        for t in (res.get("earningsTrend") or {}).get("trend", []) or []:
            if t.get("period") not in ("0y", "+1y"):
                continue
            eps = raw(t, "earningsEstimate", "avg")
            rev = raw(t, "revenueEstimate", "avg")
            n = (raw(t, "earningsEstimate", "numberOfAnalysts")
                 or raw(t, "revenueEstimate", "numberOfAnalysts"))
            if not n or n < MIN_ANALYSTS or (eps is None and rev is None):
                continue
            forecasts.append({
                "stock_code": code,
                "period": t["period"],
                "end_date": t.get("endDate", ""),
                "eps_estimate": eps if eps is not None else "",
                "revenue_estimate_m": round(rev / 1e6) if rev else "",
                "num_analysts": int(n),
            })

        if i % 25 == 0:
            log(f"  ...{i}/{len(codes)} ({ok} ok, {failed} failed)")
        time.sleep(REQUEST_DELAY)

    # ---- Coverage gate: never overwrite good data with a blocked run ----
    log(f"\nFetched OK: {ok}, failed: {failed}")
    log(f"results rows {len(results)} | forecast rows {len(forecasts)} | currencies {len(currencies)}")
    if ok < MIN_TICKERS_OK or not currencies:
        raise RuntimeError(
            f"only {ok} tickers returned data (need >= {MIN_TICKERS_OK}). "
            "Refusing to overwrite the existing CSVs - they are almost certainly "
            "better than this run. Most likely Yahoo is blocking this IP."
        )

    results.sort(key=lambda r: (r["stock_code"], r["period_end"]), reverse=True)
    forecasts.sort(key=lambda r: (r["stock_code"], r["period"]))

    def write(name, rows, cols):
        with open(DATA_DIR / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        log(f"  wrote {name} ({len(rows)} rows)")

    write("results_summaries.csv", results,
          ["stock_code", "period_end", "period_type", "currency", "revenue", "net_income",
           "revenue_qoq_pct", "net_income_qoq_pct", "eps_actual", "eps_estimate",
           "surprise_pct", "announcement_date", "announcement_url"])
    write("analyst_forecasts.csv", forecasts,
          ["stock_code", "period", "end_date", "eps_estimate",
           "revenue_estimate_m", "num_analysts"])
    write("reporting_currency.csv",
          [{"stock_code": c, "currency": currencies[c]} for c in sorted(currencies)],
          ["stock_code", "currency"])

    today = date.today().isoformat()
    with open(DATA_DIR / "data_freshness.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["dataset", "fetched_on", "source", "refresh_note"])
        w.writeheader()
        w.writerows([
            {"dataset": "results_summaries", "fetched_on": today,
             "source": "Yahoo Finance quarterly income statements",
             "refresh_note": "Refresh after each SGX reporting season (Mar / Jun / Sep / Dec)"},
            {"dataset": "analyst_forecasts", "fetched_on": today,
             "source": "Yahoo Finance analyst estimates",
             "refresh_note": "Estimates are revised continuously; refresh alongside results"},
            {"dataset": "reporting_currency", "fetched_on": today,
             "source": "Yahoo Finance financialData",
             "refresh_note": "Rarely changes; refresh only if a ticker's figures look mis-scaled"},
        ])
    log(f"  wrote data_freshness.csv (fetched_on {today})")

    from collections import Counter
    log("\nCurrencies: " + ", ".join(f"{k}:{v}" for k, v in
                                     Counter(currencies.values()).most_common()))
    log(f"Tickers with reported results: {len({r['stock_code'] for r in results})}")
    log(f"Tickers with forecasts       : {len({r['stock_code'] for r in forecasts})}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\nFAILED: {e}", flush=True)
        sys.exit(1)
