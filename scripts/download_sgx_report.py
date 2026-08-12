#!/usr/bin/env python3
"""
Download the latest SGX Fund Flow Weekly Tracker report.

Queries SGX's content API (the same JSON endpoint the data-reports page's
frontend calls internally) to find the newest report, checks if it's already
downloaded, and downloads it if new.

Note: https://www.sgx.com/stock-exchange/data-reports?reportType=203 is a
JavaScript-rendered SPA — the report list never appears in the raw page HTML,
so plain HTML scraping (requests + BeautifulSoup on that URL) always returns
zero results. Calling the underlying JSON API directly is the reliable path.

Returns: week_start date (YYYY-MM-DD) or None if no new report.
"""

import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: Missing required packages. Run: pip install requests")
    sys.exit(1)


def parse_week_from_filename(filename):
    """Extract week_start date from a name like 'SGX Fund Flow Weekly Tracker
    (Week of 27 July 2026)' or the abbreviated form '(Week of 27 Jul 2026)' —
    SGX's API returns titles with the abbreviated month, downloaded filenames
    use the full month, so both must parse."""
    match = re.search(r'Week of (\d{1,2}) (\w+) (\d{4})', filename)
    if not match:
        return None
    day, month_str, year = match.groups()
    dt = None
    for fmt in ('%d %b %Y', '%d %B %Y'):
        try:
            dt = datetime.strptime(f'{day} {month_str} {year}', fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    # Return the Monday of that week
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime('%Y-%m-%d')


def fetch_report_list(limit=20):
    """Query SGX's content API and return [(week_start, title, url), ...].

    Sorted newest week first. Note that SGX orders the raw API response by
    PUBLISH date, not by the week the report covers, and it sometimes
    publishes two weeks on the same day (e.g. "Week of 3 Aug 2026" and
    "Week of 27 Jul 2026" both appeared dated 03 Aug 2026). Callers must not
    assume element 0 is the newest week, so this re-sorts by parsed week and
    returns the whole list for backfill.

    The SGX data-reports page (https://www.sgx.com/stock-exchange/data-reports)
    is a JavaScript-rendered SPA — the report list is not present in the raw
    HTML, it's fetched client-side from this JSON API after page load. Plain
    HTML scraping (requests + BeautifulSoup on the page URL) will always find
    zero results, which is why this calls the underlying API directly instead.
    """
    api_url = "https://api2.sgx.com/content-api"
    params = {
        "queryId": "09434be8973b96b28894aefc57aff9e6c1f8f9c6:funds_flow_reports_list",
        "variables": ('{"limit":%d,"offset":0,"reportType":"203",'
                      '"reportTypeFilterEnabled":true,"lang":"EN"}' % limit),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(api_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch SGX report API: {e}")
        return []

    try:
        results = resp.json()["data"]["list"]["results"]
    except (ValueError, KeyError, TypeError) as e:
        print(f"ERROR: Unexpected SGX API response shape: {e}")
        return []

    reports = []
    for item in results:
        data = item.get("data", {})
        title = data.get("title", "")
        week_start = parse_week_from_filename(title)
        if not week_start:
            continue
        try:
            url = data["report"]["data"]["file"]["data"]["url"]
        except (KeyError, TypeError):
            print(f"WARNING: No file URL for report: {title}")
            continue
        reports.append((week_start, title, url))

    # Sort by the week the report covers, newest first — NOT by publish order.
    reports.sort(key=lambda r: r[0], reverse=True)
    return reports


def find_missing_reports(raw_dir, limit=20):
    """Return [(week_start, title, url), ...] for weeks not yet in raw_dir."""
    reports = fetch_report_list(limit=limit)
    if not reports:
        return []

    have = set()
    for f in os.listdir(raw_dir):
        if f.endswith('.xlsx'):
            w = parse_week_from_filename(f)
            if w:
                have.add(w)

    missing = [r for r in reports if r[0] not in have]
    # Oldest first so the archive fills in chronological order.
    missing.sort(key=lambda r: r[0])

    print(f"SGX API listed {len(reports)} reports; {len(have)} weeks already archived; "
          f"{len(missing)} missing.")
    for week_start, title, _ in missing:
        print(f"  MISSING: {title} (week of {week_start})")
    return missing


def get_latest_report_url():
    """Backwards-compatible helper: return (url, week_start) for the newest week."""
    reports = fetch_report_list()
    if not reports:
        print("ERROR: No Fund Flow reports found via SGX API")
        return None, None
    week_start, title, url = reports[0]
    print(f"Found latest report: {title} (week of {week_start})")
    return url, week_start


def _legacy_get_latest_report_url():
    api_url = "https://api2.sgx.com/content-api"
    params = {
        "queryId": "09434be8973b96b28894aefc57aff9e6c1f8f9c6:funds_flow_reports_list",
        "variables": '{"limit":20,"offset":0,"reportType":"203","reportTypeFilterEnabled":true,"lang":"EN"}',
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(api_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch SGX report API: {e}")
        return None, None

    try:
        payload = resp.json()
        results = payload["data"]["list"]["results"]
    except (ValueError, KeyError, TypeError) as e:
        print(f"ERROR: Unexpected SGX API response shape: {e}")
        return None, None

    if not results:
        print("ERROR: No Fund Flow reports found via SGX API")
        return None, None

    # Results are sorted newest-first
    latest = results[0]["data"]
    report_name = latest.get("title", "")
    try:
        report_url = latest["report"]["data"]["file"]["data"]["url"]
    except (KeyError, TypeError):
        print(f"ERROR: Could not extract file URL from report: {report_name}")
        return None, None

    week_start = parse_week_from_filename(report_name)

    if not week_start:
        print(f"ERROR: Could not parse week from report name: {report_name}")
        return None, None

    print(f"Found latest report: {report_name} (week of {week_start})")
    print(f"URL: {report_url}")

    return report_url, week_start


def is_already_downloaded(week_start, raw_dir):
    """Check if a report for this week already exists in raw_reports/"""
    if not week_start:
        return False

    for f in os.listdir(raw_dir):
        if f.endswith('.xlsx'):
            parsed_week = parse_week_from_filename(f)
            if parsed_week == week_start:
                print(f"Report for week {week_start} already downloaded: {f}")
                return True
    return False


def download_report(report_url, week_start, raw_dir):
    """Download the report file."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        resp = requests.get(report_url, headers=headers, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Failed to download report: {e}")
        return False

    # Extract filename from URL or use a sensible default
    filename = f"SGX Fund Flow Weekly Tracker (Week of {week_start}).xlsx"
    filepath = os.path.join(raw_dir, filename)

    try:
        with open(filepath, 'wb') as f:
            f.write(resp.content)
        print(f"Downloaded: {filepath} ({len(resp.content)} bytes)")
        return True
    except IOError as e:
        print(f"ERROR: Failed to write file: {e}")
        return False


def main():
    """Main entry point."""
    # Ensure raw_reports directory exists
    raw_dir = "raw_reports"
    os.makedirs(raw_dir, exist_ok=True)

    print("=" * 60)
    print("SGX Fund Flow Weekly Tracker Downloader")
    print("=" * 60)

    # Get latest report info
    report_url, week_start = get_latest_report_url()
    if not report_url or not week_start:
        print("ERROR: Could not find report URL")
        return 1

    # Check if already downloaded
    if is_already_downloaded(week_start, raw_dir):
        print("INFO: No new report to download")
        return 0

    # Download
    if download_report(report_url, week_start, raw_dir):
        print(f"SUCCESS: Downloaded week {week_start}")
        print(week_start)  # Print week_start for orchestrator to capture
        return 0
    else:
        print("ERROR: Download failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
