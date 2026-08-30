#!/usr/bin/env python3
"""
Browse and fetch new AS3 postings from DataJobs' Gold tables, ready to hand
to a Claude Code conversation for evaluation -- no separate agent loop, no
ANTHROPIC_API_KEY. Claude Code already has a native MCP connection to
personalknowhow-private; this script's only job is getting posting text in
front of it (or you), one at a time, to avoid dumping every posting's full
text (and burning tokens) at once.

Two modes:
- No --id: compact list (id, posted date, employer, title) of postings not
  yet reviewed. Listing costs nothing and marks nothing seen.
- --id EXTERNAL_JOB_ID: full text for that one posting, marked reviewed
  (won't show in the list again) unless --peek is also given.

Watermark-tracked in data/reviewed_as3_postings.json. First list ever shows
everything currently in Gold, since nothing's been reviewed yet.

Separate concern from linkedin/JOB_SEARCH_TRACKER.md (application outcomes,
not posting fit).

Usage:
    set -a; source .env; set +a
    python3 ingest/get_new_postings.py                    # list
    python3 ingest/get_new_postings.py --id 2544583548     # fetch one, mark reviewed
    python3 ingest/get_new_postings.py --id 2544583548 --peek  # fetch one, don't mark
"""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / "data" / "reviewed_as3_postings.json"

GOLD_QUERY = """
SELECT external_job_id, title, employer, location, job_category,
       origin_site, posted_date, url, description, language_guess
FROM workspace.datajobs_gold.fct_postings_for_evaluation
ORDER BY posted_date DESC
"""


def load_seen_ids() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    return set(json.loads(STATE_FILE.read_text()).get("seen_ids", []))


def save_seen_ids(ids: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"seen_ids": sorted(ids)}, indent=2))


def fetch_postings():
    url = URL.create(
        "databricks",
        username="token",
        password=os.environ["DATABRICKS_TOKEN"],
        host=os.environ["DATABRICKS_HOST"],
        query={"http_path": os.environ["DATABRICKS_HTTP_PATH"]},
    )
    engine = create_engine(url)
    with engine.connect() as conn:
        return conn.execute(text(GOLD_QUERY)).fetchall()


def print_list(postings, seen_ids: set[str], limit: int | None, first_run: bool) -> None:
    unseen = [p for p in postings if p.external_job_id not in seen_ids]
    print(f"# {len(postings)} total postings in Gold, {len(unseen)} not yet reviewed"
          f"{' (first run -- everything counts as new)' if first_run else ''}.\n")
    if limit is not None:
        unseen = unseen[:limit]
    if not unseen:
        print("Nothing new.")
        return
    print(f"{'id':<14} {'posted':<12} {'lang':<8} {'employer':<35} title")
    print("-" * 108)
    for row in unseen:
        employer = (row.employer or "")[:34]
        flag = "DA" if row.language_guess == "danish" else "en"
        print(f"{row.external_job_id:<14} {str(row.posted_date):<12} {flag:<8} {employer:<35} {row.title}")
    print(f"\n# Fetch one: python3 ingest/get_new_postings.py --id <id>")


def print_single(row) -> None:
    print("=" * 70)
    if row.language_guess == "danish":
        print("*** POSTING APPEARS TO BE IN DANISH -- factor this into fit, don't ignore it ***")
    print(f"Title: {row.title}")
    print(f"Employer: {row.employer}")
    print(f"Location: {row.location}")
    print(f"Category (DataJobs classification, verify against the text): {row.job_category}")
    print(f"Language (heuristic): {row.language_guess}")
    print(f"Posted: {row.posted_date}  |  Source: {row.origin_site} (via AS3)")
    print(f"URL: {row.url}")
    print(f"external_job_id: {row.external_job_id}")
    print()
    print(row.description)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="Fetch one posting's full text by external_job_id")
    parser.add_argument("--peek", action="store_true", help="With --id: don't mark it reviewed")
    parser.add_argument("--limit", type=int, default=None, help="List mode: show at most N")
    args = parser.parse_args()

    for var in ("DATABRICKS_HOST", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN"):
        if not os.environ.get(var):
            raise SystemExit(f"{var} is not set -- source your .env first.")

    seen_ids = load_seen_ids()
    first_run = not STATE_FILE.exists()
    postings = fetch_postings()

    if args.id is None:
        print_list(postings, seen_ids, args.limit, first_run)
        return

    matches = [p for p in postings if p.external_job_id == args.id]
    if not matches:
        raise SystemExit(f"No posting with external_job_id={args.id!r} found in fct_postings_for_evaluation.")

    print_single(matches[0])

    if not args.peek:
        seen_ids.add(args.id)
        save_seen_ids(seen_ids)
        print(f"\n# Marked {args.id} as reviewed -- won't show in the list again.")
    else:
        print(f"\n# --peek: {args.id} not marked reviewed, still shows in the list.")


if __name__ == "__main__":
    main()
