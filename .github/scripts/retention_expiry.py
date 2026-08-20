"""
Enforce the fixed 90-day retention policy: any upload_invites row whose last
upload (used_at) is more than 90 days old, and that hasn't already been
deleted, is auto-deleted -- same effect as a customer-initiated
POST /api/delete-data (site/src/index.ts), just triggered by the retention
clock instead of a customer action. deleted_by_email is set to
"system:retention-expiry" so the existing deleted_by_email column can
distinguish this from a real customer request without a new column.

This is a parallel implementation of site/src/index.ts's delete-data logic,
not a shared one -- that logic runs inside a Cloudflare Worker (TypeScript,
env.STORAGE R2 binding), this runs in a GitHub Actions Python job (plain
REST calls) using the same CLOUDFLARE_D1_TOKEN / CLOUDFLARE_R2_AI_TOKEN
secrets process_upload.yml and cleanup_digest.py already use -- no new
secret needed.
"""
import os

import requests

ACCOUNT_ID = "716dcf9e5806cc9dbb777e0b80bb236d"
DATABASE_ID = "934afff4-c462-41b0-91ff-2232473c5286"
RETENTION_DAYS = 90

SEND_AS = "PersonalKnowHow <office@personalknowhow.com>"
ADMIN_EMAIL = "2georgipetkov@gmail.com"

CF_D1_TOKEN = os.environ["CF_D1_TOKEN"]
CF_R2_TOKEN = os.environ["CF_R2_TOKEN"]
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

D1_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{DATABASE_ID}/query"
R2_BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/r2/buckets/personalknowhow-storage/objects"


def d1_query(sql: str, params: list | None = None) -> list[dict]:
    response = requests.post(
        D1_URL,
        headers={"Authorization": f"Bearer {CF_D1_TOKEN}"},
        json={"sql": sql, "params": params or []},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 query failed: {data}")
    return data["result"][0]["results"]


def r2_delete(key: str) -> None:
    resp = requests.delete(f"{R2_BASE}/{key}", headers={"Authorization": f"Bearer {CF_R2_TOKEN}"}, timeout=30)
    # A 404 just means the object was already gone (e.g. raw upload already
    # cleared by process_upload.yml) -- not a failure worth stopping the run for.
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()


rows = d1_query(
    "SELECT ui.token, ui.label, ui.email, ui.r2_upload_key, ui.used_at, "
    "s.public_slug, s.private_slug "
    "FROM upload_invites ui "
    "LEFT JOIN subscribers s ON s.email = ui.email "
    "WHERE ui.used_at IS NOT NULL AND ui.deleted_at IS NULL "
    f"AND ui.used_at < datetime('now', '-{RETENTION_DAYS} days')"
)

if not rows:
    print(f"No invites past the {RETENTION_DAYS}-day retention window -- nothing to expire.")
    raise SystemExit(0)

expired = []
for row in rows:
    token = row["token"]

    # Tombstone first -- this is what actually cuts off access (every deployed
    # customer Worker checks upload_invites.deleted_at before serving anything),
    # so it must land before the R2 cleanup below, matching site/src/index.ts's
    # own ordering and its stated reasoning.
    d1_query(
        "UPDATE upload_invites SET deleted_at = datetime('now'), deleted_by_email = ?, "
        "mcp_token_private = NULL, r2_upload_key = NULL WHERE token = ?",
        ["system:retention-expiry", token],
    )

    keys = [row["r2_upload_key"]]
    for slug in (row["public_slug"], row["private_slug"]):
        if slug:
            keys += [f"customers/{slug}/entries.json", f"customers/{slug}/embeddings.json"]
    for key in keys:
        if key:
            r2_delete(key)

    expired.append(row)
    print(f"Expired (90-day retention): {row['label']} (last upload {row['used_at']})")

print(f"Auto-deleted {len(expired)} invite(s) past the {RETENTION_DAYS}-day retention window.")

if RESEND_API_KEY:
    lines = [f"- {r['label']} (last upload {r['used_at']})" for r in expired]
    body = (
        f"{len(expired)} customer(s) auto-deleted under the 90-day fixed retention policy "
        f"(no upload since more than {RETENTION_DAYS} days ago):\n\n" + "\n".join(lines) +
        "\n\nAccess is already cut off. Their deployed Worker(s) still need manual teardown -- "
        "same as any other deletion, see the weekly Cleanup Digest email."
    )
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": SEND_AS,
            "to": [ADMIN_EMAIL],
            "subject": f"PersonalKnowHow: {len(expired)} customer(s) auto-deleted (90-day retention)",
            "text": body,
        },
        timeout=30,
    )
    response.raise_for_status()
    print(f"Sent retention-expiry notice for {len(expired)} customer(s) to {ADMIN_EMAIL}")
