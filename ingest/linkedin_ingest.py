#!/usr/bin/env python3
"""
LinkedIn export → corpus/linkedin/ markdown files.

Reads from data/incoming/extracted/linkedin*/  (or the first extracted dir)
and produces one .md file per item using the PKH common schema.

Sources consumed:
  Certifications.csv   → corpus/linkedin/certifications/
  Learning.csv         → corpus/linkedin/learning/  (completed items only)
  Positions.csv        → corpus/linkedin/positions/
  Education.csv        → corpus/linkedin/education/
  Skills.csv           → corpus/linkedin/profile.md  (skill list, used as tags)
  Courses.csv          → corpus/linkedin/certifications/  (merged with certs)
  Endorsement_Received_Info.csv → corpus/linkedin/endorsements/  (dated, peer-corroborated)
"""
import argparse
import csv
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

ROOT       = Path(__file__).parent.parent
EXTRACT    = ROOT / "data" / "incoming" / "extracted"
CORPUS_OUT = ROOT / "corpus" / "linkedin"

# Set by main() for the duration of a run; read by write_md()/write_profile()
# so a --dry-run preview touches zero filesystem state, and the connector app
# can report accurate "N new since last sync" counts before anything is saved.
DRY_RUN = False
_NEW_PATHS: list[Path] = []

# ---------------------------------------------------------------------------
# Domain-tag inference from free text
# ---------------------------------------------------------------------------
# Was a separate, un-synced copy of TAG_PATTERNS/infer_tags here (diverged from
# common.py's over time -- e.g. never got the aws/databricks/azure/dbt/spark
# split, so a real AWS certification + 2 boto3 DataCamp courses + LinkedIn
# Skills.csv entries all under-reported as "0 known coverage" in the
# 2026-08-02 job-market gap analysis, even after common.py was fixed).
# Now imports the single shared copy so this can't drift again.
sys.path.insert(0, str(Path(__file__).parent))
from common import infer_tags, infer_role_tags, load_position_notes  # noqa: E402
from merge import parse_file, merge_frontmatters  # noqa: E402


# ---------------------------------------------------------------------------
# Date normalization
# ---------------------------------------------------------------------------
MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def date_to_iso(raw: str) -> str:
    """Convert LinkedIn date strings to YYYY-MM-DD (best effort)."""
    raw = raw.strip()
    if not raw or raw.lower() in ("present", "n/a", ""):
        return ""
    # "2025-10-25 05:19 UTC" → "2025-10-25"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    # "2022/09/21 11:56:50 UTC" → "2022-09-21"
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # "09/06/22, 06:24 AM" or "6/18/26, 6:43 AM" → ISO date
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2})", raw)
    if m:
        mm = m.group(1).zfill(2)
        dd = m.group(2).zfill(2)
        yy = m.group(3)
        yyyy = f"20{yy}" if int(yy) < 50 else f"19{yy}"
        return f"{yyyy}-{mm}-{dd}"
    # "Jan 2023" / "Jun 2017"
    m = re.match(r"([A-Za-z]{3})\s+(\d{4})", raw)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower(), "01")
        return f"{m.group(2)}-{mon}-01"
    # "2024"
    m = re.match(r"(\d{4})$", raw)
    if m:
        return f"{m.group(1)}-01-01"
    return raw


def _slug(text: str, maxlen: int = 55) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:maxlen]


