import json
import os

import requests

ACCOUNT_ID = "716dcf9e5806cc9dbb777e0b80bb236d"
DATABASE_ID = "934afff4-c462-41b0-91ff-2232473c5286"
SEND_AS = "PersonalKnowHow <office@personalknowhow.com>"

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
TOKEN = os.environ["INVITE_TOKEN"].strip()


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


rows = d1_query(
    "SELECT email, mcp_url_public, mcp_url_private, mcp_token_private, deleted_at "
    "FROM upload_invites WHERE token = ?",
    [TOKEN],
)
if not rows:
    raise SystemExit(f"No invite found for token {TOKEN}")
row = rows[0]
if row["deleted_at"]:
    raise SystemExit("This invite has already been deleted -- not sending the MCP link.")
if not row["mcp_url_public"] and not row["mcp_url_private"]:
    raise SystemExit("This invite has no MCP URLs set yet -- deploy the customer's Workers first.")
email = row["email"]
if not email:
    raise SystemExit("This invite has no email on file -- can't send the MCP link.")

mcp_url_public = row["mcp_url_public"]
mcp_url_private = row["mcp_url_private"]
mcp_token_private = row.get("mcp_token_private")


def mcp_config(url: str, token: str | None) -> str:
    server: dict = {"command": "npx", "args": ["mcp-remote", url]}
    if token:
        server["args"] += ["--header", "Authorization:${AUTH_HEADER}"]
        server["env"] = {"AUTH_HEADER": f"Bearer {token}"}
    return json.dumps({"mcpServers": {"personalknowhow": server}}, indent=2)


private_block = f"""PRIVATE -- your full data (requires the login token below):

In Claude Desktop, add this to your claude_desktop_config.json, then restart:

{mcp_config(mcp_url_private, mcp_token_private)}

(Claude.ai's web custom-connector form has no field for a login token, so this one can't be
added there -- use Claude Desktop.)
""" if mcp_url_private else ""

public_block = f"""PUBLIC -- shareable know-how card, no login needed (excludes job-search
activity, share only with who you intend to use it):

In Claude.ai (web): Settings -> Connectors -> Add custom connector, then paste this URL:
{mcp_url_public}

In Claude Desktop:
{mcp_config(mcp_url_public, None)}
""" if mcp_url_public else ""

body = f"""Hi,

Your PersonalKnowHow MCP server(s) are ready.

{private_block}
{public_block}
Once connected, ask it anything about your real, uploaded background -- job postings, skills,
"do I have experience with X."

Georgi
"""

send_email(email, "Your PersonalKnowHow MCP server is ready", body)

print(f"MCP-ready email sent to {email} (token {TOKEN})")
