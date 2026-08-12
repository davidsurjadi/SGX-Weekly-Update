#!/usr/bin/env python3
"""
Orchestrate the full SGX fund-flow update pipeline.

Steps:
  1. Download latest SGX report
  2. Parse reports with parse_reports.py
  3. Build ticker summary with build_ticker_summary.py
  4. Render dashboard with render_dashboard.py
  5. Commit and push to GitHub (if in a git repo)

Sends Telegram notifications at start, success, and failure.
"""

import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime

# Add scripts directory to path
SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

from telegram_notifier import TelegramNotifier
from download_sgx_report import (
    get_latest_report_url,
    is_already_downloaded,
    download_report,
    parse_week_from_filename,
    find_missing_reports,
)

# If the newest week in the dataset is older than this, the run is treated as a
# FAILURE even if every step "succeeded". Without this, a run that quietly
# processes nothing still reports green — which is how the week of 3 Aug 2026
# went unnoticed for over a week.
STALENESS_LIMIT_DAYS = 10


def newest_week_in_dataset(data_dir):
    """Return the max week_start present in weekly_top10.csv, or None."""
    import csv as _csv
    path = Path(data_dir) / "weekly_top10.csv"
    if not path.exists():
        return None
    newest = None
    with open(path, newline="", encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            w = (row.get("week_start") or "").strip()
            if w and (newest is None or w > newest):
                newest = w
    return newest


def run_command(cmd, cwd=None, env=None):
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except Exception as e:
        return 1, "", str(e)


def main():
    """Main orchestration."""
    notifier = TelegramNotifier()
    data_dir = Path("data")
    raw_dir = Path("raw_reports")

    # Ensure directories exist
    data_dir.mkdir(exist_ok=True)
    raw_dir.mkdir(exist_ok=True)

    notifier.send_start()

    week_start = None
    total_weeks = 0
    ticker_count = 0
    weeks_downloaded = []

    try:
        # ============ STEP 1: Download ============
        print("\n" + "=" * 60)
        print("STEP 1: Download SGX Report")
        print("=" * 60)

        week_before = newest_week_in_dataset(data_dir)
        print(f"Newest week currently in dataset: {week_before or 'none'}")

        missing = find_missing_reports(str(raw_dir))
        if not missing:
            print("No missing weeks — archive is already in sync with the SGX API.")
        for m_week, m_title, m_url in missing:
            print(f"Downloading {m_title} (week of {m_week})...")
            if not download_report(m_url, m_week, str(raw_dir)):
                raise Exception(f"Failed to download SGX report for week {m_week}")
            weeks_downloaded.append(m_week)

        # week_start = newest week the API knows about, used for the commit message
        week_start = max([m[0] for m in missing]) if missing else week_before

        # ============ STEP 2: Parse Reports ============
        print("\n" + "=" * 60)
        print("STEP 2: Parse Reports")
        print("=" * 60)

        env = os.environ.copy()
        env['RAW_DIR'] = str(raw_dir)
        env['OUT_DIR'] = str(data_dir)

        rc, stdout, stderr = run_command(
            [sys.executable, str(SCRIPTS_DIR / "parse_reports.py")],
            env=env
        )
        print(stdout)
        if rc != 0:
            raise Exception(f"parse_reports.py failed:\n{stderr}")

        # ---- Staleness guard ----
        # A run that processes nothing must not report success. Compare the
        # newest week actually in the rebuilt dataset against today.
        week_after = newest_week_in_dataset(data_dir)
        print(f"Newest week after parse: {week_after or 'none'}")
        if not week_after:
            raise Exception("weekly_top10.csv has no week_start values after parsing")

        age_days = (datetime.now() - datetime.strptime(week_after, "%Y-%m-%d")).days
        print(f"Newest week is {age_days} days old (limit {STALENESS_LIMIT_DAYS}).")
        if age_days > STALENESS_LIMIT_DAYS:
            raise Exception(
                f"STALE DATA: newest week is {week_after} ({age_days} days old). "
                f"SGX may have changed its report list, or the download step is "
                f"silently skipping weeks."
            )
        week_start = week_after

        # ============ STEP 2B: Refresh announcements (best-effort) ============
        # Replaces the old browser-scraping of SGX's announcements page, which
        # managed ~5 tickers/week and left coverage at 10%. This pulls all
        # tickers from SGX's JSON API in one pass. Deliberately non-fatal: a
        # flaky third-party fetch must not fail the whole weekly update.
        print("\n" + "=" * 60)
        print("STEP 2B: Refresh Announcements")
        print("=" * 60)

        env = os.environ.copy()
        env['DATA_DIR'] = str(data_dir)
        rc, stdout, stderr = run_command(
            [sys.executable, str(SCRIPTS_DIR / "fetch_announcements.py")],
            env=env
        )
        print(stdout)
        if rc != 0:
            print(f"WARNING: announcements refresh failed (continuing):\n{stderr[:400]}")

        # ============ STEP 3: Build Ticker Summary ============
        print("\n" + "=" * 60)
        print("STEP 3: Build Ticker Summary")
        print("=" * 60)

        env = os.environ.copy()
        env['DATA_DIR'] = str(data_dir)

        rc, stdout, stderr = run_command(
            [sys.executable, str(SCRIPTS_DIR / "build_ticker_summary.py")],
            env=env
        )
        print(stdout)
        if rc != 0:
            raise Exception(f"build_ticker_summary.py failed:\n{stderr}")

        # Extract real dataset stats for the notification (e.g. "weeks=159 tickers=137 ...")
        for token in stdout.split():
            if token.startswith("weeks="):
                try:
                    total_weeks = int(token.split("=", 1)[1])
                except ValueError:
                    pass
            elif token.startswith("tickers="):
                try:
                    ticker_count = int(token.split("=", 1)[1])
                except ValueError:
                    pass

        # ============ STEP 4: Render Dashboard ============
        print("\n" + "=" * 60)
        print("STEP 4: Render Dashboard")
        print("=" * 60)

        env = os.environ.copy()
        env['DATA_DIR'] = str(data_dir)
        env['OUT_FILE'] = str(Path.cwd() / "sgx_fund_flow_dashboard.html")

        rc, stdout, stderr = run_command(
            [sys.executable, str(SCRIPTS_DIR / "render_dashboard.py")],
            env=env
        )
        print(stdout)
        if rc != 0:
            raise Exception(f"render_dashboard.py failed:\n{stderr}")

        # ============ STEP 5: Git Commit & Push ============
        print("\n" + "=" * 60)
        print("STEP 5: Git Commit & Push")
        print("=" * 60)

        # Check if we're in a git repo
        if Path(".git").exists():
            # Copy dashboard HTML to index.html for GitHub Pages
            import shutil
            shutil.copy("sgx_fund_flow_dashboard.html", "index.html")
            print("Copied sgx_fund_flow_dashboard.html to index.html")

            # Configure git (for GitHub Actions)
            run_command(["git", "config", "user.email", "automation@sgx-update.local"])
            run_command(["git", "config", "user.name", "SGX Update Bot"])

            # Add and commit
            run_command(["git", "add", "-A"])
            commit_msg = f"Weekly update: week of {week_start}" if week_start else "Weekly update"
            rc, stdout, stderr = run_command(["git", "commit", "-m", commit_msg])

            if rc == 0:
                print(stdout)
                # Push
                rc, stdout, stderr = run_command(["git", "push"])
                if rc == 0:
                    print("✅ Pushed to GitHub")
                else:
                    # This must be treated as a hard failure: a "successful" run that
                    # can't push is worse than a visible failure, since it silently
                    # discards the update and reports green.
                    raise Exception(f"git push failed: {stderr}")
            else:
                print("INFO: No changes to commit")
        else:
            print("INFO: Not in a git repo, skipping commit/push")

        # ============ Success ============
        print("\n" + "=" * 60)
        print("SUCCESS: All steps completed")
        print("=" * 60)

        notifier.send_success(week_start or "unknown", total_weeks, ticker_count,
                              weeks_added=weeks_downloaded)
        return 0

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()

        notifier.send_failure(
            step="Pipeline execution",
            error_msg=str(e)[:200]
        )
        return 1


if __name__ == '__main__':
    sys.exit(main())