def write_md(path: Path, fm: dict, body: str = "") -> None:
    existed = path.exists()
    if not existed:
        _NEW_PATHS.append(path)
    if DRY_RUN:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Was: skip entirely if path.exists() ("idempotent — merge.py handles
    # dedup"), which meant re-running this script NEVER updated an existing
    # file's domain_tags, even after a TAG_PATTERNS fix -- confirmed 2026-08-02
    # (a real AWS certification sat un-retagged through two re-ingest runs).
    # merge.py's dedup pass handles same-content-different-filename duplicates,
    # a different problem than this -- same path needs its own merge-on-write.
    if existed:
        old_fm, old_body = parse_file(path)
        fm = merge_frontmatters([old_fm, fm])
        body = old_body.strip() or body
    fm_yaml = yaml.dump(fm, allow_unicode=True, sort_keys=True, default_flow_style=False)
    path.write_text(f"---\n{fm_yaml}---\n\n{body}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-source ingest functions
# ---------------------------------------------------------------------------

def ingest_certifications(src: Path, out: Path) -> int:
    """Certifications.csv → corpus/linkedin/certifications/"""
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    count = 0
    for r in rows:
        name = r.get("Name", "").strip()
        if not name:
            continue
        authority = r.get("Authority", "").strip()
        date_raw  = r.get("Finished On", "") or r.get("Started On", "")
        date_iso  = date_to_iso(date_raw)
        url       = r.get("Url", "").strip()
        license_  = r.get("License Number", "").strip()

        slug      = _slug(name)
        date_tag  = date_iso[:7].replace("-", "") if date_iso else "0000"
        path      = out / f"{slug}_{date_tag}.md"

        desc_parts = [f"Issued by {authority}." if authority else ""]
        if license_:
            desc_parts.append(f"License: {license_}")
        if url:
            desc_parts.append(f"URL: {url}")
        description = " ".join(p for p in desc_parts if p)

        tags = infer_tags(name + " " + authority)

        fm = {
            "title":       name,
            "type":        "certification",
            "provider":    authority or "LinkedIn",
            "date":        date_iso or None,
            "description": description,
            "domain_tags": tags,
        }
        write_md(path, fm, description)
        count += 1
    return count


def ingest_courses(src: Path, out: Path) -> int:
    """Courses.csv (sparse — just Name) → corpus/linkedin/certifications/"""
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    count = 0
    for r in rows:
        name = r.get("Name", "").strip()
        if not name:
            continue
        slug  = _slug(name)
        path  = out / f"{slug}_courses.md"
        tags  = infer_tags(name)
        fm = {
            "title":       name,
            "type":        "certification",
            "provider":    "LinkedIn / Coursera",
            "date":        None,
            "description": f"Completed {name}.",
            "domain_tags": tags,
        }
        write_md(path, fm, f"Completed {name}.")
        count += 1
    return count


def ingest_learning(src: Path, out: Path) -> int:
    """Learning.csv (completed items only) → corpus/linkedin/learning/"""
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    count = 0
    for r in rows:
        completed_at = r.get("Content Completed At (if completed)", "").strip()
        if not completed_at or completed_at.upper() == "N/A":
            continue
        title = r.get("Content Title", "").strip()
        if not title:
            continue
        desc_raw  = r.get("Content Description", "").strip()
        ctype     = r.get("Content Type", "Course").strip()
        date_iso  = date_to_iso(completed_at)
        slug      = _slug(title)
        date_tag  = date_iso[:7].replace("-", "") if date_iso else "0000"
        path      = out / f"{slug}_{date_tag}.md"

        # Truncate description for YAML field; full text goes in body
        short_desc = (desc_raw[:200] + "…") if len(desc_raw) > 200 else desc_raw
        tags       = infer_tags(title + " " + desc_raw[:200])

        fm = {
            "title":       title,
            "type":        ctype.lower() if ctype else "course",
            "provider":    "LinkedIn Learning",
            "date":        date_iso or None,
            "description": short_desc,
            "domain_tags": tags,
        }
        write_md(path, fm, desc_raw)
        count += 1
    return count


def ingest_positions(src: Path, out: Path) -> int:
    """Positions.csv → corpus/linkedin/positions/"""
    rows  = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    notes = load_position_notes()
    count = 0
    for r in rows:
        title   = r.get("Title", "").strip()
        company = r.get("Company Name", "").strip()
        if not title or not company:
            continue
        desc     = r.get("Description", "").strip()
        location = r.get("Location", "").strip()
        date_iso = date_to_iso(r.get("Started On", ""))
        end_iso  = date_to_iso(r.get("Finished On", ""))
        slug     = _slug(f"{title}_{company}")
        date_tag = date_iso[:7].replace("-", "") if date_iso else "0000"
        path     = out / f"{slug}_{date_tag}.md"

        note = notes.get(f"{title.lower()} @ {company.lower()}", "")

        body_parts = [f"**Role:** {title} at {company}"]
        if location:
            body_parts.append(f"**Location:** {location}")
        if date_iso:
            end_label = end_iso if end_iso else "Present"
            body_parts.append(f"**Period:** {date_iso} – {end_label}")
        if desc:
            body_parts.append(f"\n{desc}")
        if note:
            body_parts.append(f"\n**What I actually did:**\n{note}")
        body = "\n\n".join(body_parts)

        full_text = f"{title} {company} {desc} {note}"
        tags      = sorted(set(infer_tags(full_text)) | set(infer_role_tags(title)))

        short_desc = f"{title} at {company}"
        if location:
            short_desc += f", {location}"
        if note:
            short_desc += f" — {note}"

        fm = {
            "title":       f"{title} at {company}",
            "type":        "position",
            "provider":    company,
            "date":        date_iso or None,
            "description": short_desc,
            "domain_tags": tags,
        }
        write_md(path, fm, body)
        count += 1
    return count


def ingest_education(src: Path, out: Path) -> int:
    """Education.csv → corpus/linkedin/education/"""
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    count = 0
    for r in rows:
        school = r.get("School Name", "").strip()
        degree = r.get("Degree Name", "").strip()
        if not school and not degree:
            continue
        notes      = r.get("Notes", "").strip()
        activities = r.get("Activities", "").strip()
        end_iso    = date_to_iso(r.get("End Date", "") or r.get("Start Date", ""))
        title      = degree if degree else f"Studies at {school}"
        slug       = _slug(f"{title}_{school}")
        date_tag   = end_iso[:7].replace("-", "") if end_iso else "0000"
        path       = out / f"{slug}_{date_tag}.md"

        body_parts = [f"**School:** {school}"]
        if degree:
            body_parts.append(f"**Degree:** {degree}")
        if notes:
            body_parts.append(f"\n{notes}")
        body = "\n\n".join(body_parts)

        # Activities field uses "|" as separator
        act_tags = [a.strip().lower().replace(" ", "-")
                    for a in activities.split("|") if a.strip()] if activities else []
        inferred = infer_tags(f"{title} {school} {notes} {activities}")
        tags      = sorted(set(act_tags + inferred))

        fm = {
            "title":       title,
            "type":        "education",
            "provider":    school,
            "date":        end_iso or None,
            "description": notes[:200] if notes else f"{degree} at {school}",
            "domain_tags": tags,
        }
        write_md(path, fm, body)
        count += 1
    return count


def ingest_articles(src_dir: Path, out: Path) -> int:
    """Articles/Articles/*.html → corpus/linkedin/articles/"""
    import html as html_mod
    articles_dir = src_dir / "Articles" / "Articles"
    if not articles_dir.exists():
        return 0
    count = 0
    for html_file in sorted(articles_dir.glob("*.html")):
        raw = html_file.read_text(encoding="utf-8")

        # strip style/script blocks
        raw = re.sub(r"<style[^>]*>.*?</style>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL)

        # extract title from <title> tag
        title_m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.DOTALL | re.IGNORECASE)
        title = html_mod.unescape(title_m.group(1).strip()) if title_m else html_file.stem

        # extract published date
        pub_m = re.search(r"Published on (\d{4}-\d{2}-\d{2})", raw)
        date_iso = pub_m.group(1) if pub_m else ""

        # strip all tags and collapse whitespace
        text = re.sub(r"<[^>]+>", " ", raw)
        text = html_mod.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()

        # drop leading CSS noise — everything before and including the date line
        idx = text.find("Published on")
        if idx >= 0:
            # skip past "Published on YYYY-MM-DD HH:MM"
            after = text[idx + 30:].lstrip()
            body = after
        else:
            body = text

        slug     = _slug(title)
        date_tag = date_iso[:7].replace("-", "") if date_iso else "0000"
        path     = out / f"{slug}_{date_tag}.md"

        tags = infer_tags(title + " " + body[:500])

        fm = {
            "title":       title,
            "type":        "article",
            "provider":    "LinkedIn",
            "date":        date_iso or None,
            "description": body[:200].strip() + ("…" if len(body) > 200 else ""),
            "domain_tags": tags,
        }
        write_md(path, fm, body)
        count += 1
    return count


def ingest_recommendations_received(src: Path, out: Path) -> int:
    """Recommendations_Received.csv → corpus/linkedin/recommendations/"""
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    count = 0
    for r in rows:
        text      = r.get("Text", "").strip()
        first     = r.get("First Name", "").strip()
        last      = r.get("Last Name", "").strip()
        company   = r.get("Company", "").strip()
        job_title = r.get("Job Title", "").strip()
        date_raw  = r.get("Creation Date", "").strip()

        if not text:
            continue

        recommender = f"{first} {last}".strip()
        date_iso    = date_to_iso(date_raw)
        slug        = _slug(f"recommendation_{recommender}")
        date_tag    = date_iso[:7].replace("-", "") if date_iso else "0000"
        path        = out / f"{slug}_{date_tag}.md"

        body = f"**Recommender:** {recommender}"
        if job_title:
            body += f", {job_title}"
        if company:
            body += f" at {company}"
        body += f"\n\n{text}"

        tags = infer_tags(text + " " + job_title)

        fm = {
            "title":       f"Recommendation from {recommender}",
            "type":        "recommendation",
            "provider":    company or "LinkedIn",
            "date":        date_iso or None,
            "description": text[:200].strip() + ("…" if len(text) > 200 else ""),
            "domain_tags": tags,
        }
        write_md(path, fm, body)
        count += 1
    return count


def _parse_qa(raw: str) -> dict[str, str]:
    """Parse 'Key:Value | Key:Value' Q&A strings from job applications."""
    result = {}
    if not raw:
        return result
    for part in raw.split(" | "):
        if ":" in part:
            k, _, v = part.partition(":")
            result[k.strip()] = v.strip()
    return result


ACTIVE_SEARCH_CUTOFF = "2026-06"  # applications from this month onward are active pursuit


def ingest_job_applications(src: Path, app_out: Path, interest_out: Path) -> tuple[int, int]:
    """
    Jobs/Job Applications.csv → two folders:
      - app_out/       (type: job_application)  — June 2026+, active search
      - interest_out/  (type: career_interest)  — older, general domain signal only
    Returns (active_count, interest_count).
    """
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    active_count = interest_count = 0

    for r in rows:
        job_title = r.get("Job Title", "").strip()
        company   = r.get("Company Name", "").strip()
        if not job_title:
            continue
        date_raw = r.get("Application Date", "").strip()
        date_iso = date_to_iso(date_raw)
        url      = r.get("Job Url", "").strip()
        slug     = _slug(f"{job_title}_{company}")
        date_tag = date_iso[:7].replace("-", "") if date_iso else "0000"
        tags     = infer_tags(job_title + " " + company)

        if date_iso >= ACTIVE_SEARCH_CUTOFF:
            # Active application — full detail including Q&A experience signals
            qa       = _parse_qa(r.get("Question And Answers", "").strip())
            exp_lines = [
                f"- {k}: {v}"
                for k, v in qa.items()
                if "year" in k.lower() and v.strip().lstrip("-").isdigit()
            ]
            body = f"**Role:** {job_title} at {company}\n"
            if date_iso:
                body += f"**Applied:** {date_iso}\n"
            if url:
                body += f"**URL:** {url}\n"
            if exp_lines:
                body += "\n**Experience indicated:**\n" + "\n".join(exp_lines)
            tags = infer_tags(job_title + " " + company + " " + " ".join(
                f"{k} {v}" for k, v in qa.items() if "year" in k.lower()
            ))
            fm = {
                "title":       f"{job_title} at {company}",
                "type":        "job_application",
                "provider":    company or "LinkedIn",
                "date":        date_iso or None,
                "description": f"Applied for {job_title} at {company}",
                "domain_tags": tags,
            }
            write_md(app_out / f"{slug}_{date_tag}.md", fm, body)
            active_count += 1
        else:
            # Historical interest — lightweight domain signal
            fm = {
                "title":       f"{job_title} at {company}",
                "type":        "career_interest",
                "provider":    company or "LinkedIn",
                "date":        date_iso or None,
                "description": f"Previously applied for {job_title} at {company}",
                "domain_tags": tags,
            }
            write_md(interest_out / f"{slug}_{date_tag}.md", fm)
            interest_count += 1

    return active_count, interest_count


def ingest_saved_jobs(src: Path, out: Path) -> int:
    """Jobs/Saved Jobs.csv → corpus/linkedin/career_interests/ (domain signal only)."""
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    count = 0
    for r in rows:
        job_title = r.get("Job Title", "").strip()
        company   = r.get("Company Name", "").strip()
        if not job_title:
            continue
        date_raw = r.get("Saved Date", "").strip()
        date_iso = date_to_iso(date_raw)
        slug     = _slug(f"saved_{job_title}_{company}")
        date_tag = date_iso[:7].replace("-", "") if date_iso else "0000"
        tags     = infer_tags(job_title + " " + company)
        fm = {
            "title":       f"{job_title} at {company}",
            "type":        "career_interest",
            "provider":    company or "LinkedIn",
            "date":        date_iso or None,
            "description": f"Saved job: {job_title} at {company}",
            "domain_tags": tags,
        }
        write_md(out / f"{slug}_{date_tag}.md", fm)
        count += 1
    return count


def ingest_skills(src: Path, out_dir: Path) -> list[str]:
    """Skills.csv → list of skill strings (written into profile.md)."""
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    return [r.get("Name", "").strip() for r in rows if r.get("Name", "").strip()]


def ingest_endorsements(src: Path, out: Path) -> int:
    """Endorsement_Received_Info.csv → corpus/linkedin/endorsements/

    Peer-endorsed skills LinkedIn never exposes anywhere else in the export --
    Positions.csv has no description text and Skills.csv is a flat, dateless
    list, so this is the only dated, third-party-corroborated skill signal
    available. Still has no link to which job it came from (LinkedIn doesn't
    export that), so this stays a separate, standalone entry per skill rather
    than being merged into position notes.
    """
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    earliest: dict[str, str] = {}
    for r in rows:
        skill = r.get("Skill Name", "").strip()
        if not skill or r.get("Endorsement Status", "").strip() != "ACCEPTED":
            continue
        date_iso = date_to_iso(r.get("Endorsement Date", ""))
        if skill not in earliest or (date_iso and date_iso < earliest[skill]):
            earliest[skill] = date_iso

    count = 0
    for skill, date_iso in earliest.items():
        slug     = _slug(skill)
        date_tag = date_iso[:7].replace("-", "") if date_iso else "0000"
        path     = out / f"{slug}_{date_tag}.md"
        tags     = infer_tags(skill)

        fm = {
            "title":       f"Endorsed: {skill}",
            "type":        "endorsement",
            "provider":    "LinkedIn",
            "date":        date_iso or None,
            "description": f"Peer-endorsed for {skill} on LinkedIn",
            "domain_tags": tags,
        }
        write_md(path, fm, f"**Skill:** {skill}\n\n**First endorsed:** {date_iso or 'unknown'}")
        count += 1
    return count


def write_profile(skills: list[str], profile_src: Path, out: Path) -> None:
    """Combine Profile.csv headline/summary + Skills into a profile.md."""
    profile_rows = list(csv.DictReader(profile_src.open(encoding="utf-8-sig")))
    p = profile_rows[0] if profile_rows else {}
    name     = f"{p.get('First Name', '')} {p.get('Last Name', '')}".strip()
    headline = p.get("Headline", "").strip()
    summary  = p.get("Summary", "").strip()
    industry = p.get("Industry", "").strip()

    path = out / "profile.md"
    existed = path.exists()
    if not existed:
        _NEW_PATHS.append(path)
    if DRY_RUN:
        return

    tags = infer_tags(headline + " " + summary + " " + " ".join(skills[:30]))

    fm = {
        "title":       name or "LinkedIn Profile",
        "type":        "profile",
        "provider":    "LinkedIn",
        "date":        None,
        "description": headline or summary[:200],
        "domain_tags": tags,
    }
    fm_yaml = yaml.dump(fm, allow_unicode=True, sort_keys=True, default_flow_style=False)
    body = f"## {name}\n\n**Headline:** {headline}\n\n**Industry:** {industry}\n\n"
    if summary:
        body += f"## Summary\n\n{summary}\n\n"
    if skills:
        body += "## Skills\n\n" + "\n".join(f"- {s}" for s in skills)
    path.write_text(f"---\n{fm_yaml}---\n\n{body}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def find_extract_dir() -> Path:
    """Return the path to the LinkedIn CSV directory, or Path() if not found."""
    # Most-recent linkedin* folder under data/incoming/extracted/
    candidates = sorted(EXTRACT.glob("linkedin*"), reverse=True) if EXTRACT.exists() else []
    if candidates:
        return candidates[0]
    # Any dir under data/incoming/ that contains Certifications.csv
    incoming = ROOT / "data" / "incoming"
    if incoming.exists():
        for d in sorted(incoming.iterdir(), reverse=True):
            if d.is_dir() and (d / "Certifications.csv").exists():
                return d
    return Path()


def main(extract_dir: Path, dry_run: bool = False, json_summary: bool = False) -> None:
    global DRY_RUN, _NEW_PATHS
    DRY_RUN = dry_run
    _NEW_PATHS = []

    def _mkdir(p: Path) -> None:
        if not dry_run:
            p.mkdir(parents=True, exist_ok=True)

    if not extract_dir.exists():
        msg = f"extract dir not found: {extract_dir}"
        print(f"ERROR: {msg}")
        if json_summary:
            print("PKH_JSON_SUMMARY: " + json.dumps({"ok": False, "error": msg}))
        sys.exit(1)

    print(f"Reading from: {extract_dir}" + (" [dry run — nothing will be written]" if dry_run else ""))
    total = 0
    summary: dict[str, int] = {}

    cert_out = CORPUS_OUT / "certifications"
    _mkdir(cert_out)
    if (extract_dir / "Certifications.csv").exists():
        n = ingest_certifications(extract_dir / "Certifications.csv", cert_out)
        print(f"  Certifications : {n}")
        summary["certifications"] = n
        total += n
    if (extract_dir / "Courses.csv").exists():
        n = ingest_courses(extract_dir / "Courses.csv", cert_out)
        print(f"  Courses        : {n}")
        summary["courses"] = n
        total += n

    learn_out = CORPUS_OUT / "learning"
    _mkdir(learn_out)
    if (extract_dir / "Learning.csv").exists():
        n = ingest_learning(extract_dir / "Learning.csv", learn_out)
        print(f"  Learning       : {n} (completed only)")
        summary["learning"] = n
        total += n

    pos_out = CORPUS_OUT / "positions"
    _mkdir(pos_out)
    if (extract_dir / "Positions.csv").exists():
        n = ingest_positions(extract_dir / "Positions.csv", pos_out)
        print(f"  Positions      : {n}")
        summary["positions"] = n
        total += n

    edu_out = CORPUS_OUT / "education"
    _mkdir(edu_out)
    if (extract_dir / "Education.csv").exists():
        n = ingest_education(extract_dir / "Education.csv", edu_out)
        print(f"  Education      : {n}")
        summary["education"] = n
        total += n

    jobs_dir = extract_dir / "Jobs"
    if jobs_dir.exists():
        app_out      = CORPUS_OUT / "job_applications"
        interest_out = CORPUS_OUT / "career_interests"
        _mkdir(app_out)
        _mkdir(interest_out)

        app_src = jobs_dir / "Job Applications.csv"
        if app_src.exists():
            active, historical = ingest_job_applications(app_src, app_out, interest_out)
            print(f"  Job Applications (active) : {active}  [{ACTIVE_SEARCH_CUTOFF}+]")
            print(f"  Job Applications (signal) : {historical}  [pre-{ACTIVE_SEARCH_CUTOFF}]")
            summary["job_applications_active"] = active
            summary["job_applications_signal"] = historical
            total += active + historical

        saved_src = jobs_dir / "Saved Jobs.csv"
        if saved_src.exists():
            n = ingest_saved_jobs(saved_src, interest_out)
            print(f"  Saved Jobs (signal)       : {n}")
            summary["saved_jobs"] = n
            total += n

    art_out = CORPUS_OUT / "articles"
    _mkdir(art_out)
    n = ingest_articles(extract_dir, art_out)
    if n:
        print(f"  Articles       : {n}")
        summary["articles"] = n
        total += n

    rec_out = CORPUS_OUT / "recommendations"
    _mkdir(rec_out)
    if (extract_dir / "Recommendations_Received.csv").exists():
        n = ingest_recommendations_received(extract_dir / "Recommendations_Received.csv", rec_out)
        if n:
            print(f"  Recommendations: {n}")
            summary["recommendations"] = n
            total += n

    endorse_out = CORPUS_OUT / "endorsements"
    _mkdir(endorse_out)
    if (extract_dir / "Endorsement_Received_Info.csv").exists():
        n = ingest_endorsements(extract_dir / "Endorsement_Received_Info.csv", endorse_out)
        print(f"  Endorsements   : {n}")
        summary["endorsements"] = n
        total += n

    skills = []
    if (extract_dir / "Skills.csv").exists():
        skills = ingest_skills(extract_dir / "Skills.csv", CORPUS_OUT)
        print(f"  Skills         : {len(skills)}")
        summary["skills"] = len(skills)

    if (extract_dir / "Profile.csv").exists():
        write_profile(skills, extract_dir / "Profile.csv", CORPUS_OUT)
        print(f"  Profile        : 1")
        summary["profile"] = 1
        total += 1

    print(f"\nTotal files written to corpus/linkedin/: {total}"
          + (" [dry run — nothing written]" if dry_run else ""))

    if json_summary:
        print("PKH_JSON_SUMMARY: " + json.dumps({
            "ok": True,
            "extract_dir": str(extract_dir),
            "dry_run": dry_run,
            "sources": summary,
            "total_items": total,
            "new_items": len(_NEW_PATHS),
            "updated_items": max(total - len(_NEW_PATHS), 0),
        }))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Ingest LinkedIn export CSVs into corpus/.")
    p.add_argument("--extract-dir", default="",
                   help="Path to extracted LinkedIn export folder (auto-detected if omitted)")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be written without writing anything")
    p.add_argument("--json-summary", action="store_true",
                   help="Print a final PKH_JSON_SUMMARY: {...} line for programmatic callers")
    args = p.parse_args()

    if args.extract_dir:
        extract_dir = Path(args.extract_dir)
    else:
        extract_dir = find_extract_dir()
        if not (extract_dir / "Certifications.csv").exists():
            print(f"No LinkedIn extract found under data/incoming/. "
                  f"Pass --extract-dir <path> to the unzipped LinkedIn export folder.")
            sys.exit(1)

    main(extract_dir, dry_run=args.dry_run, json_summary=args.json_summary)
