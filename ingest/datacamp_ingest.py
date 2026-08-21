#!/usr/bin/env python3
"""
Reconcile data/datacamp_completed.json (DataCamp completed-courses scrape) into
corpus/datacamp/ markdown files.

Courses with status Completed or Skipped are "known" content — the graph should
treat both identically (a skipped course means the user tested out of it, not
that they don't know the material). Any other status (currently just "Continue",
DataCamp's legacy-catalog-migration artifact) is treated as genuinely unfinished:
its corpus file (if any) is removed, and it's recorded separately in
data/datacamp_legacy_pending.json for manual follow-up — never fed to build_graph.py.

Usage:
  python ingest/datacamp_ingest.py
  python ingest/datacamp_ingest.py --input data/datacamp_completed.json
"""
import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "pyyaml", "-q",
                   "--break-system-packages"], check=True)
    import yaml

sys.path.insert(0, str(Path(__file__).parent))
from merge import parse_file, merge_frontmatters, write_file  # noqa: E402
from common import infer_tags, _slug  # noqa: E402

ROOT          = Path(__file__).parent.parent
INPUT_FILE    = ROOT / "data" / "datacamp_completed.json"
ASSESSMENTS_FILE = ROOT / "data" / "datacamp_assessments.json"
TRACKS_FILE   = ROOT / "data" / "datacamp_tracks.json"
CORPUS_OUT    = ROOT / "corpus" / "datacamp"
LEGACY_OUT    = ROOT / "data" / "datacamp_legacy_pending.json"

KNOWN_STATUSES = {"Completed", "Skipped"}

CATALOG_ARCHIVED = "2026-07-07"
COMPLETE_BY       = "2026-09-08"


