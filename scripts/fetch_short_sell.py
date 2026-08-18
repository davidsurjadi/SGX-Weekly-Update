#!/usr/bin/env python3
"""
Capture the daily SGX short-sell Top-20 snapshots from SGinvestors.io.

Updates (in DATA_DIR):
    short_sell.csv   stock_code,snapshot_date,last_price,total_volume_000,
                     short_sell_volume_000,short_sell_pct,
                     short_sell_value_sgd_000,avg_short_sell_price,source_list
                     (APPEND-ONLY)

WHY THIS EXISTS
---------------
Until now short_sell.csv was a single frozen snapshot captured by hand in a
browser session - nothing in the repo ever refreshed it. The dashboard's
"Short-Sell Snapshot" card was therefore quietly serving one stale day.

The source pages are plain server-rendered HTML (no JavaScript needed), so a
normal requests.get is enough and this runs fine in CI.

APPEND-ONLY BY DESIGN
---------------------
Each run adds rows keyed on (stock_code, snapshot_date, source_list) and never
rewrites an existing row. Run it every trading day and the file accumulates a
genuine short-sell history instead of a single point. A failed or partial run
is harmless - the next run simply adds whatever is still missing.

COVERAGE CAVEAT (surfaced on the dashboard, not hidden here)
------------------------------------------------------------
Each page lists roughly the top 20 counters for that day, ranked by short-sell
volume ratio and by short-sell value. A ticker only appears on days it made one
of those lists, so absence means "not in the top 20", NOT "no short selling".
"""

import csv
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install requests beautifulsoup4")
    sys.exit(1)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
OUT_PATH = DATA_DIR / "short_sell.csv"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-SG,en;q=0.9",
}

SOURCES = [
    ("by_volume", "https://sginvestors.io/market/sgx-top-short-sell-by-volume"),
    ("by_value", "https://sginvestors.io/market/sgx-top-short-sell-by-value"),
]

TIMEOUT = int(os.environ.get("SS_TIMEOUT", "30"))
REQUEST_DELAY = float(os.environ.get("SS_DELAY", "1.0"))

FIELDS = ["stock_code", "snapshot_date", "last_price", "total_volume_000",
          "short_sell_volume_000", "short_sell_pct", "short_sell_value_sgd_000",
          "avg_short_sell_price", "source_list"]

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def log(msg):
    print(msg, flush=True)


def header_date(soup, today=None):
    """
    The column headers read 'Total Volume ('000) on 17-Aug' - day and month but
    no year. Resolve the year by choosing the most recent such date that is not
    in the future, which handles the 31-Dec / 1-Jan rollover correctly.
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
    """'14,663.8' -> '14663.8'."""
    return (text or "").replace(",", "").strip()


def price(text):
    """
    'SGD 0.470' -> '0.470' but 'USD 0.510' -> 'USD 0.510'.

    Matches the convention already in short_sell.csv: SGD is the default and is
    left implicit, while a non-SGD quote currency is kept so it stays visible.
    """
    t = num(text)
    return re.sub(r"^SGD\s+", "", t)


def parse_page(html, source_list):
    soup = BeautifulSoup(html, "html.parser")
    snapshot = header_date(soup)
    if not snapshot:
        log(f"  {source_list}: could not read the snapshot date from headers")
        return []

    table = soup.find("table", class_=re.compile(r"short-sell")) or soup.find("table")
    if not table:
        log(f"  {source_list}: no table found")
        return []

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 10:
            continue
        label = cells[2].get_text(" ", strip=True)
        m = re.search(r"\(SGX:\s*([A-Z0-9]+)\)", label)
        if not m:
            continue  # header row, or a row without a resolvable ticker
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


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    scraped, failures = [], 0
    for source_list, url in SOURCES:
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                log(f"  {source_list}: HTTP {r.status_code}")
                failures += 1
            else:
                scraped.extend(parse_page(r.text, source_list))
        except requests.RequestException as e:
            log(f"  {source_list}: request failed ({e.__class__.__name__})")
            failures += 1
        time.sleep(REQUEST_DELAY)

    if failures == len(SOURCES):
        raise RuntimeError(
            "every short-sell page failed - leaving short_sell.csv untouched")
    if not scraped:
        log("Nothing parsed; existing data left untouched.")
        return 0

    existing = load_existing()
    have = {(r["stock_code"], r["snapshot_date"], r.get("source_list", ""))
            for r in existing}
    fresh = [r for r in scraped
             if (r["stock_code"], r["snapshot_date"], r["source_list"]) not in have]

    if not fresh:
        log(f"No new rows - this snapshot is already recorded "
            f"(file holds {len(existing)} rows).")
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
