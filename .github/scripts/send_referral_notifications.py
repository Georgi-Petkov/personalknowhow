import os

import requests

ACCOUNT_ID = "716dcf9e5806cc9dbb777e0b80bb236d"
DATABASE_ID = "934afff4-c462-41b0-91ff-2232473c5286"
SEND_AS = "PersonalKnowHow <office@personalknowhow.com>"

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]

BODIES = {
    "referrer_bonus": (
        "Good news -- someone you referred to PersonalKnowHow just completed their first "
        "payment. You've earned a free month (a waive-credit).\n\n"
        "We apply waive-credits by hand against an upcoming renewal -- no action needed from "
        "you, just wanted you to know it's earned and on the books."
    ),
    "referred_new_user": (
        "Thanks for signing up through a referral -- you've earned a free month (a "
        "waive-credit), which we'll apply to your *next* renewal (not this one, since your "
        "first payment is what earned it).\n\n"
        "We apply waive-credits by hand -- no action needed from you."
    ),
}


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
    "SELECT id, beneficiary_email, reason FROM referral_credits WHERE notified_at IS NULL ORDER BY id"
)

if not rows:
    print("No un-notified referral credits -- skipping.")
    raise SystemExit(0)

sent = 0
for row in rows:
    body = BODIES.get(row["reason"])
    if not body:
        print(f"Skipping row {row['id']}: unknown reason {row['reason']!r}")
        continue

    text = f"Hi,\n\n{body}\n\nGeorgi\n"

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": SEND_AS,
                "to": [row["beneficiary_email"]],
                "subject": "You've earned a PersonalKnowHow waive-credit",
                "text": text,
            },
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Failed to send to {row['beneficiary_email']} (row {row['id']}): {e}")
        continue

    d1_query(
        "UPDATE referral_credits SET notified_at = datetime('now') WHERE id = ?",
        [row["id"]],
    )
    sent += 1
    print(f"Notified {row['beneficiary_email']} ({row['reason']}, row {row['id']})")

print(f"Sent {sent} of {len(rows)} referral notification(s).")
