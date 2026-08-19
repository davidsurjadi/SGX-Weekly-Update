#!/usr/bin/env python3
"""
Capture the daily SGX short-sell Top-20 snapshots using a real browser.

Updates (in DATA_DIR):
    short_sell.csv   stock_code,snapshot_date,last_price,total_volume_000,
                     short_sell_volume_000,short_sell_pct,
                     short_sell_value_sgd_000,avg_short_sell_price,source_list
                     (APPEND-ONLY)

WHY A BROWSER AND NOT requests
------------------------------
An earlier pair of scripts tried plain HTTP and were deleted because they
cannot work on any host. Measured 18-19 Aug 2026:

    GitHub runner + requests   HTTP 200, table body absent, 0 rows
    Local Python + requests    HTTP 200, 0 rows
    Local Chrome               full table, 25 <tr>
    Headless Chromium in CI    full table, 25 <tr>   <- this script

The differentiator is session state the browser accumulates on its own
(sginvestors.io sets cookies from page JavaScript; a requests session ends up
with none), NOT the IP address. So a real browser works fine from CI.

THE ONE NON-OBVIOUS BIT
-----------------------
The rendered DOM of these pages exposes only ~5 <tr> (a summary table). The
full 20-row table comes back from an in-page `fetch()` of the same URL, issued
AFTER navigating there so the session exists. So: navigate first, then fetch
from inside the page context, then parse that HTML. Reading the DOM directly
gets you almost nothing - that was measured, not assumed.

APPEND-ONLY BY DESIGN
---------------------
Rows are keyed on (stock_code, snapshot_date, source_list) and existing rows
are never rewritten or deleted. Run it daily and the file accumulates a real
short-sell history. A failed run writes nothing at all.

COVERAGE CAVEAT (shown on the dashboard, not hidden here)
---------------------------------------------------------
Each page lists roughly the top 20 counters for that trading day, by short-sell
volume ratio and by short-sell value. A ticker appears only on days it made one
of those lists, so absence means "not in the top 20", NOT "no short selling".
"""

import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install beautifulsoup4")
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: pip install playwright && python -m playwright install chromium")
    sys.exit(1)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
OUT_PATH = DATA_DIR / "short_sell.csv"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

HOME = "https://sginvestors.io/"
SOURCES = [
    ("by_volume", "https://sginvestors.io/market/sgx-top-short-sell-by-volume"),
    ("by_value", "https://sginvestors.io/market/sgx-top-short-sell-by-value"),
]

NAV_TIMEOUT = int(os.environ.get("SS_NAV_TIMEOUT", "45000"))
SETTLE_MS = int(os.environ.get("SS_SETTLE_MS", "5000"))

FIELDS = ["stock_code", "snapshot_date", "last_price", "total_volume_000",
          "short_sell_volume_000", "short_sell_pct", "short_sell_value_sgd_000",
          "avg_short_sell_price", "source_list"]

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

IN_PAGE_FETCH = "async () => await fetch(location.href).then(r => r.text())"


def log(msg):
    print(msg, flush=True)


def header_date(soup, today=None):
    """
    Column headers read like "Total Volume ('000) on 18-Aug" - day and month,
    no year. Resolve to the most recent such date that is not in the future,
    which handles the 31-Dec / 1-Jan rollover correctly.
    """
    today = today or date.today()
    text = " ".join(th.get_text(" ", strip=True) for th in soup.find_all("th"))
    m = re.search(r"\bon\s+(\d{1,2})-([A-Z][a-z]{2})\b", text)
    if not m:
        return None
    day, mon = int(m.group(1)), MONTHS.get(m.group(2))
    if not mon:
        return None
    for year in (today.year, today.year - 1):
        try:
            cand = date(year, mon, day)
        except ValueError:
            continue
        if cand <= today:
            return cand.isoformat()
    return None


def num(text):
    return (text or "").replace(",", "").strip()


def price(text):
    """
    'SGD 0.470' -> '0.470'; 'USD 0.510' stays as-is.
    Matches the convention already in short_sell.csv: SGD is implicit, any
    other quote currency is kept so it stays visible.
    """
    return re.sub(r"^SGD\s+", "", num(text))


