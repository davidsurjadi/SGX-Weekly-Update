import json
import os
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(SCRIPT_DIR, "..", "data"))
OUT_FILE = os.environ.get("OUT_FILE", os.path.join(SCRIPT_DIR, "..", "sgx_fund_flow_dashboard.html"))
# data.json is written alongside OUT_FILE by default so the page's relative fetch('data.json') resolves.
DATA_OUT_FILE = os.environ.get("DATA_OUT_FILE", os.path.join(os.path.dirname(os.path.abspath(OUT_FILE)), "data.json"))

template = open(os.path.join(SCRIPT_DIR, "dashboard_template.html"), encoding="utf-8").read()
data = json.load(open(os.path.join(DATA_DIR, "dashboard_data.json")))

# Add last updated timestamp in Singapore timezone (UTC+8, fixed offset — SGX/SGT doesn't observe DST)
SG_TZ = timezone(timedelta(hours=8))
now_sg = datetime.now(timezone.utc).astimezone(SG_TZ)

# Format: "08 Aug 2026, 09:15 AM SGT"
last_updated = now_sg.strftime("%d %b %Y, %I:%M %p") + " SGT"
data['last_updated'] = last_updated

data_json = json.dumps(data, separators=(",", ":"))

with open(DATA_OUT_FILE, "w", encoding="utf-8") as f:
    f.write(data_json)

with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write(template)

print(f"Wrote {OUT_FILE} ({len(template)//1024} KB) and {DATA_OUT_FILE} ({len(data_json)//1024} KB)")
