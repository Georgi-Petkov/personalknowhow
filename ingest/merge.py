#!/usr/bin/env python3
"""
Idempotent merge/dedupe for corpus markdown files.
Keyed by (title.lower(), date) so re-running with new/partial exports
doesn't duplicate or overwrite history.
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "pyyaml", "-q",
                   "--break-system-packages"], check=True)
    import yaml

CORPUS_DIR = Path(__file__).parent.parent / "corpus"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_file(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    return fm, text[m.end():]


def dedup_key(fm: dict) -> tuple[str, str, str]:
    # Was (title, date) only -- collided a real job_application with an
    # unrelated career_interest that happened to share a title+date (applied
    # to a job the same day it was saved), silently discarding the
    # application's type and Q&A evidence body on merge. Confirmed 2026-08-09
    # via the PKH LinkedIn Export Connector's real (non-mock) save path.
    # Including type keeps these as separate entries -- true duplicates
    # (same source, same type, re-ingested) still share all three fields.
    title = str(fm.get("title", "")).strip().lower()
    date = str(fm.get("date", "")).strip()
    type_ = str(fm.get("type", "")).strip().lower()
    return title, date, type_


def merge_frontmatters(records: list[dict]) -> dict:
    """Merge multiple frontmatter dicts. Union for lists, longest description wins."""
    merged: dict = {}
    for fm in records:
        for k, v in fm.items():
            if not v and v != 0:
                continue
            if k == "domain_tags":
                existing = merged.get(k) or []
                incoming = v if isinstance(v, list) else [v]
                merged[k] = sorted(set(str(t) for t in existing + incoming))
            elif k == "description":
                if len(str(v)) > len(str(merged.get(k, ""))):
                    merged[k] = v
            elif k not in merged or not merged[k]:
                merged[k] = v
    return merged


def write_file(path: Path, fm: dict, body: str) -> None:
    fm_yaml = yaml.dump(fm, allow_unicode=True, sort_keys=True, default_flow_style=False)
    path.write_text(f"---\n{fm_yaml}---\n{body}", encoding="utf-8")


def merge_corpus(corpus_dir: Path = CORPUS_DIR, dry_run: bool = False) -> dict:
    if not corpus_dir.exists():
        print(f"corpus dir not found: {corpus_dir}")
        return {"scanned": 0, "duplicates_found": 0, "files_removed": 0}

    all_files = sorted(corpus_dir.rglob("*.md"))
    print(f"Scanning {len(all_files)} markdown files in {corpus_dir} ...")

    groups: dict[tuple, list[tuple]] = {}
    no_key: list[Path] = []

    for path in all_files:
        fm, body = parse_file(path)
        if not fm:
            no_key.append(path)
            continue
        key = dedup_key(fm)
        if not key[0]:
            no_key.append(path)
            continue
        groups.setdefault(key, []).append((path, fm, body))

    duplicates_found = 0
    files_removed = 0

    for key, entries in groups.items():
        if len(entries) <= 1:
            continue
        duplicates_found += len(entries) - 1
        print(f"  Duplicate ({len(entries)}x) title={key[0]!r} date={key[1]!r} type={key[2]!r}")

        # Keep the entry with the most complete frontmatter as canonical
        entries.sort(key=lambda e: -sum(1 for v in e[1].values() if v))
        canonical_path, _, canonical_body = entries[0]
        merged_fm = merge_frontmatters([e[1] for e in entries])

        if not dry_run:
            write_file(canonical_path, merged_fm, canonical_body)
            for path, _, _ in entries[1:]:
                print(f"    Removing: {path}")
                path.unlink()
                files_removed += 1
        else:
            print(f"    Would keep: {canonical_path}")
            for path, _, _ in entries[1:]:
                print(f"    Would remove: {path}")

    scanned = len(all_files)
    print(
        f"Done — scanned: {scanned}, duplicates merged: {duplicates_found}, "
        f"files removed: {files_removed}, skipped (no key): {len(no_key)}"
    )
    return {"scanned": scanned, "duplicates_found": duplicates_found, "files_removed": files_removed}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Deduplicate corpus markdown files.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--corpus", default=str(CORPUS_DIR))
    p.add_argument("--json-summary", action="store_true",
                   help="Print a final PKH_JSON_SUMMARY: {...} line for programmatic callers")
    args = p.parse_args()
    result = merge_corpus(Path(args.corpus), dry_run=args.dry_run)
    if args.json_summary:
        print("PKH_JSON_SUMMARY: " + json.dumps({**result, "dry_run": args.dry_run}))
