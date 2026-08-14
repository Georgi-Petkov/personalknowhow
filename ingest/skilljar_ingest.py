#!/usr/bin/env python3
"""
Reconcile data/skilljar_completed.json (Anthropic Skilljar registrations scrape)
into corpus/skilljar/ markdown files.

Only registrations with status Completed (a certificate was issued) count as
known content. In-progress registrations are excluded from the graph and
recorded separately in data/skilljar_in_progress.json so they aren't lost,
but they're never fed to build_graph.py.

Usage:
  python ingest/skilljar_ingest.py
  python ingest/skilljar_ingest.py --input data/skilljar_completed.json
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

ROOT             = Path(__file__).parent.parent
INPUT_FILE       = ROOT / "data" / "skilljar_completed.json"
CORPUS_OUT       = ROOT / "corpus" / "skilljar"
IN_PROGRESS_OUT  = ROOT / "data" / "skilljar_in_progress.json"


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
    in_progress: list[dict] = []

    for c in courses:
        title = (c.get("title") or "").strip()
        if not title:
            continue
        slug = _slug(title)
        path = CORPUS_OUT / f"{slug}.md"

        if c.get("status") != "Completed":
            if path.exists():
                path.unlink()
                removed += 1
            in_progress.append({
                "title":       title,
                "description": c.get("description"),
                "course_url":  c.get("course_url"),
                "progress":    c.get("progress"),
            })
            continue

        desc = c.get("description") or ""
        short = (desc[:200] + "…") if len(desc) > 200 else desc
        tags = infer_tags(title + " " + desc)

        new_fm = {
            "title":           title,
            "type":            "course",
            "provider":        "Anthropic Skilljar",
            "date":            c.get("completed_date"),
            "description":     short,
            "domain_tags":     tags,
            "course_url":      c.get("course_url"),
            "certificate_url": c.get("certificate_url"),
            "score":           c.get("score"),
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

    IN_PROGRESS_OUT.write_text(
        json.dumps(in_progress, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(f"New files: {written}, updated: {updated}, removed (in-progress): {removed}")
    print(f"In progress: {len(in_progress)} entries -> {IN_PROGRESS_OUT}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=str(INPUT_FILE))
    args = p.parse_args()
    main(Path(args.input))
