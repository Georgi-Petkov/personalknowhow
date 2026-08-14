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
            "SELECT label, customer_label, deleted_at, mcp_url FROM upload_invites "
            "WHERE deleted_at IS NOT NULL AND worker_cleaned_up_at IS NULL "
            "AND customer_label IS NOT NULL "
            "ORDER BY deleted_at ASC"
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
    print("No pending Worker cleanups -- skipping email.")
    raise SystemExit(0)

lines = []
for row in rows:
    label = row["customer_label"]
    lines.append(
        f"- {row['label']} (deleted {row['deleted_at']})\n"
        f"    Worker: {row['mcp_url'] or '(unknown)'}\n"
        f"    Teardown:  cd mcp-{label} && npx wrangler delete\n"
        f"    Mark done: npx wrangler d1 execute personalknowhow-waitlist --remote --command "
        f"\"UPDATE upload_invites SET worker_cleaned_up_at = datetime('now') WHERE customer_label = '{label}'\""
    )
body = (
    f"{len(rows)} deleted customer(s) still have a deployed Worker to tear down:\n\n"
    + "\n\n".join(lines)
)

msg = MIMEMultipart("alternative")
msg["Subject"] = f"PersonalKnowHow: {len(rows)} Worker(s) need manual cleanup"
msg["From"] = GMAIL_SENDER
msg["To"] = GMAIL_SENDER
msg.attach(MIMEText(body, "plain", "utf-8"))

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
    smtp.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
    smtp.sendmail(GMAIL_SENDER, [GMAIL_SENDER], msg.as_string())

print(f"Sent cleanup digest for {len(rows)} pending Worker(s) to {GMAIL_SENDER}")
