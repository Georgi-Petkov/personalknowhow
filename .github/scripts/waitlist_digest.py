import os

import requests

ACCOUNT_ID = "716dcf9e5806cc9dbb777e0b80bb236d"
DATABASE_ID = "934afff4-c462-41b0-91ff-2232473c5286"
SEND_AS = "PersonalKnowHow <office@personalknowhow.com>"
ADMIN_EMAIL = "2georgipetkov@gmail.com"

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]

response = requests.post(
    f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{DATABASE_ID}/query",
    headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
    json={
        "sql": (
            "SELECT email, note, created_at FROM subscribers "
            "WHERE created_at > datetime('now', '-1 day') "
            "ORDER BY created_at DESC"
        )
    },
    timeout=30,
)
response.raise_for_status()
data = response.json()
if not data.get("success"):
    raise RuntimeError(f"D1 query failed: {data}")

rows = data["result"][0]["results"]

if not rows:
    print("No new signups in the last 24h -- skipping email.")
    raise SystemExit(0)

lines = []
for row in rows:
    line = f"- {row['email']}  ({row['created_at']})"
    if row.get("note"):
        line += f"\n  {row['note']}"
    lines.append(line)
body = f"{len(rows)} new waitlist signup(s) in the last 24h:\n\n" + "\n".join(lines)

response = requests.post(
    "https://api.resend.com/emails",
    headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
    json={
        "from": SEND_AS,
        "to": [ADMIN_EMAIL],
        "subject": f"PersonalKnowHow: {len(rows)} new waitlist signup(s)",
        "text": body,
    },
    timeout=30,
)
response.raise_for_status()

print(f"Sent digest for {len(rows)} new signup(s) to {ADMIN_EMAIL}")
