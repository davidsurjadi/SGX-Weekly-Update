#!/usr/bin/env python3
"""
Send Telegram notifications for SGX pipeline events.

Usage:
    notifier = TelegramNotifier(token, chat_id)
    notifier.send_start()
    notifier.send_success(week_start, total_weeks, ticker_count)
    notifier.send_failure(step, error_msg)
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

SG_TZ = timezone(timedelta(hours=8))
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://davidsurjadi.github.io/SGX-Weekly-Update/")


def now_sgt_str() -> str:
    """Return the current time in SGT, e.g. '07 Aug 2026, 10:09 AM SGT'."""
    now_sg = datetime.now(timezone.utc).astimezone(SG_TZ)
    return now_sg.strftime("%d %b %Y, %I:%M %p") + " SGT"


def next_monday_sgt_str() -> str:
    """Return the date of the next scheduled run, e.g. 'Tuesday, 11 Aug 2026, 8:00 AM SGT'.

    Kept the old function name so callers don't need to change; the schedule
    itself runs Tuesday 8:00 AM SGT (see .github/workflows/sgx-weekly-update.yml).
    """
    now_sg = datetime.now(timezone.utc).astimezone(SG_TZ)
    days_ahead = (1 - now_sg.weekday()) % 7  # Tuesday == 1
    if days_ahead == 0:
        days_ahead = 7  # today is Tuesday and this run already happened; next one is in 7 days
    next_run = now_sg + timedelta(days=days_ahead)
    return next_run.strftime("%A, %d %b %Y") + ", 8:00 AM SGT"


class TelegramNotifier:
    """Send messages to Telegram."""

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        """
        Initialize with Telegram bot token and chat ID.
        Falls back to environment variables TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
        """
        self.token = token or os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = chat_id or os.getenv('TELEGRAM_CHAT_ID')
        self.enabled = bool(self.token and self.chat_id)

        if not self.enabled:
            print("WARNING: Telegram not configured (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing)")

    def send_message(self, text: str) -> bool:
        """Send a message to Telegram. Returns True if sent, False otherwise."""
        if not self.enabled:
            print(f"[TELEGRAM DISABLED] {text[:100]}")
            return False

        try:
            import requests
        except ImportError:
            print("ERROR: requests package not installed")
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'HTML'
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            else:
                print(f"WARNING: Telegram API error: {resp.status_code}")
                return False
        except Exception as e:
            print(f"WARNING: Failed to send Telegram message: {e}")
            return False

    def send_start(self):
        """Notify that pipeline is starting."""
        msg = f"""🚀 <b>SGX Fund Flow Update Started</b>

⏰ {now_sgt_str()}
Fetching the latest report..."""
        self.send_message(msg)

    def send_success(self, week_start: str, total_weeks: int = 0, ticker_count: int = 0):
        """Notify successful completion with stats and a link to the live dashboard."""
        msg = f"""✅ <b>SGX Fund Flow Dashboard Updated</b>

📅 Week of {week_start}
📊 {total_weeks} weeks tracked · {ticker_count} tickers

🔗 <a href="{DASHBOARD_URL}">View Dashboard</a>

⏰ Next update: {next_monday_sgt_str()}"""
        self.send_message(msg)

    def send_failure(self, step: str, error_msg: str):
        """Notify of failure at a specific step."""
        msg = f"""❌ <b>SGX Weekly Update Failed</b>

<b>Step:</b> {step}
<b>Error:</b> <code>{error_msg[:200]}</code>

Check GitHub Actions logs for details.
"""
        self.send_message(msg)


if __name__ == '__main__':
    # Test mode
    notifier = TelegramNotifier()
    notifier.send_start()
    notifier.send_success("2026-07-27", total_weeks=159, ticker_count=137)