def main(input_file: Path) -> None:
    if not input_file.exists():
        print(f"ERROR: {input_file} not found")
        sys.exit(1)

    courses = json.loads(input_file.read_text(encoding="utf-8"))
    print(f"Loaded {len(courses)} entries from {input_file.name}")

    CORPUS_OUT.mkdir(parents=True, exist_ok=True)

    written = 0
    updated = 0
    removed = 0
    legacy: list[dict] = []

    for c in courses:
        title = (c.get("title") or "").strip()
        if not title:
            continue
        slug = _slug(title)
        path = CORPUS_OUT / f"{slug}.md"

        if c.get("status") not in KNOWN_STATUSES:
            if path.exists():
                path.unlink()
                removed += 1
            legacy.append({
                "title":            title,
                "description":      c.get("description"),
                "course_url":       c.get("course_url"),
                "catalog_archived": CATALOG_ARCHIVED,
                "complete_by":      COMPLETE_BY,
            })
            continue

        desc = c.get("description") or ""
        short = (desc[:200] + "…") if len(desc) > 200 else desc
        tags = infer_tags(title + " " + desc)

        new_fm = {
            "title":              title,
            "type":               "course",
            "provider":           "DataCamp",
            "date":               c.get("completion_date"),
            "description":        short,
            "domain_tags":        tags,
            "course_url":         c.get("course_url"),
            "accomplishment_url": c.get("accomplishment_url"),
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

    LEGACY_OUT.write_text(
        json.dumps(legacy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(f"New files: {written}, updated: {updated}, removed (legacy): {removed}")
    print(f"Legacy/pending: {len(legacy)} entries -> {LEGACY_OUT}")

    ingest_assessments()
    ingest_tracks()


def ingest_assessments(input_file: Path = ASSESSMENTS_FILE) -> None:
    """DataCamp Skill Assessments -- a separate DataCamp feature from Courses
    Completed, with no scrape/export of its own (confirmed 2026-08-10: real
    evidence a keyword-tag system had scored as "0 known coverage" -- the
    user passed this assessment with zero courses from the matching learning
    path completed, proving graded/real knowledge that neither the old
    keyword tags nor course-completion data could see). Same idiom as
    position_notes.json/edx_completed.json: no automated source exists, so
    entries are hand-entered here from the DataCamp assessment results page.
    """
    if not input_file.exists():
        print(f"No {input_file.name} found -- skipping skill assessments (none recorded yet).")
        return

    assessments = json.loads(input_file.read_text(encoding="utf-8"))
    written = updated = 0
    for a in assessments:
        skill_name = (a.get("skill_name") or "").strip()
        if not skill_name:
            continue
        score = a.get("score")
        level = a.get("level") or ""
        date = a.get("verified_date")
        skill_area = a.get("primary_skill_area") or ""

        slug = _slug(f"{skill_name}_assessment")
        path = CORPUS_OUT / f"{slug}.md"

        desc = f"DataCamp Skill Assessment: {level} ({score}/100), verified {date}."
        tags = infer_tags(f"{skill_name} {skill_area}")

        new_fm = {
            "title":       f"{skill_name} (Skill Assessment)",
            "type":        "skill_assessment",
            "provider":    "DataCamp",
            "date":        date,
            "description": desc,
            "domain_tags": tags,
            "score":       score,
            "level":       level,
        }

        if path.exists():
            old_fm, old_body = parse_file(path)
            merged_fm = merge_frontmatters([old_fm, new_fm])
            write_file(path, merged_fm, old_body if old_body.strip() else f"\n{desc}\n")
            updated += 1
        else:
            write_file(path, new_fm, f"\n{desc}\n")
            written += 1

        print(f"  {skill_name}: {level} ({score}/100), tags={tags}")

    print(f"Skill assessments -- new: {written}, updated: {updated}")


def ingest_tracks(input_file: Path = TRACKS_FILE) -> None:
    """DataCamp Tracks (Skill Tracks / Career Tracks) -- bundles of courses,
    distinct from individual course completions in data/datacamp_completed.json.
    No formal export/API exists (same idiom as skill assessments), so entries
    are hand-entered or scraped separately into data/datacamp_tracks.json.

    Unlike a course, a track node is written even when incomplete
    (status: in_progress) -- its whole point is to carry "what's left to
    finish this track" (the `courses` list, filtered to non-completed
    entries) somewhere queryable, since an unfinished course has no corpus
    entry of its own to be found by otherwise.
    """
    if not input_file.exists():
        print(f"No {input_file.name} found -- skipping tracks (none recorded yet).")
        return

    tracks = json.loads(input_file.read_text(encoding="utf-8"))
    written = updated = 0
    for t in tracks:
        track_name = (t.get("track_name") or "").strip()
        if not track_name:
            continue
        status = t.get("status") or "in_progress"
        track_type = t.get("track_type") or ""
        date = t.get("completed_date") or t.get("started_date")
        courses = t.get("courses") or []
        courses_verified = bool(t.get("courses_verified"))
        remaining = [c.get("title") for c in courses if c.get("status") != "completed"]

        slug = _slug(f"{track_name}_track")
        path = CORPUS_OUT / f"{slug}.md"

        if status == "completed":
            desc = f"DataCamp {track_type} track, completed {date}."
        else:
            desc = f"DataCamp {track_type} track, in progress (started {date})."
        tags = infer_tags(track_name)

        body_lines = [f"\n{desc}\n"]
        if not courses_verified:
            body_lines.append(
                "\nPer-course membership not yet confirmed via a live DataCamp "
                "scrape -- see data/datacamp_tracks.json's `note` field for this "
                "entry. Do not read an empty remaining-courses list below as "
                "\"nothing left\"; it means \"not yet checked\".\n"
            )
        elif status != "completed":
            if remaining:
                body_lines.append("\nCourses remaining to complete this track:\n")
                body_lines.extend(f"- {title}\n" for title in remaining)
            else:
                body_lines.append("\nAll known courses in this track are complete.\n")

        new_fm = {
            "title":              track_name,
            "type":               "track",
            "provider":           "DataCamp",
            "date":               date,
            "description":        desc,
            "domain_tags":        tags,
            "track_type":         track_type,
            "status":             status,
            "accomplishment_url": t.get("accomplishment_url"),
            "remaining_courses":  remaining if courses_verified else None,
            "courses_verified":   courses_verified,
        }

        body = "".join(body_lines)
        if path.exists():
            old_fm, old_body = parse_file(path)
            merged_fm = merge_frontmatters([old_fm, new_fm])
            write_file(path, merged_fm, body)
            updated += 1
        else:
            write_file(path, new_fm, body)
            written += 1

        print(f"  {track_name}: {status}, courses_verified={courses_verified}, remaining={len(remaining)}")

    print(f"Tracks -- new: {written}, updated: {updated}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=str(INPUT_FILE))
    args = p.parse_args()
    main(Path(args.input))
