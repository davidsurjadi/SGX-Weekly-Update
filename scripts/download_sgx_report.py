#!/usr/bin/env python3
"""
Download the latest SGX Fund Flow Weekly Tracker report.

Scrapes https://www.sgx.com/stock-exchange/data-reports?reportType=203
to find the newest report, checks if already downloaded, and downloads if new.

Returns: week_start date (YYYY-MM-DD) or None if no new report.
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: Missing required packages. Run: pip install requests beautifulsoup4")
    sys.exit(1)


def parse_week_from_filename(filename):
    """Extract week_start date from filename like 'SGX Fund Flow Weekly Tracker (Week of 27 July 2026).xlsx'"""
    match = re.search(r'Week of (\d{1,2}) (\w+) (\d{4})', filename)
    if not match:
        return None
    day, month_str, year = match.groups()
    try:
        month_map = {
            'January': 1, 'February': 2, 'March': 3, 'April': 4, 'May': 5, 'June': 6,
            'July': 7, 'August': 8, 'September': 9, 'October': 10, 'November': 11, 'December': 12
        }
        month = month_map[month_str]
        dt = datetime(int(year), month, int(day))
        # Return the Monday of that week
        monday = dt - (dt.weekday() * (dt.weekday() > 0))
        return monday.strftime('%Y-%m-%d')
    except (ValueError, KeyError):
        return None


def get_latest_report_url():
    """Fetch SGX data reports page and extract latest Fund Flow report URL."""
    url = "https://www.sgx.com/stock-exchange/data-reports?reportType=203"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch SGX page: {e}")
        return None, None

    soup = BeautifulSoup(resp.text, 'html.parser')

    # Find all links containing "Fund Flow" and ".xlsx"
    links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text(strip=True)
        if 'Fund Flow' in text and '.xlsx' in href:
            links.append((href, text))

    if not links:
        print("ERROR: No Fund Flow reports found on SGX page")
        return None, None

    # Take the first (newest) link
    report_url, report_name = links[0]
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
    try:
        resp = requests.get(report_url, timeout=30)
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