def parse_page(html, source_list):
    soup = BeautifulSoup(html, "html.parser")
    snapshot = header_date(soup)
    if not snapshot:
        log(f"  {source_list}: could not read the snapshot date from headers")
        return []

    table = soup.find("table", class_=re.compile(r"short-sell")) or soup.find("table")
    if not table:
        log(f"  {source_list}: no table element found")
        return []

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 10:
            continue
        m = re.search(r"\(SGX:\s*([A-Z0-9]+)\)", cells[2].get_text(" ", strip=True))
        if not m:
            continue  # header row, or a row with no resolvable ticker
        rows.append({
            "stock_code": m.group(1),
            "snapshot_date": snapshot,
            "last_price": price(cells[3].get_text(" ", strip=True)),
            "total_volume_000": num(cells[5].get_text(strip=True)),
            "short_sell_volume_000": num(cells[6].get_text(strip=True)),
            "short_sell_pct": num(cells[7].get_text(strip=True)),
            "short_sell_value_sgd_000": num(cells[8].get_text(strip=True)),
            "avg_short_sell_price": num(cells[9].get_text(strip=True)),
            "source_list": source_list,
        })
    log(f"  {source_list}: {len(rows)} rows for {snapshot}")
    return rows


def load_existing():
    if not OUT_PATH.exists():
        return []
    with open(OUT_PATH, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r.get("stock_code")]


def scrape():
    scraped, failures = [], 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="en-SG",
                                  viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        # Land on the homepage first so the session exists before we ask for data.
        try:
            r = page.goto(HOME, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            page.wait_for_timeout(2000)
            log(f"Primed session: HTTP {r.status if r else '?'}, "
                f"{len(ctx.cookies())} cookies")
        except Exception as e:
            log(f"Priming failed ({type(e).__name__}); continuing anyway")

        for source_list, url in SOURCES:
            try:
                # domcontentloaded, not networkidle: ad scripts on these pages
                # may never let the network go quiet.
                r = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                page.wait_for_timeout(SETTLE_MS)
                status = r.status if r else None
                html = page.evaluate(IN_PAGE_FETCH)
                log(f"  {source_list}: HTTP {status}, "
                    f"{len(html)} bytes via in-page fetch, "
                    f"{len(re.findall(r'<tr', html, re.I))} tr")
                if status != 200:
                    failures += 1
                    continue
                scraped.extend(parse_page(html, source_list))
            except Exception as e:
                log(f"  {source_list}: failed ({type(e).__name__}: {str(e)[:120]})")
                failures += 1

        browser.close()
    return scraped, failures


def main():
    scraped, failures = scrape()

    if failures == len(SOURCES):
        raise RuntimeError(
            "every short-sell page failed - short_sell.csv left untouched")
    if not scraped:
        # A 200 with an empty table is NOT a quiet no-op. An earlier version
        # exited 0 here and made a run report success having written nothing,
        # which is the worst possible outcome.
        raise RuntimeError(
            "pages fetched but zero rows parsed - the table body was missing. "
            "Either the session was refused or the page structure changed; "
            "short_sell.csv left untouched.")

    existing = load_existing()
    have = {(r["stock_code"], r["snapshot_date"], r.get("source_list", ""))
            for r in existing}
    fresh = [r for r in scraped
             if (r["stock_code"], r["snapshot_date"], r["source_list"]) not in have]

    if not fresh:
        log(f"No new rows - this snapshot is already recorded "
            f"(file holds {len(existing)} rows). Nothing to commit.")
        return 0

    combined = existing + fresh
    combined.sort(key=lambda r: (r["snapshot_date"], r["source_list"], r["stock_code"]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(combined)

    dates = sorted({r["snapshot_date"] for r in combined})
    log(f"Added {len(fresh)} rows (total {len(combined)}); "
        f"{len(dates)} snapshot dates, {dates[0]} to {dates[-1]}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\nFAILED: {e}", flush=True)
        sys.exit(1)
