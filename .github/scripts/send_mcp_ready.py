import json
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
    "SELECT email, mcp_url, mcp_token, deleted_at FROM upload_invites WHERE token = ?", [TOKEN]
)
if not rows:
    raise SystemExit(f"No invite found for token {TOKEN}")
row = rows[0]
if row["deleted_at"]:
    raise SystemExit("This invite has already been deleted -- not sending the MCP link.")
if not row["mcp_url"]:
    raise SystemExit("This invite has no mcp_url set yet -- deploy the customer's Worker first.")
email = row["email"]
if not email:
    raise SystemExit("This invite has no email on file -- can't send the MCP link.")

mcp_url = row["mcp_url"]
mcp_token = row.get("mcp_token")

if mcp_token:
    config = {
        "mcpServers": {
            "personalknowhow": {
                "command": "npx",
                "args": ["mcp-remote", mcp_url, "--header", "Authorization:${AUTH_HEADER}"],
                "env": {"AUTH_HEADER": f"Bearer {mcp_token}"},
            }
        }
    }
    connect_block = f"""In Claude Desktop, add this to your claude_desktop_config.json, then restart:

{json.dumps(config, indent=2)}
"""
else:
    connect_block = f"""In Claude.ai (web): Settings -> Connectors -> Add custom connector, then paste this URL:

{mcp_url}
"""

body = f"""Hi,

Your PersonalKnowHow MCP server is ready.

{connect_block}
Once connected, ask it anything about your real, uploaded background -- job postings, skills,
"do I have experience with X."

Georgi
"""

SEND_AS = "PersonalKnowHow <office@personalknowhow.com>"

msg = MIMEMultipart("alternative")
msg["Subject"] = "Your PersonalKnowHow MCP server is ready"
msg["From"] = SEND_AS
msg["To"] = email
msg.attach(MIMEText(body, "plain", "utf-8"))

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
    smtp.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
    smtp.sendmail("office@personalknowhow.com", [email], msg.as_string())

print(f"MCP-ready email sent to {email} (token {TOKEN})")
