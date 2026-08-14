import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

ACCOUNT_ID = "716dcf9e5806cc9dbb777e0b80bb236d"
DATABASE_ID = "934afff4-c462-41b0-91ff-2232473c5286"

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
GMAIL_SENDER = os.environ["GMAIL_SENDER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

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

msg = MIMEMultipart("alternative")
msg["Subject"] = f"PersonalKnowHow: {len(rows)} new waitlist signup(s)"
msg["From"] = GMAIL_SENDER
msg["To"] = GMAIL_SENDER
msg.attach(MIMEText(body, "plain", "utf-8"))

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
    smtp.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
    smtp.sendmail(GMAIL_SENDER, [GMAIL_SENDER], msg.as_string())

print(f"Sent digest for {len(rows)} new signup(s) to {GMAIL_SENDER}")
