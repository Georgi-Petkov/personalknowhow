#!/usr/bin/env python3
"""
Reconcile data/edx_completed.json (edX/MITx learner records, scraped from
records.edx.org — no public export API, so this is populated by hand or via
an authenticated Playwright session) into corpus/edx/ markdown files.

Only "Earned" courses go in data/edx_completed.json in the first place —
unlike datacamp_ingest.py there's no separate legacy/pending bucket here,
since "Not Earned" rows on records.edx.org aren't attempted work, just
program requirements the learner hasn't taken yet.

Usage:
  python ingest/edx_ingest.py
  python ingest/edx_ingest.py --input data/edx_completed.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from merge import parse_file, merge_frontmatters, write_file  # noqa: E402
from common import infer_tags, _slug  # noqa: E402

ROOT       = Path(__file__).parent.parent
INPUT_FILE = ROOT / "data" / "edx_completed.json"
CORPUS_OUT = ROOT / "corpus" / "edx"


def main(input_file: Path) -> None:
    if not input_file.exists():
        print(f"ERROR: {input_file} not found")
        sys.exit(1)

    courses = json.loads(input_file.read_text(encoding="utf-8"))
    print(f"Loaded {len(courses)} entries from {input_file.name}")

    CORPUS_OUT.mkdir(parents=True, exist_ok=True)

    written = 0
    updated = 0

    for c in courses:
        title = (c.get("title") or "").strip()
        if not title:
            continue
        slug = _slug(title)
        path = CORPUS_OUT / f"{slug}.md"

        school = c.get("school") or ""
        program = c.get("program") or ""
        desc = f"{program} — {school}".strip(" —")
        tags = infer_tags(title + " " + program)

        new_fm = {
            "title":        title,
            "type":         "course",
            "provider":     "edX",
            "date":         c.get("date_earned"),
            "description":  desc,
            "domain_tags":  tags,
            "school":       school,
            "course_id":    c.get("course_id"),
            "grade":        c.get("grade"),
            "letter_grade": c.get("letter_grade"),
        }

        if path.exists():
            old_fm, old_body = parse_file(path)
            merged_fm = merge_frontmatters([old_fm, new_fm])
            body = old_body if old_body.strip() else f"\n{desc}\n"
            write_file(path, merged_fm, body)
            updated += 1
        else:
            write_file(path, new_fm, f"\n{desc}\n")
            written += 1

    print(f"New files: {written}, updated: {updated}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=str(INPUT_FILE))
    args = p.parse_args()
    main(Path(args.input))
