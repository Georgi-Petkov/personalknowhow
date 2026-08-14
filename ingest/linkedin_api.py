#!/usr/bin/env python3
"""
LinkedIn Member Data Portability API — token loading + generic paginated
domain fetch. This is the thin transport layer; ingest/linkedin_api_ingest.py
is where domain-specific field mapping happens.

Token comes from connector/linkedin_token.json (OAuth already completed via
LinkedIn's Developer Portal / OAuth Token Generator Tool for this single-user
app — see ~/.claude/plans/look-at-this-lazy-wombat.md for how it was
obtained). This module never runs an OAuth flow itself, only reads the
cached token.

Pagination note (verified live 2026-08-10, don't re-derive this from the
docs alone): `paging.total` is NOT a reliable "how many pages" count for
every domain. For most domains it's 1 and there's no `next` link — done in
one call. But RECOMMENDATIONS and ENDORSEMENTS silently paginate a *second*,
structurally different page onto the same domain (recommendations/
endorsements the member GAVE to others, not received) — total=2 there, with
a real `next` link. So fetch_domain() always walks start=0,1,2,... and stops
on the domain's own 404 "No data found" response, never on `total`.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
TOKEN_FILE = ROOT / "connector" / "linkedin_token.json"

API_BASE = "https://api.linkedin.com/rest/memberSnapshotData"
LINKEDIN_VERSION = "202312"
COUNT_PER_PAGE = 10  # LinkedIn's own page size for this endpoint; not user-tunable in practice
MAX_PAGES = 20        # generous ceiling — no observed domain has gone past 2
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2


class LinkedInAPIError(RuntimeError):
    pass


def load_token() -> str:
    if not TOKEN_FILE.exists():
        raise LinkedInAPIError(
            f"No cached token at {TOKEN_FILE}. Run the OAuth flow first "
            f"(see ~/.claude/plans/look-at-this-lazy-wombat.md)."
        )
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    token = data.get("access_token", "")
    if not token:
        raise LinkedInAPIError(f"{TOKEN_FILE} has no access_token field.")
    return token


def _fetch_page(domain: str, start: int, token: str) -> dict | None:
    """One HTTP call. Returns the parsed body, or None on the documented
    'no data found' 404 (a normal, expected end-of-data signal, not an error)."""
    url = f"{API_BASE}?q=criteria&domain={domain}&start={start}&count={COUNT_PER_PAGE}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Linkedin-Version": LINKEDIN_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        },
    )
    last_err: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 404:
                return None  # "No data found for this domain and memberId" — expected, not an error
            if e.code == 401:
                raise LinkedInAPIError(
                    f"{domain}: 401 Unauthorized — token expired or invalid. "
                    f"Re-run the OAuth flow. Body: {body}"
                ) from e
            last_err = LinkedInAPIError(f"{domain} start={start}: HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            last_err = LinkedInAPIError(f"{domain} start={start}: {e}")
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
    raise last_err  # type: ignore[misc]


def fetch_domain(domain: str, token: str | None = None) -> list[dict]:
    """Fetch every row of a domain's snapshotData, across however many pages
    actually exist (see module docstring — don't trust paging.total)."""
    token = token or load_token()
    rows: list[dict] = []
    for start in range(MAX_PAGES):
        body = _fetch_page(domain, start, token)
        if body is None:
            break
        page_rows: list[dict] = []
        for element in body.get("elements", []):
            page_rows.extend(element.get("snapshotData", []))
        if not page_rows:
            break
        rows.extend(page_rows)
    return rows


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python ingest/linkedin_api.py DOMAIN_NAME")
        sys.exit(1)
    domain = sys.argv[1]
    result = fetch_domain(domain)
    print(f"{domain}: {len(result)} rows")
    print(json.dumps(result[:3], indent=2))
