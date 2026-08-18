#!/usr/bin/env python3
"""
Collect The Edge Singapore news headlines for each tracked ticker.

Updates (in DATA_DIR):
    edge_news.csv   stock_code,publish_date,headline,category,url,is_premium,tag_slug
                    (APPEND-ONLY)

WHY THE TAG PAGES AND NOT THE RSS FEED
--------------------------------------
theedgesingapore.com publishes an RSS feed, but it holds only 50 items
spanning roughly 27 hours - a job running even daily would miss most of it,
and only a small fraction of those items concern an SGX-listed counter we
track. The per-company tag pages (/tags/<slug>) instead return that company's
recent coverage with dates, and are server-rendered, so they work from CI.

Each tag page embeds a Next.js __NEXT_DATA__ blob whose props.pageProps.data
.data array carries clean fields (headline, url path, category, publish date,
paywall flag). Reading that JSON is far more stable than scraping the markup.

WHAT THIS DOES NOT DO
---------------------
It records headlines, dates, categories and links only - no summarising and no
interpretation, matching how SGX announcements are already presented. A tag
page also includes articles that merely mention the company in passing, so the
category is stored and displayed to keep that visible rather than implying
every item is company news.

COVERAGE IS UNEVEN BY DESIGN
----------------------------
Slugs are derived from the company name. Large caps resolve reliably; obscure
small caps often have no tag page at all and return 404, which is skipped
harmlessly. Misses are logged so ALIASES below can be extended over time.
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install requests beautifulsoup4")
    sys.exit(1)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
OUT_PATH = DATA_DIR / "edge_news.csv"

BASE = "https://www.theedgesingapore.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# All 138 tag requests failed on the first CI run, fast enough to look like an
# immediate refusal. A bare requests call is trivially distinguishable from a
# browser; this is the full Chrome header set, sent from a session that has
# visited the homepage first.
HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-SG,en-GB;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="120", "Not(A:Brand";v="24", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

TIMEOUT = int(os.environ.get("EDGE_TIMEOUT", "30"))
REQUEST_DELAY = float(os.environ.get("EDGE_DELAY", "0.6"))
# A run that resolves almost nothing means the site changed or is blocking us;
# fail loudly rather than silently recording an empty week.
MIN_TAGS_OK = int(os.environ.get("EDGE_MIN_TAGS", "20"))

FIELDS = ["stock_code", "publish_date", "headline", "category",
          "url", "is_premium", "tag_slug"]

# Names stored in weekly_top10.csv are sometimes abbreviated; map them to the
# slug The Edge actually uses. Extend as the miss log reveals more.
ALIASES = {
    "1R6": "avi-tech-holdings",
    "BEI": "lht-holdings",
    "C6L": "singapore-airlines",
    "S68": "singapore-exchange",
    "5E2": "seatrium",
    "9A4U": "esr-reit",
    "ULG": "ultragreen",
    "A31": "addvalue-technologies",
    "Y3D": "mdr-limited",
    "5UL": "atlantic-navigation",
    "8C8U": "centurion-accommodation-reit",
}


def log(msg):
    print(msg, flush=True)


def slugify(name):
    s = name.lower().replace("&", " ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def tracked_tickers():
    """stock_code -> company name, from the flow data itself."""
    out = {}
    path = DATA_DIR / "weekly_top10.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            code = (r.get("stock_code") or "").strip()
            name = (r.get("stock_name") or "").strip()
            if code and name:
                out[code] = name
    return out


def iso_date(raw):
    """'8/13/2026' -> '2026-08-13'. Returns '' if unparseable."""
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def prime(session):
    """
    Visit the homepage first to pick up cookies and look like a real visit.
    Best-effort; the tag fetches are attempted regardless.
    """
    try:
        r = session.get(BASE + "/", timeout=TIMEOUT)
        log(f"Primed session: HTTP {r.status_code}, "
            f"{len(r.content)} bytes, {len(session.cookies)} cookies")
    except requests.RequestException as e:
        log(f"Priming failed ({e.__class__.__name__}); continuing anyway")


def fetch_tag(session, slug, diag):
    """
    Returns (status, [articles]). status is 'ok' | 'missing' | 'error'.

    `diag` is a counter dict recording WHY requests fail. The first run only
    told us "errors 138", which was useless for diagnosis - a connection error,
    a 403 and a page missing its data blob are three very different problems.
    """
    try:
        r = session.get(f"{BASE}/tags/{slug}", timeout=TIMEOUT,
                        headers={"Referer": BASE + "/",
                                 "Sec-Fetch-Site": "same-origin"})
    except requests.RequestException as e:
        diag[f"exception:{e.__class__.__name__}"] = diag.get(
            f"exception:{e.__class__.__name__}", 0) + 1
        return "error", []
    if r.status_code == 404:
        return "missing", []
    if r.status_code != 200:
        diag[f"http:{r.status_code}"] = diag.get(f"http:{r.status_code}", 0) + 1
        return "error", []

    soup = BeautifulSoup(r.text, "html.parser")
    blob = soup.find("script", id="__NEXT_DATA__")
    if not blob or not blob.string:
        diag["200_but_no_data_blob"] = diag.get("200_but_no_data_blob", 0) + 1
        return "error", []
    try:
        payload = json.loads(blob.string)
        data = payload["props"]["pageProps"]["data"]
        items = data.get("data") or []
    except (ValueError, KeyError, TypeError):
        diag["blob_shape_changed"] = diag.get("blob_shape_changed", 0) + 1
        return "error", []

    articles = []
    for it in items:
        alias = (it.get("alias") or "").strip()
        headline = (it.get("name") or "").strip()
        if not alias or not headline:
            continue
        articles.append({
            "publish_date": iso_date(it.get("publish_date")),
            "headline": headline,
            "category": (it.get("flash_category") or "").strip(),
            "url": alias if alias.startswith("http") else BASE + alias,
            "is_premium": "1" if it.get("isPremium") else "0",
            "tag_slug": slug,
        })
    return "ok", articles


def load_existing():
    if not OUT_PATH.exists():
        return []
    with open(OUT_PATH, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r.get("stock_code")]


def main():
    tickers = tracked_tickers()
    log(f"Tracked tickers: {len(tickers)}")

    session = requests.Session()
    session.headers.update(HEADERS)
    prime(session)

    existing = load_existing()
    have = {(r["stock_code"], r["url"]) for r in existing}
    log(f"Existing news rows: {len(existing)}")

    fresh, ok, missing, errors = [], 0, [], 0
    diag = {}

    for i, (code, name) in enumerate(sorted(tickers.items()), 1):
        slug = ALIASES.get(code) or slugify(name)
        status, articles = fetch_tag(session, slug, diag)

        # Bail out early if we are clearly being refused, rather than spending
        # 90 seconds proving it 138 times over.
        if i >= 8 and ok == 0 and not missing:
            raise RuntimeError(
                f"first {i} tag requests all failed - aborting rather than "
                f"hammering the site. Failure breakdown: {diag}")

        if status == "missing":
            missing.append(f"{code} ({name}) -> /tags/{slug}")
        elif status == "error":
            errors += 1
        else:
            ok += 1
            for a in articles:
                if (code, a["url"]) in have:
                    continue
                have.add((code, a["url"]))
                row = {"stock_code": code}
                row.update(a)
                fresh.append(row)

        if i % 25 == 0:
            log(f"  ...{i}/{len(tickers)} (resolved {ok}, missing {len(missing)})")
        time.sleep(REQUEST_DELAY)

    log(f"Tags resolved {ok} | no tag page {len(missing)} | errors {errors}")
    if diag:
        log(f"Failure breakdown: {diag}")

    if ok < MIN_TAGS_OK:
        raise RuntimeError(
            f"only {ok} tag pages resolved (need >= {MIN_TAGS_OK}). "
            "Refusing to treat this as a real result - leaving edge_news.csv "
            "untouched. The site layout or access policy may have changed.")

    if missing:
        log("No tag page for (add to ALIASES if the name is just abbreviated):")
        for m in missing[:40]:
            log(f"    {m}")
        if len(missing) > 40:
            log(f"    ...and {len(missing) - 40} more")

    if not fresh:
        log("No new articles.")
        return 0

    combined = existing + fresh
    combined.sort(key=lambda r: (r["stock_code"], r.get("publish_date", ""), r["url"]),
                  reverse=False)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(combined)

    covered = len({r["stock_code"] for r in combined})
    log(f"Added {len(fresh)} articles (total {len(combined)}) "
        f"across {covered} tickers")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\nFAILED: {e}", flush=True)
        sys.exit(1)
