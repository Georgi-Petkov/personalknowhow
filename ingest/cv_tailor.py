#!/usr/bin/env python3
"""
Per-application CV tailoring: given a job posting's text, reports which of its
requirements the master CV (cv/master_cv.md) can actually back up, which bullets
support each match, and which requirements are honest gaps.

Two-tier matching (reworked 2026-08-10, same motivation as the semantic-coverage
fix in analyze_job_postings.py, but NOT the same fix -- this tool has a stricter
honesty requirement the other one doesn't, since its output feeds directly into
what actually goes on a CV):

- EXACT (unchanged): the same SKILLS regex fires on both the posting AND a
  specific CV bullet -- provably grounded, the bullet literally names the term.
  This stays regex-based on purpose; a real quote beats a similarity score.
- SEMANTIC (new): for a posting requirement with no exact-term bullet, compares
  the skill's embedding (data/skill_embeddings.json, same model/vectors as
  analyze_job_postings.py) against every CV bullet's embedding
  (data/cv_bullet_embeddings.json, content-hash-keyed against
  cv/master_cv.md's actual current bullets -- regenerate via the wrangler-dev-
  proxy embedding technique, see PKH/CLAUDE.md, whenever the CV changes).
  Always shown with its real bullet text and score, explicitly labeled as
  needing verification before being claimed -- never silently promoted to
  "matched." This is what catches a bullet that clearly backs a requirement
  in different words (e.g. posting wants "data governance," a bullet says
  "schema enforcement and data quality standards") without pretending a
  loose semantic association is the same as an exact, quotable match.

Reuses the same SKILLS vocabulary as ingest/analyze_job_postings.py for
detecting what the posting is asking for (rather than inventing a second
taxonomy) -- this half is unchanged.

This script does not rewrite prose or invent bullets. It only reports matches --
the actual tailored CV (selecting/reordering existing bullets) is produced by
pasting this report into a Claude Code chat, same pattern as
ingest/recommend_courses.py and ingest/analyze_job_postings.py.

Usage:
  python ingest/cv_tailor.py --text-file posting.txt
  cat posting.txt | python ingest/cv_tailor.py
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from analyze_job_postings import SKILLS  # noqa: E402
from common import _b64_to_vec, _cosine  # noqa: E402

ROOT = Path(__file__).parent.parent
MASTER_CV = ROOT / "cv" / "master_cv.md"
SKILL_EMBEDDINGS_FILE = ROOT / "data" / "skill_embeddings.json"
CV_BULLET_EMBEDDINGS_FILE = ROOT / "data" / "cv_bullet_embeddings.json"
SEMANTIC_FLOOR = 0.55  # lower than analyze_job_postings.py's 0.65 -- bullets are
                        # short, dense claims (not full corpus entries), and this
                        # tier is already explicitly labeled "verify before using"
                        # rather than asserted as fact, so a slightly wider net
                        # is the right trade here.


def parse_experience(text: str) -> list[dict]:
    """Each entry: {company, role, period, bullets: [str]}."""
    entries = []
    blocks = re.split(r"\n(?=### )", text)
    for block in blocks:
        header_m = re.match(r"### (.+?), .+? — (\S.*\S)$", block, re.MULTILINE)
        role_m = re.search(r"^\*\*(.+?)\*\*$", block, re.MULTILINE)
        if not header_m:
            continue
        bullets = re.findall(r"^- (.+)$", block, re.MULTILINE)
        entries.append({
            "company": header_m.group(1).strip(),
            "period": header_m.group(2).strip(),
            "role": role_m.group(1).strip() if role_m else "",
            "bullets": bullets,
        })
    return entries


def parse_section_bullets(text: str, heading: str) -> list[str]:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?:\n## |\Z)", text, re.DOTALL | re.MULTILINE)
    if not m:
        return []
    return re.findall(r"^- (.+)$", m.group(1), re.MULTILINE)


def load_master() -> dict:
    if not MASTER_CV.exists():
        print(f"ERROR: {MASTER_CV} not found")
        sys.exit(1)
    text = MASTER_CV.read_text(encoding="utf-8")
    exp_m = re.search(r"^## Professional Experience\n(.*?)(?:\n## |\Z)", text, re.DOTALL | re.MULTILINE)
    return {
        "full_text": text,
        "experience": parse_experience(exp_m.group(1)) if exp_m else [],
        "projects": parse_section_bullets(text, "Projects"),
        # Added 2026-08-10 -- these were being scanned as part of full_text for
        # the coarse jd/cv skill-set overlap, but never checked in the per-bullet
        # "which line backs this" loop, so a real Key Achievement (e.g. "Replaced
        # 13 fragmented country reports with one governed Power BI data model")
        # could never surface as supporting evidence for its own matched skill.
        "achievements": parse_section_bullets(text, "Key Achievements"),
    }


def matched_skills(text: str) -> set[str]:
    return {name for name, pattern in SKILLS if re.search(pattern, text, re.IGNORECASE)}


def all_cv_bullets(master: dict) -> list[tuple[str, str]]:
    """(source_label, bullet_text) for every bullet the semantic layer can cite."""
    out = []
    for entry in master["experience"]:
        for b in entry["bullets"]:
            out.append((entry["company"], b))
    for b in master["projects"]:
        out.append(("Project", b))
    for b in master["achievements"]:
        out.append(("Key Achievement", b))
    return out


def bullet_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def load_skill_vectors() -> dict[str, list[float]]:
    if not SKILL_EMBEDDINGS_FILE.exists():
        return {}
    data = json.loads(SKILL_EMBEDDINGS_FILE.read_text(encoding="utf-8"))
    return {name: _b64_to_vec(b64) for name, b64 in data["vectors"].items()}


def load_cv_bullet_vectors() -> dict[str, list[float]]:
    if not CV_BULLET_EMBEDDINGS_FILE.exists():
        return {}
    data = json.loads(CV_BULLET_EMBEDDINGS_FILE.read_text(encoding="utf-8"))
    return {h: _b64_to_vec(b64) for h, b64 in data["vectors"].items()}


def semantic_match(skill: str, skill_vectors: dict, bullets: list[tuple[str, str]],
                    bullet_vectors: dict) -> tuple[str, str, float] | None:
    """Best (source, bullet_text, score) for one skill, or None if nothing clears
    SEMANTIC_FLOOR or the skill/bullet has no cached embedding yet."""
    vec = skill_vectors.get(skill)
    if vec is None:
        return None
    best = None
    for source, text in bullets:
        h = bullet_hash(text)
        bvec = bullet_vectors.get(h)
        if bvec is None:
            continue  # bullet added/edited since the embedding cache was last built
        score = _cosine(vec, bvec)
        if best is None or score > best[2]:
            best = (source, text, score)
    if best is not None and best[2] >= SEMANTIC_FLOOR:
        return best
    return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--text-file", type=Path, help="File containing the job posting text")
    args = p.parse_args()

    posting_text = args.text_file.read_text(encoding="utf-8") if args.text_file else sys.stdin.read()
    if not posting_text.strip():
        print("ERROR: no job posting text provided (use --text-file or pipe via stdin)")
        sys.exit(1)

    master = load_master()
    jd_skills = matched_skills(posting_text)
    cv_skills = matched_skills(master["full_text"])

    matched = sorted(jd_skills & cv_skills)
    remaining = sorted(jd_skills - cv_skills)

    print(f"JOB POSTING: {len(posting_text)} chars")
    print(f"Requirements detected in posting: {len(jd_skills)}")
    print()

    print("=== MATCHED (posting requirement -- exact term found in master_cv.md) ===")
    if not matched:
        print("  None -- no exact-term overlap between posting requirements and the master CV's "
              "own text. Check SEMANTICALLY RELATED below before concluding poor fit.")
    for skill in matched:
        print(f"\n  {skill}")
        found_bullet = False
        for entry in master["experience"]:
            hits = [b for b in entry["bullets"] if matched_skills(b) & {skill}]
            for b in hits:
                print(f"    - [{entry['company']}] {b}")
                found_bullet = True
        for proj in master["projects"]:
            if matched_skills(proj) & {skill}:
                print(f"    - [Project] {proj}")
                found_bullet = True
        for ach in master["achievements"]:
            if matched_skills(ach) & {skill}:
                print(f"    - [Key Achievement] {ach}")
                found_bullet = True
        if not found_bullet:
            print("    (only in the Skills section -- no Experience/Project/Achievement bullet "
                  "names it explicitly)")

    # Semantic tier: catches a requirement the CV genuinely backs in different
    # words -- e.g. posting wants "data governance," a bullet says "schema
    # enforcement and data quality standards." Always shown with the real
    # bullet + score, explicitly not asserted as a match -- verify before using.
    skill_vectors = load_skill_vectors()
    bullet_vectors = load_cv_bullet_vectors()
    bullets = all_cv_bullets(master)
    semantic_hits: dict[str, tuple[str, str, float]] = {}
    unmatched: list[str] = []
    embeddings_available = bool(skill_vectors) and bool(bullet_vectors)

    for skill in remaining:
        hit = semantic_match(skill, skill_vectors, bullets, bullet_vectors) if embeddings_available else None
        if hit is not None:
            semantic_hits[skill] = hit
        else:
            unmatched.append(skill)

    print("\n=== SEMANTICALLY RELATED (not an exact term -- verify before claiming) ===")
    if not embeddings_available:
        print(f"  Skipped -- {SKILL_EMBEDDINGS_FILE.name} or {CV_BULLET_EMBEDDINGS_FILE.name} "
              f"missing. See PKH/CLAUDE.md for the wrangler-dev-proxy technique to (re)generate them.")
    elif not semantic_hits:
        print("  None.")
    else:
        for skill, (source, text, score) in sorted(semantic_hits.items(), key=lambda x: -x[1][2]):
            print(f"\n  {skill}  (similarity {score:.3f})")
            print(f"    - [{source}] {text}")

    print("\n=== UNMATCHED (posting wants this, nothing in master_cv.md backs it, even loosely) ===")
    if not unmatched:
        print("  None -- every detected requirement has some support in the master CV.")
    for skill in sorted(unmatched):
        print(f"  {skill}")

    print("\n=== NEXT STEPS ===")
    print(
        "Paste this report into a Claude Code chat along with the full job posting text.\n"
        "Ask it to select and reorder EXISTING bullets from cv/master_cv.md (never rewriting\n"
        "or inventing new ones) into a tailored variant that leads with whatever matched this\n"
        "posting's requirements. Save the result as cv/tailored/<company>_<role>_<date>.md.\n"
        "For each SEMANTICALLY RELATED hit, read the cited bullet and decide honestly whether\n"
        "it really backs that requirement before using it -- it's a real, scored candidate,\n"
        "not a confirmed match, and it may not fairly represent the requirement.\n"
        "For anything in UNMATCHED that feels like a real, honest gap, decide whether it's\n"
        "worth addressing via ingest/recommend_courses.py / ingest/analyze_job_postings.py\n"
        "before applying, or just applying as-is."
    )


if __name__ == "__main__":
    main()
