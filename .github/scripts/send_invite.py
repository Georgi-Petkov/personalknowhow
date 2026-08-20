import os
import uuid

import requests

ACCOUNT_ID = "716dcf9e5806cc9dbb777e0b80bb236d"
DATABASE_ID = "934afff4-c462-41b0-91ff-2232473c5286"
SEND_AS = "PersonalKnowHow <office@personalknowhow.com>"

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
REQUESTED_EMAIL = os.environ.get("INVITE_EMAIL", "").strip().lower()


def send_email(to: str, subject: str, text: str) -> None:
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": SEND_AS, "to": [to], "subject": subject, "text": text},
        timeout=30,
    )
    response.raise_for_status()


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


if REQUESTED_EMAIL:
    email = REQUESTED_EMAIL
else:
    rows = d1_query(
        "SELECT s.email FROM subscribers s "
        "LEFT JOIN upload_invites i ON i.email = s.email "
        "WHERE i.email IS NULL "
        "ORDER BY s.created_at ASC LIMIT 1"
    )
    if not rows:
        print("No waitlist signups without an invite yet -- nothing to do.")
        raise SystemExit(0)
    email = rows[0]["email"]

token = str(uuid.uuid4())
d1_query(
    "INSERT INTO upload_invites (token, label, created_at, email) VALUES (?, ?, datetime('now'), ?)",
    [token, email, email],
)

upload_url = f"https://personalknowhow.com/upload?token={token}"

body = f"""Hi,

Thanks for your interest in PersonalKnowHow. Here's your personal upload link:

{upload_url}

You'll need LinkedIn's own data export (a zip file) to upload -- step-by-step guide here,
including a heads-up about the two emails LinkedIn sends (wait for the second one, it's the
complete archive, not the fast partial one):

https://personalknowhow.com/linkedin-data-export

Once it's processed, you'll get your own MCP server you can connect to Claude to check your
real background against any job posting or question -- grounded in what you've actually done,
not just what a resume claims.

This link is single-use and tied to this email address.

Georgi
"""

send_email(email, "Your PersonalKnowHow upload link", body)

print(f"Invite sent to {email} (token {token})")
