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
)


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


def get_file_size_mb(filepath):
    """Get file size in MB."""
    try:
        return os.path.getsize(filepath) / 1024 / 1024
    except:
        return 0


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
    new_rows = 0

    try:
        # ============ STEP 1: Download ============
        print("\n" + "=" * 60)
        print("STEP 1: Download SGX Report")
        print("=" * 60)

        report_url, week_start = get_latest_report_url()
        if not report_url:
            raise Exception("Could not find latest SGX report URL")

        if is_already_downloaded(week_start, str(raw_dir)):
            print(f"Report for week {week_start} already downloaded. Skipping download.")
        else:
            if not download_report(report_url, week_start, str(raw_dir)):
                raise Exception("Failed to download SGX report")

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

        # Extract stats from stdout
        if "weekly_top10 rows:" in stdout:
            try:
                new_rows = int(stdout.split("weekly_top10 rows:")[1].split()[0])
            except:
                pass

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

        file_sizes = {
            "data.json": get_file_size_mb(data_dir / "dashboard_data.json"),
            "index.html": get_file_size_mb("index.html"),
        }

        notifier.send_success(week_start or "unknown", new_rows, file_sizes)
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
