#!/usr/bin/env python3
"""
Fetch SGX company announcements for every tracked ticker via SGX's JSON API.

Replaces the old browser-scraping approach (Step 7D), which drove the
announcements page UI one ticker at a time, was prone to hanging on
"Loading...", and only managed ~5 tickers per week. That capped coverage at
14 of 137 tickers (10%) after months of running. This pulls every ticker in
a single pass.

HOW THE API IS REACHED
----------------------
www.sgx.com is a JavaScript SPA; the announcements list is fetched client-side
from https://api.sgx.com/announcements/v1.1/ . That endpoint requires an
`authorizationToken` header. The SPA obtains it from a CMS endpoint named
(misleadingly) `we_chat_qr_validator` and ROT13-decodes the value before use.
Both the endpoint list and this token dance are read from the site's own
appconfig.json / JS bundle, so this mirrors exactly what the page itself does
to render data that is public.

OUTPUTS
-------
  data/shareholder_announcements.csv  ANNC14 only  (existing schema, unchanged)
  data/company_announcements.csv      everything else
"""

import codecs
import csv
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

TOKEN_URL = ("https://api2.sgx.com/content-api/?queryId="
             "09434be8973b96b28894aefc57aff9e6c1f8f9c6:we_chat_qr_validator")
API_BASE = "https://api.sgx.com/announcements/v1.1/"

# Category of interest for the existing disclosures panel.
DISCLOSURE_CAT = "ANNC14"          # Disclosure of Interest / Changes in Interest

# High-volume routine filings. Singtel and DBS file these most trading days;
# across a 6-ticker sample they were 52% of all records. Kept but capped so
# they cannot bury material news like a regulatory action or profit warning.
NOISY_CATS = {
    "ANNC13",   # Share Buy Back - On Market
    "ANNC15",   # Employee Stock Option / Share Scheme
}
NOISY_KEEP_PER_TICKER = 3

PAGE_SIZE = int(os.environ.get("ANNC_PAGE_SIZE", "100"))
REQUEST_DELAY = float(os.environ.get("ANNC_DELAY", "0.4"))


def get_token(session):
    """Fetch and ROT13-decode the API token the SGX SPA uses."""
    r = session.get(TOKEN_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    raw = (r.json().get("data") or {}).get("qrValidator")
    if not raw:
        raise RuntimeError("SGX token endpoint returned no qrValidator")
    return codecs.encode(raw, "rot_13")


def tracked_tickers():
    """Every stock_code that has appeared in a weekly Top 10."""
    path = DATA_DIR / "weekly_top10.csv"
    codes = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            c = (row.get("stock_code") or "").strip()
            if c:
                codes.setdefault(c, (row.get("stock_name") or "").strip())
    return codes


def fetch_ticker(session, token, code):
    """Return raw announcement records for one ticker, or None on failure."""
    try:
        r = session.get(API_BASE + "securitycode", headers={**HEADERS,
                        "authorizationToken": token},
                        params={"value": code, "pagestart": 0,
                                "pagesize": PAGE_SIZE}, timeout=40)
    except requests.RequestException as e:
        print(f"  {code}: request failed ({e})")
        return None
    if r.status_code in (401, 403):
        return "EXPIRED"
    if r.status_code != 200:
        print(f"  {code}: HTTP {r.status_code}")
        return None
    try:
        return r.json().get("data") or []
    except ValueError:
        print(f"  {code}: bad JSON")
        return None


def iso_date(rec):
    d = str(rec.get("submission_date") or "")
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else ""


def notice_type(title):
    """Match the existing CSV's vocabulary so the current panel keeps working."""
    t = (title or "").lower()
    if "substantial" in t:
        return "substantial_shareholder"
    return "director"


def load_existing(path, key_field="url"):
    """Existing rows keyed by URL, so reruns are idempotent."""
    rows, keys = [], set()
    if path.exists():
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows.append(row)
                keys.add(row.get(key_field, ""))
    return rows, keys


def main():
    codes = tracked_tickers()
    print(f"Tracked tickers: {len(codes)}")

    session = requests.Session()
    token = get_token(session)
    print("Token acquired.")

    disc_path = DATA_DIR / "shareholder_announcements.csv"
    annc_path = DATA_DIR / "company_announcements.csv"
    disc_rows, disc_keys = load_existing(disc_path)
    annc_rows, annc_keys = load_existing(annc_path)
    print(f"Existing: {len(disc_rows)} disclosures, {len(annc_rows)} announcements")

    ok = failed = new_disc = new_annc = 0

    for i, code in enumerate(sorted(codes), 1):
        recs = fetch_ticker(session, token, code)
        if recs == "EXPIRED":
            print("  token expired, refreshing...")
            token = get_token(session)
            recs = fetch_ticker(session, token, code)
        if recs is None:
            failed += 1
            continue
        ok += 1

        noisy_seen = {}
        for rec in recs:
            url = rec.get("url") or ""
            if not url:
                continue
            sub = rec.get("sub") or ""
            title = (rec.get("title") or "").strip()
            date = iso_date(rec)
            company = (rec.get("issuer_name") or rec.get("security_name") or "").strip()

            if sub == DISCLOSURE_CAT:
                if url in disc_keys:
                    continue
                disc_keys.add(url)
                disc_rows.append({
                    "stock_code": code, "company_name": company, "date": date,
                    "notice_type": notice_type(title), "title": title, "url": url,
                })
                new_disc += 1
            else:
                if sub in NOISY_CATS:
                    n = noisy_seen.get(sub, 0)
                    if n >= NOISY_KEEP_PER_TICKER:
                        continue
                    noisy_seen[sub] = n + 1
                if url in annc_keys:
                    continue
                annc_keys.add(url)
                annc_rows.append({
                    "stock_code": code, "company_name": company, "date": date,
                    "category": (rec.get("category_name") or "").strip(),
                    "category_code": sub, "title": title, "url": url,
                })
                new_annc += 1

        if i % 20 == 0:
            print(f"  ...{i}/{len(codes)} tickers")
        time.sleep(REQUEST_DELAY)

    disc_rows.sort(key=lambda r: (r.get("stock_code", ""), r.get("date", "")), reverse=True)
    annc_rows.sort(key=lambda r: (r.get("stock_code", ""), r.get("date", "")), reverse=True)

    with open(disc_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["stock_code", "company_name", "date",
                                           "notice_type", "title", "url"])
        w.writeheader(); w.writerows(disc_rows)

    with open(annc_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["stock_code", "company_name", "date",
                                           "category", "category_code", "title", "url"])
        w.writeheader(); w.writerows(annc_rows)

    covered = len({r["stock_code"] for r in disc_rows} | {r["stock_code"] for r in annc_rows})
    print(f"\nTickers fetched OK: {ok}, failed: {failed}")
    print(f"New rows: {new_disc} disclosures, {new_annc} announcements")
    print(f"Totals: {len(disc_rows)} disclosures, {len(annc_rows)} announcements")
    print(f"Coverage: {covered}/{len(codes)} tickers ({round(100*covered/len(codes))}%)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
