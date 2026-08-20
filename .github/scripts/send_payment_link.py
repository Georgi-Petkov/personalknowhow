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

# Once a real Stripe Payment Link exists, set STRIPE_PAYMENT_LINK (repo secret) to its URL --
# client_reference_id is how /api/stripe-webhook's checkout.session.completed handler
# correlates the payment back to this exact invite row, so it must be appended here, not
# configured inside the Payment Link itself. Until then, falls back to the mock click-through
# confirmation at /mock-pay.
stripe_payment_link = os.environ.get("STRIPE_PAYMENT_LINK", "").strip()
test_mode = not stripe_payment_link
pay_url = (
    f"{stripe_payment_link}?client_reference_id={TOKEN}"
    if stripe_payment_link
    else f"https://personalknowhow.com/mock-pay?token={TOKEN}"
)

test_mode_note = (
    "\n(Test mode: Stripe isn't wired up yet, so this link simulates a completed payment "
    "instead of charging anything.)\n"
    if test_mode
    else ""
)

body = f"""Hi,

Your PersonalKnowHow upload was received. One step left -- complete payment to start processing:

{pay_url}
{test_mode_note}
Once confirmed, processing starts right away -- within 24 hours you'll get another email with
your personal MCP connection link and step-by-step instructions to add it to Claude (Claude.ai
web or Claude Desktop).

Georgi
"""

send_email(email, "Complete payment to process your PersonalKnowHow upload", body)

print(f"Payment link sent to {email} (token {TOKEN})")
