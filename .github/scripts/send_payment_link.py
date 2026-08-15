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
TOKEN = os.environ["INVITE_TOKEN"].strip()


def d1_query(sql: str, params: list | None = None) -> list[dict]:
    response = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{DATABASE_ID}/query",
        headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
        json={"sql": sql, "params": params or []},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 query failed: {data}")
    return data["result"][0]["results"]


rows = d1_query(
    "SELECT email, used_at, deleted_at FROM upload_invites WHERE token = ?", [TOKEN]
)
if not rows:
    raise SystemExit(f"No invite found for token {TOKEN}")
row = rows[0]
if row["deleted_at"]:
    raise SystemExit("This invite has already been deleted -- not sending a payment link.")
if not row["used_at"]:
    raise SystemExit("This invite hasn't uploaded a file yet -- not sending a payment link.")
email = row["email"]
if not email:
    raise SystemExit("This invite has no email on file -- can't send a payment link.")

pay_url = f"https://personalknowhow.com/mock-pay?token={TOKEN}"

body = f"""Hi,

Your PersonalKnowHow upload was received. One step left -- complete payment to start processing:

{pay_url}

(Test mode: Stripe isn't wired up yet, so this link simulates a completed payment instead of
charging anything.)

Georgi
"""

SEND_AS = "PersonalKnowHow <office@personalknowhow.com>"

msg = MIMEMultipart("alternative")
msg["Subject"] = "Complete payment to process your PersonalKnowHow upload"
msg["From"] = SEND_AS
msg["To"] = email
msg.attach(MIMEText(body, "plain", "utf-8"))

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
    smtp.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
    smtp.sendmail("office@personalknowhow.com", [email], msg.as_string())

print(f"Payment link sent to {email} (token {TOKEN})")
