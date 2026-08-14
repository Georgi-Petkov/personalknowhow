# LinkedIn export connector — design notes (retired)

PKH's primary LinkedIn ingestion path is now the [Member Data Portability API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/overview)
(`ingest/linkedin_api_ingest.py`), available to accounts covered by the EU Digital Markets Act.
For accounts outside that scope (e.g. the US), the API path isn't available, and the fallback is
LinkedIn's manual "request an export" flow feeding `ingest/linkedin_ingest.py`.

An Electron desktop app was built to automate that manual flow end-to-end — request the export,
wait for the two sequential download-ready emails, download, merge, ingest, with a preview gate
before anything is saved. It's since been deleted from this repo: it was built the day before the
API path became available and made it unnecessary, and a few parts of it were never finished. The
code is gone, but the two real, verified findings behind it are worth keeping for anyone building
something similar for a non-EU account.

## Finding 1: don't automate the browser for LinkedIn login

The first version drove a Playwright-controlled Chromium window to open LinkedIn's export page.
This does not work for any account that signs in via "Sign in with Google" — Google blocks OAuth
sign-in through an automation-controlled browser outright, regardless of whether a real human is
at the keyboard. This was confirmed by hitting the block against a real account, not assumed from
documentation.

The fix: don't automate the browser at all. Open the relevant LinkedIn URL in the user's own,
already-signed-in default browser (`shell.openExternal` in Electron, or equivalent) and let them
click through normally, the same way they always would. The app's job is to know *when* and
*where* to send them, and to pick up the resulting download — not to drive the session itself.

## Finding 2: the real shape of LinkedIn's export emails

Verified against a real account, not assumed: there is no "we received your request"
confirmation email. LinkedIn sends a fast "first installment" email almost immediately, then a
second email roughly 24 hours later that explicitly identifies itself as the "second part" of the
same archive. Some data only appears in the second part — Certifications, Skills, and Learning
history were confirmed to land there and not in the first installment. Any parser watching for a
single export-ready email, or watching for a confirmation email that doesn't exist, will miss
data. `ingest/linkedin_export_email.py`'s Gmail search logic is built around this real two-part
shape.

## What was actually proven

- The full app state machine — export request → first-part received → second-part received →
  download → mandatory preview ("found N certifications, M new since last sync") → save — was
  verified end-to-end with zero human interaction, via a scripted mock mode with canned responses
  and short timers standing in for the real external calls.
- The Gmail-parsing logic itself was calibrated against real email content pulled from a live
  Gmail account (subject lines, link shapes, the two-part timing), independent of the mock-mode
  testing above.

## What was never finished

The app's own Gmail OAuth was never authorized against a real account — no `gmail_client_secret.json`
was ever placed, so the live sync path (as opposed to the mocked one) never actually ran end to
end for real. Whether LinkedIn's export-request page behaves identically when opened cold via
`shell.openExternal` versus a normal typed URL was also never confirmed live.

## If you're building this for real

The shape that worked: a small state machine (request → part 1 → part 2 → download → preview →
save) driven by Gmail polling, with the browser interaction limited to "open this URL in the
user's real browser" and nothing more automated than that. Budget real time to finish the live
OAuth authorization step and confirm the cold-open behavior — those were the two gaps between
"proven in mock mode" and "actually usable."
