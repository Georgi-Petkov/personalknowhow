#!/usr/bin/env python3
"""
Gather and structure data for a course-gap analysis — no scoring/ranking judgment
baked in here. Reads DataCamp's full course catalog (data/datacamp_catalog.json),
excludes courses already completed/skipped, tags every remaining candidate with
the same domain_tags vocabulary used across the corpus, and cross-references
against how much existing coverage the user already has per tag (from graph.json).

The actual judgment call — which gaps matter for income, gap-filling vs. pivot —
is made by pasting this output into a Claude Code chat along with a stated goal,
the same pattern already used by score_video.py. This script only prepares the
grounding data.

Usage:
  python ingest/recommend_courses.py
  python ingest/recommend_courses.py --input data/datacamp_catalog.json
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "pyyaml", "-q",
                   "--break-system-packages"], check=True)
    import yaml

sys.path.insert(0, str(Path(__file__).parent))
from common import infer_tags, known_tag_counts  # noqa: E402

ROOT       = Path(__file__).parent.parent
INPUT_FILE = ROOT / "data" / "datacamp_catalog.json"
PROFILE_FILE = ROOT / "corpus" / "linkedin" / "profile.md"

KNOWN_STATUSES = {"Completed", "Skipped"}


def main(input_file: Path) -> None:
    if not input_file.exists():
        print(f"ERROR: {input_file} not found")
        sys.exit(1)

    catalog = json.loads(input_file.read_text(encoding="utf-8"))
    known_counts = known_tag_counts()
    profile_text = PROFILE_FILE.read_text(encoding="utf-8") if PROFILE_FILE.exists() else ""

    not_started: list[dict] = []
    in_progress: list[dict] = []

    for c in catalog:
        if c.get("status") in KNOWN_STATUSES:
            continue
        title = (c.get("title") or "").strip()
        if not title:
            continue
        desc = c.get("description") or ""
        tags = infer_tags(title + " " + desc)
        entry = {
            "title": title,
            "description": desc,
            "tags": tags,
            "course_url": c.get("course_url"),
            "progress_pct": c.get("progress_pct"),
        }
        (in_progress if c.get("status") == "In Progress" else not_started).append(entry)

    # Group not-started candidates by tag, tags sorted so sparsest known-coverage
    # (biggest gaps) lead the report.
    by_tag: dict[str, list[dict]] = defaultdict(list)
    untagged: list[dict] = []
    for entry in not_started:
        if not entry["tags"]:
            untagged.append(entry)
        for tag in entry["tags"]:
            by_tag[tag].append(entry)

    tags_sorted = sorted(by_tag.keys(), key=lambda t: (known_counts.get(t, 0), t))

    print(f"CATALOG: {len(catalog)} total courses")
    print(f"Already known (Completed/Skipped): {len(catalog) - len(not_started) - len(in_progress)}")
    print(f"In progress (resume candidates): {len(in_progress)}")
    print(f"Not started (new candidates): {len(not_started)}")
    print()

    print("=== IN PROGRESS — already started, consider finishing before new ones ===")
    for e in sorted(in_progress, key=lambda x: -(x["progress_pct"] or 0)):
        print(f"- [{e['progress_pct']}%] {e['title']} — {e['description']}")
        print(f"  tags: {', '.join(e['tags']) or '(none)'} | {e['course_url']}")
    print()

    print("=== NOT STARTED, GROUPED BY TAG (sparsest existing coverage first) ===")
    for tag in tags_sorted:
        entries = by_tag[tag]
        print(f"\n--- {tag} (known coverage: {known_counts.get(tag, 0)} completed courses, "
              f"{len(entries)} candidates) ---")
        for e in sorted(entries, key=lambda x: x["title"]):
            print(f"- {e['title']} — {e['description']}")
            print(f"  {e['course_url']}")

    if untagged:
        print(f"\n=== UNTAGGED CANDIDATES ({len(untagged)}) — no domain_tags match, "
              "may need a human look ===")
        for e in untagged:
            print(f"- {e['title']} — {e['description']}")
            print(f"  {e['course_url']}")

    print("\n=== YOUR CURRENT PROFILE (corpus/linkedin/profile.md) ===")
    print(profile_text[:3000])

    print("\n=== SCORING INSTRUCTIONS ===")
    print(
        "Paste this entire output into a Claude Code chat along with your current goal "
        "(e.g. 'gaps toward Data Engineer', 'pivot toward AI Engineering'). Ask it to:\n"
        "1. Identify which tags represent real gaps given the stated goal — not just low "
        "known-coverage, but actually relevant to income/the stated direction.\n"
        "2. Recommend a short, specific shortlist (not 'take everything') from the "
        "NOT STARTED section, prioritizing IN PROGRESS courses first if any are relevant.\n"
        "3. Flag anything in UNTAGGED CANDIDATES that looks relevant despite missing tags."
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=str(INPUT_FILE))
    args = p.parse_args()
    main(Path(args.input))
