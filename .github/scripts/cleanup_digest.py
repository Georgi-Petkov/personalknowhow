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
            "SELECT ui.label, ui.deleted_at, ui.mcp_url_public, ui.mcp_url_private, "
            "s.public_slug, s.private_slug "
            "FROM upload_invites ui "
            "LEFT JOIN subscribers s ON s.email = ui.email "
            "WHERE ui.deleted_at IS NOT NULL AND ui.worker_cleaned_up_at IS NULL "
            "AND (s.public_slug IS NOT NULL OR s.private_slug IS NOT NULL) "
            "ORDER BY ui.deleted_at ASC"
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
    slugs = [s for s in (row["public_slug"], row["private_slug"]) if s]
    teardown = "\n".join(f"    Teardown:  npx wrangler delete personalknowhow-{s}" for s in slugs)
    mark_done = (
        "    Mark done: npx wrangler d1 execute personalknowhow-waitlist --remote --command "
        f"\"UPDATE upload_invites SET worker_cleaned_up_at = datetime('now') WHERE email = "
        f"(SELECT email FROM subscribers WHERE public_slug = '{slugs[0] if slugs else ''}' "
        f"OR private_slug = '{slugs[0] if slugs else ''}')\""
    )
    lines.append(
        f"- {row['label']} (deleted {row['deleted_at']})\n"
        f"    Public URL:  {row['mcp_url_public'] or '(unknown)'}\n"
        f"    Private URL: {row['mcp_url_private'] or '(unknown)'}\n"
        f"{teardown}\n{mark_done}"
    )
body = (
    f"{len(rows)} deleted customer(s) still have a deployed Worker to tear down:\n\n"
    + "\n\n".join(lines)
)

response = requests.post(
    "https://api.resend.com/emails",
    headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
    json={
        "from": SEND_AS,
        "to": [ADMIN_EMAIL],
        "subject": f"PersonalKnowHow: {len(rows)} Worker(s) need manual cleanup",
        "text": body,
    },
    timeout=30,
)
response.raise_for_status()

print(f"Sent cleanup digest for {len(rows)} pending Worker(s) to {ADMIN_EMAIL}")
