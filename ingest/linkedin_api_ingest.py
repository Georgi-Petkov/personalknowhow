#!/usr/bin/env python3
"""
LinkedIn Member Data Portability API → corpus/linkedin/ markdown files.

This is the primary LinkedIn ingestion path (see
~/.claude/plans/pkh-linkedin-api-rebuild.md — explicit instruction not to
glue this onto linkedin_ingest.py's CSV parsers). linkedin_ingest.py's CSV
path stays as a documented fallback for fork users without API access; it is
NOT extended or reused for field-mapping logic here, only for its generic
file-writing helpers (write_md/_slug/date_to_iso), which are genuinely
source-agnostic already.

Architecture: the API's snapshotData shape is a flat list of key-value dicts
for every domain — unlike the CSVs, which each had bespoke column layouts.
Most domains below are handled by one generic, config-driven function
(GENERIC_DOMAINS + ingest_generic_domain). A handful of domains have a row
shape that doesn't fit "one entry per item" (PROFILE+SKILLS combine into one
file; LEARNING needs a completed-only filter; JOB_APPLICATIONS needs a
date-cutoff split into two corpus folders; JOB_SEEKER_PREFERENCES is a
single always-one-row record) and get their own small function instead of
being forced into the generic shape.

Domain scope (confirmed with the user 2026-08-10 — see conversation, not
re-litigated here):
  - Public-tier: PROFILE, POSITIONS, EDUCATION, SKILLS, CERTIFICATIONS,
    COURSES, LEARNING, PROJECTS, ORGANIZATIONS, LANGUAGES, HONORS,
    PUBLICATIONS, PATENTS, VOLUNTEERING_EXPERIENCES, RECOMMENDATIONS,
    ENDORSEMENTS, TEST_SCORES (demonstrated-evidence tier, same reasoning as
    certifications — not signal_only).
  - Private-tier: JOB_APPLICATIONS, SAVED_JOBS, SAVED_JOB_ALERTS,
    JOB_SEEKER_PREFERENCES, JOB_APPLICANT_SAVED_ANSWERS,
    TALENT_QUESTION_SAVED_RESPONSE.
  - Excluded entirely (not ingested): CAUSES_YOU_CARE_ABOUT (could reveal
    political/religious/personal leanings), PROFILE_SUMMARY (content never
    inspected), plus the full "not relevant to a skills/experience graph"
    list in the rebuild plan (ADS_CLICKED, CONNECTIONS, EMAIL_ADDRESSES,
    PHONE_NUMBERS, etc.) — never fetched at all, not just filtered post-hoc.
  - ARTICLES has no Snapshot API equivalent (confirmed 2026-08-09) — stays
    CSV-only via linkedin_ingest.py's ingest_articles(), not in this file.

"Tier" on each DomainConfig is informational only (drives which corpus
subfolder it lands in, matching the existing career_interests/
job_applications precedent) — the actual public/private enforcement lives in
build_public_export.py's ALLOWLIST / build_private_export.py's TYPE_MAP,
which must each be updated deliberately when a new corpus subfolder appears
here (fail-closed discipline — a new folder is private-by-omission until
added to ALLOWLIST on purpose).

PII note: PROFILE's real API response includes Birth Date, Address, Zip
Code, Maiden Name, and Instant Messengers — none of which the old CSV-based
Profile.csv path ever exposed (it only had Headline/Summary/Industry/name).
Those fields are deliberately never written to corpus/ at all here, not just
filtered at export time — profile.md lands in the PUBLIC allowlist, so
anything written here needs to be safe unauthenticated-web-reader content.
"""
import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
from common import infer_tags, infer_role_tags  # noqa: E402
from linkedin_api import fetch_domain, load_token, LinkedInAPIError, TOKEN_FILE  # noqa: E402
import linkedin_ingest as li  # noqa: E402 — reuse write_md/_slug/date_to_iso, share its DRY_RUN state

CORPUS_OUT = li.CORPUS_OUT  # corpus/linkedin/

ACTIVE_SEARCH_CUTOFF = li.ACTIVE_SEARCH_CUTOFF  # "2026-06" — kept in sync with the CSV path


# ---------------------------------------------------------------------------
# Generic domain-config machinery
# ---------------------------------------------------------------------------

@dataclass
class DomainConfig:
    corpus_subdir: str
    entry_type: str
    tier: str  # "public" | "private" — see module docstring
    title_fields: list[str] = field(default_factory=list)
    provider_fields: list[str] = field(default_factory=list)
    date_fields: list[str] = field(default_factory=list)
    text_fields: list[str] = field(default_factory=list)  # main free-text -> description/body/tag inference
    compose_title: Callable[[dict], str] | None = None  # overrides title_fields when a row needs combining
    title_fmt: Callable[[str, str], str] | None = None  # (title, provider) -> final display title
    role_tag_field: str | None = None  # also run infer_role_tags() on this field's value
    row_filter: Callable[[dict], bool] | None = None
    exclude_fields: list[str] = field(default_factory=list)  # never surfaced, even in the generic body dump


def _first_nonempty(row: dict, fields: list[str]) -> str:
    for f in fields:
        v = str(row.get(f, "") or "").strip()
        if v:
            return v
    return ""


GENERIC_DOMAINS: dict[str, DomainConfig] = {
    "POSITIONS": DomainConfig(
        corpus_subdir="positions", entry_type="position", tier="public",
        title_fields=["Title"], provider_fields=["Company Name"],
        date_fields=["Started On"], text_fields=["Description"],
        title_fmt=lambda t, p: f"{t} at {p}" if p else t,
        role_tag_field="Title",
    ),
    "EDUCATION": DomainConfig(
        corpus_subdir="education", entry_type="education", tier="public",
        title_fields=["Degree Name"], provider_fields=["School Name"],
        date_fields=["End Date", "Start Date"], text_fields=["Notes"],
        title_fmt=lambda t, p: t or (f"Studies at {p}" if p else "Studies"),
    ),
    "CERTIFICATIONS": DomainConfig(
        corpus_subdir="certifications", entry_type="certification", tier="public",
        title_fields=["Name"], provider_fields=["Authority"],
        date_fields=["Finished On", "Started On"],
    ),
    "COURSES": DomainConfig(
        corpus_subdir="certifications", entry_type="certification", tier="public",
        title_fields=["Name"],
    ),
    "PROJECTS": DomainConfig(
        # LinkedIn's own "Projects" section — kept separate from corpus/projects/
        # (project_ingest.py's sibling-git-repo scan) and corpus/github/, which
        # are different evidence sources describing the same real-world work.
        corpus_subdir="projects_li", entry_type="project", tier="public",
        title_fields=["Title"], date_fields=["Started On", "Finished On"],
        text_fields=["Description"],
    ),
    "ORGANIZATIONS": DomainConfig(
        corpus_subdir="organizations", entry_type="organization", tier="public",
        title_fields=["Organization Name", "Name"], provider_fields=["Position", "Role"],
        date_fields=["Started On", "Finished On"], text_fields=["Description"],
    ),
    "LANGUAGES": DomainConfig(
        # Field names are best-guess — LinkedIn returns 404 "no data" for this
        # domain until the user adds Languages on their profile (as of
        # 2026-08-10). Verify against a real response once that's done; this
        # config activates automatically once rows exist, no code change
        # needed if the guessed field names are right.
        corpus_subdir="languages", entry_type="language", tier="public",
        title_fields=["Name", "Language"], provider_fields=["Proficiency"],
    ),
    "HONORS": DomainConfig(
        corpus_subdir="honors", entry_type="honor", tier="public",
        title_fields=["Title", "Name"], provider_fields=["Issuer", "Authority"],
        date_fields=["Issued On", "Started On"], text_fields=["Description"],
    ),
    "PUBLICATIONS": DomainConfig(
        corpus_subdir="publications", entry_type="publication", tier="public",
        title_fields=["Title", "Name"], provider_fields=["Publisher"],
        date_fields=["Published On", "Date"], text_fields=["Description"],
    ),
    "PATENTS": DomainConfig(
        corpus_subdir="patents", entry_type="patent", tier="public",
        title_fields=["Title", "Name"], provider_fields=["Office", "Authority"],
        date_fields=["Issued On", "Filed On"], text_fields=["Description"],
    ),
    "VOLUNTEERING_EXPERIENCES": DomainConfig(
        corpus_subdir="volunteering", entry_type="volunteering", tier="public",
        title_fields=["Role", "Title"], provider_fields=["Company Name", "Organization Name"],
        date_fields=["Started On"], text_fields=["Description"],
        title_fmt=lambda t, p: f"{t} at {p}" if t and p else (t or p),
    ),
    "TEST_SCORES": DomainConfig(
        corpus_subdir="test_scores", entry_type="test_score", tier="public",
        title_fields=["Test Name", "Name"], provider_fields=["Score"],
        date_fields=["Date"], text_fields=["Description"],
    ),
    "RECOMMENDATIONS": DomainConfig(
        # API bundles recommendations GIVEN onto a second page alongside
        # RECEIVED (discovered live 2026-08-10 — Status="VISIBLE" for
        # received, Status="" for given). Only received is third-party
        # corroboration of the user's own skill; given says nothing about
        # the user's own experience, so it's filtered out here rather than
        # invented as a new corpus category without the user asking for one.
        corpus_subdir="recommendations", entry_type="recommendation", tier="public",
        title_fields=["First Name", "Last Name"], provider_fields=["Company"],
        date_fields=["Creation Date"], text_fields=["Text"],
        compose_title=lambda r: f"{r.get('First Name', '').strip()} {r.get('Last Name', '').strip()}".strip(),
        title_fmt=lambda t, p: f"Recommendation from {t}" if t else "Recommendation",
        row_filter=lambda r: r.get("Status") == "VISIBLE",
    ),
    "ENDORSEMENTS": DomainConfig(
        # Same given/received split as RECOMMENDATIONS, distinguished here by
        # field shape (received rows have "Endorser First Name"; given rows
        # have "Endorsee First Name" instead) rather than a status flag.
        corpus_subdir="endorsements", entry_type="endorsement", tier="public",
        title_fields=["Skill Name"], date_fields=["Endorsement Date"],
        title_fmt=lambda t, p: f"Endorsed: {t}",
        row_filter=lambda r: r.get("Endorsement Status") == "ACCEPTED" and "Endorser First Name" in r,
    ),
    "SAVED_JOBS": DomainConfig(
        corpus_subdir="career_interests", entry_type="career_interest", tier="private",
        title_fields=["Job Title"], provider_fields=["Company Name"],
        date_fields=["Saved Date"],
        title_fmt=lambda t, p: f"{t} at {p}" if p else t,
    ),
    "JOB_APPLICANT_SAVED_ANSWERS": DomainConfig(
        corpus_subdir="job_applicant_saved_answers", entry_type="saved_answer", tier="private",
        title_fields=["Question"], text_fields=["Answer"],
    ),
    "TALENT_QUESTION_SAVED_RESPONSE": DomainConfig(
        corpus_subdir="talent_question_saved_responses", entry_type="saved_answer", tier="private",
        title_fields=["Question"], text_fields=["Answer"],
    ),
}


def ingest_generic_domain(domain: str, cfg: DomainConfig, rows: list[dict], out_dir: Path) -> int:
    count = 0
    used_fields = (set(cfg.title_fields) | set(cfg.provider_fields) | set(cfg.date_fields)
                   | set(cfg.text_fields) | set(cfg.exclude_fields))
    for row in rows:
        if cfg.row_filter and not cfg.row_filter(row):
            continue
        title = cfg.compose_title(row) if cfg.compose_title else _first_nonempty(row, cfg.title_fields)
        provider = _first_nonempty(row, cfg.provider_fields)
        date_raw = _first_nonempty(row, cfg.date_fields)
        date_iso = li.date_to_iso(date_raw) if date_raw else ""
        text = _first_nonempty(row, cfg.text_fields)

        final_title = cfg.title_fmt(title, provider) if cfg.title_fmt else title
        if not final_title:
            continue
        slug = li._slug(final_title)
        if date_iso:
            date_tag = date_iso[:7].replace("-", "")
        else:
            # No date field on this domain (e.g. saved Q&A answers) -- a
            # constant fallback tag here would let two rows with the same
            # title but different content (a repeated interview question
            # answered differently at different jobs) collide on one
            # filename, and write_md's merge-on-rewrite silently keeps only
            # the first body. A content hash keeps genuinely identical rows
            # deduped (same tag as before) while separating real variants.
            # out_dir.name salts the hash so identical content in two
            # different domains (confirmed real: the same saved Q&A answer
            # shows up in both JOB_APPLICANT_SAVED_ANSWERS and
            # TALENT_QUESTION_SAVED_RESPONSE) can't collide on the same
            # filename stem -- build_graph.py's node_id is derived from the
            # filename alone, not the full path, so a cross-domain filename
            # collision would silently drop one of the two nodes.
            date_tag = hashlib.md5(f"{out_dir.name}|{final_title}|{provider}|{text}".encode()).hexdigest()[:8]
        path = out_dir / f"{slug}_{date_tag}.md"

        extra_lines = [f"**{k}:** {v}" for k, v in row.items()
                       if k not in used_fields and str(v or "").strip()]
        body = ((text + "\n\n") if text else "") + "\n\n".join(extra_lines)

        short_desc = (text[:200] + "…") if len(text) > 200 else text
        if not short_desc:
            short_desc = f"{final_title}" + (f" — {provider}" if provider and provider not in final_title else "")

        tag_text = " ".join([final_title, text])
        tags = infer_tags(tag_text)
        if cfg.role_tag_field:
            tags = sorted(set(tags) | set(infer_role_tags(row.get(cfg.role_tag_field, ""))))

        fm = {
            "title": final_title,
            "type": cfg.entry_type,
            "provider": provider or "LinkedIn",
            "date": date_iso or None,
            "description": short_desc,
            "domain_tags": tags,
        }
        li.write_md(path, fm, body)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Domains with a row shape that doesn't fit the generic one-entry-per-row model
# ---------------------------------------------------------------------------

def ingest_profile_and_skills(profile_rows: list[dict], skill_rows: list[dict]) -> int:
    """PROFILE (1 row) + SKILLS (~N rows) -> corpus/linkedin/profile.md.

    Deliberately whitelist-only: PROFILE's real API response also includes
    Birth Date, Address, Zip Code, Maiden Name, Instant Messengers -- none of
    which the old CSV path ever surfaced, and this file lands in the PUBLIC
    export allowlist. Those fields are never read here, not just dropped
    later.
    """
    p = profile_rows[0] if profile_rows else {}
    name = f"{p.get('First Name', '')} {p.get('Last Name', '')}".strip()
    headline = str(p.get("Headline", "")).strip()
    summary = str(p.get("Summary", "")).strip()
    industry = str(p.get("Industry", "")).strip()
    websites = str(p.get("Websites", "")).strip()

    skills = [str(r.get("Name", "")).strip() for r in skill_rows if str(r.get("Name", "")).strip()]

    path = CORPUS_OUT / "profile.md"
    existed = path.exists()
    if not existed:
        li._NEW_PATHS.append(path)
    if li.DRY_RUN:
        return 1

    tags = infer_tags(headline + " " + summary + " " + " ".join(skills[:30]))
    fm = {
        "title": name or "LinkedIn Profile",
        "type": "profile",
        "provider": "LinkedIn",
        "date": None,
        "description": headline or summary[:200],
        "domain_tags": tags,
    }
    import yaml
    fm_yaml = yaml.dump(fm, allow_unicode=True, sort_keys=True, default_flow_style=False)
    body = f"## {name}\n\n**Headline:** {headline}\n\n**Industry:** {industry}\n\n"
    if websites:
        body += f"**Websites:** {websites}\n\n"
    if summary:
        body += f"## Summary\n\n{summary}\n\n"
    if skills:
        body += "## Skills\n\n" + "\n".join(f"- {s}" for s in skills)
    path.write_text(f"---\n{fm_yaml}---\n\n{body}\n", encoding="utf-8")
    return 1


def ingest_learning(rows: list[dict], out_dir: Path) -> int:
    """LEARNING -> corpus/linkedin/learning/ (completed items only)."""
    count = 0
    for r in rows:
        completed_at = str(r.get("Content Completed At (if completed)", "")).strip()
        if not completed_at or completed_at.upper() == "N/A":
            continue
        title = str(r.get("Content Title", "")).strip()
        if not title:
            continue
        desc_raw = str(r.get("Content Description", "")).strip()
        ctype = str(r.get("Content Type", "Course")).strip()
        date_iso = li.date_to_iso(completed_at)
        slug = li._slug(title)
        date_tag = date_iso[:7].replace("-", "") if date_iso else "0000"
        path = out_dir / f"{slug}_{date_tag}.md"

        short_desc = (desc_raw[:200] + "…") if len(desc_raw) > 200 else desc_raw
        tags = infer_tags(title + " " + desc_raw[:200])

        fm = {
            "title": title,
            "type": ctype.lower() if ctype else "course",
            "provider": "LinkedIn Learning",
            "date": date_iso or None,
            "description": short_desc,
            "domain_tags": tags,
        }
        li.write_md(path, fm, desc_raw)
        count += 1
    return count


def ingest_job_applications(rows: list[dict], app_out: Path, interest_out: Path) -> tuple[int, int]:
    """JOB_APPLICATIONS -> job_applications/ (active, June 2026+) +
    career_interests/ (historical signal only). Same cutoff/shape as the CSV
    path's ingest_job_applications(), fed from API rows instead.

    Contact Email / Contact Phone Number are dropped outright (a recruiter's
    or ATS's contact details, not the user's own data, no evidentiary value
    for a skills graph -- a new PII surface the old CSV export never had).
    """
    active_count = interest_count = 0
    for r in rows:
        job_title = str(r.get("Job Title", "")).strip()
        company = str(r.get("Company Name", "")).strip()
        if not job_title:
            continue
        date_iso = li.date_to_iso(str(r.get("Application Date", "")).strip())
        url = str(r.get("Job Url", "")).strip()
        slug = li._slug(f"{job_title}_{company}")
        date_tag = date_iso[:7].replace("-", "") if date_iso else "0000"

        if date_iso >= ACTIVE_SEARCH_CUTOFF:
            qa = li._parse_qa(str(r.get("Question And Answers", "")).strip())
            exp_lines = [f"- {k}: {v}" for k, v in qa.items()
                         if "year" in k.lower() and v.strip().lstrip("-").isdigit()]
            body = f"**Role:** {job_title} at {company}\n"
            if date_iso:
                body += f"**Applied:** {date_iso}\n"
            if url:
                body += f"**URL:** {url}\n"
            if exp_lines:
                body += "\n**Experience indicated:**\n" + "\n".join(exp_lines)
            tags = infer_tags(job_title + " " + company + " " + " ".join(
                f"{k} {v}" for k, v in qa.items() if "year" in k.lower()))
            fm = {
                "title": f"{job_title} at {company}", "type": "job_application",
                "provider": company or "LinkedIn", "date": date_iso or None,
                "description": f"Applied for {job_title} at {company}", "domain_tags": tags,
            }
            li.write_md(app_out / f"{slug}_{date_tag}.md", fm, body)
            active_count += 1
        else:
            tags = infer_tags(job_title + " " + company)
            fm = {
                "title": f"{job_title} at {company}", "type": "career_interest",
                "provider": company or "LinkedIn", "date": date_iso or None,
                "description": f"Previously applied for {job_title} at {company}", "domain_tags": tags,
            }
            li.write_md(interest_out / f"{slug}_{date_tag}.md", fm)
            interest_count += 1
    return active_count, interest_count


_ALERT_KV_RE = re.compile(r"(\w+)=([^,{}]+(?:\{[^{}]*\}[^,{}]*)*)")


def _parse_java_map_string(raw: str) -> dict[str, str]:
    """Best-effort parse of LinkedIn's Java-map-toString() fields
    ('{keywords=X, frequency=Y}') -- not JSON, unquoted keys, '=' not ':'.
    Doesn't attempt full nested-structure parsing; grabs top-level key=value
    pairs, which is all the useful signal (keywords, location, frequency)."""
    inner = raw.strip().strip("{}")
    return {m.group(1): m.group(2).strip() for m in _ALERT_KV_RE.finditer(inner)}


def ingest_saved_job_alerts(rows: list[dict], out_dir: Path) -> int:
    """SAVED_JOB_ALERTS -> corpus/linkedin/saved_job_alerts/ (private)."""
    count = 0
    for i, r in enumerate(rows):
        query_ctx = _parse_java_map_string(str(r.get("QUERY_CONTEXT", "")))
        keywords = query_ctx.get("keywords", "").strip()
        if not keywords:
            continue
        search_id = str(r.get("SAVED_SEARCH_ID", "")).strip() or str(i)
        slug = li._slug(f"job_alert_{keywords}_{search_id}")
        path = out_dir / f"{slug}_0000.md"

        alert_params = _parse_java_map_string(str(r.get("ALERT_PARAMETERS", "")))
        body = f"**Search:** {keywords}\n"
        if alert_params:
            body += f"\n**Alert settings:** {alert_params}\n"

        fm = {
            "title": f"Job alert: {keywords}", "type": "saved_job_alert",
            "provider": "LinkedIn", "date": None,
            "description": f"Standing job search alert for '{keywords}'",
            "domain_tags": infer_tags(keywords),
        }
        li.write_md(path, fm, body)
        count += 1
    return count


def ingest_job_seeker_preferences(rows: list[dict], out_dir: Path) -> int:
    """JOB_SEEKER_PREFERENCES -> corpus/linkedin/job_seeker_preferences/ (private, single record)."""
    if not rows:
        return 0
    r = rows[0]
    path = out_dir / "job_seeker_preferences.md"
    body_lines = [f"**{k}:** {v}" for k, v in r.items() if str(v or "").strip()]
    body = "\n\n".join(body_lines)
    intro = str(r.get("Introduction Statement", "")).strip()

    fm = {
        "title": "Job Seeker Preferences", "type": "job_seeker_preferences",
        "provider": "LinkedIn", "date": None,
        "description": (intro[:200] if intro else "LinkedIn job-seeking preferences and settings"),
        "domain_tags": infer_tags(intro + " " + str(r.get("Job Titles", ""))),
    }
    li.write_md(path, fm, body)
    return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

ALL_DOMAINS = ["PROFILE", "SKILLS", "LEARNING", "JOB_APPLICATIONS", "SAVED_JOB_ALERTS",
               "JOB_SEEKER_PREFERENCES"] + list(GENERIC_DOMAINS.keys())


def main(domains: list[str] | None, dry_run: bool, json_summary: bool) -> None:
    li.DRY_RUN = dry_run
    li._NEW_PATHS = []

    def _mkdir(p: Path) -> None:
        if not dry_run:
            p.mkdir(parents=True, exist_ok=True)

    try:
        token = load_token()
    except LinkedInAPIError as e:
        print(f"ERROR: {e}")
        if json_summary:
            print("PKH_JSON_SUMMARY: " + json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    # Proactive expiry warning for the weekly-cadence operating model
    # (2026-08-10 audit) -- the token file stores no expires_in/issued-date,
    # and real lifetime is unconfirmed (LinkedIn's docs say 60 days, the
    # Developer Portal UI showed ~1 year, never resolved). 45 days is a
    # conservative margin under the more cautious estimate: a weekly run
    # should warn ahead of a failure, not just hit a 401 with no notice.
    token_age_days = (time.time() - TOKEN_FILE.stat().st_mtime) / 86400
    if token_age_days > 45:
        print(f"WARNING: connector/linkedin_token.json is {token_age_days:.0f} days old. "
              f"Real token lifetime was never confirmed (60 days vs ~1 year, conflicting "
              f"sources) -- if this run starts failing with 401s, re-run the OAuth flow "
              f"(see ~/.claude/plans/look-at-this-lazy-wombat.md).\n")

    wanted = set(domains) if domains else set(ALL_DOMAINS)
    print(f"Fetching from LinkedIn API" + (" [dry run — nothing will be written]" if dry_run else ""))
    summary: dict[str, int] = {}
    total = 0

    def fetch(domain: str) -> list[dict]:
        try:
            rows = fetch_domain(domain, token)
        except LinkedInAPIError as e:
            print(f"  {domain}: ERROR — {e}")
            return []
        return rows

    if "PROFILE" in wanted or "SKILLS" in wanted:
        _mkdir(CORPUS_OUT)
        profile_rows = fetch("PROFILE") if "PROFILE" in wanted else []
        skill_rows = fetch("SKILLS") if "SKILLS" in wanted else []
        n = ingest_profile_and_skills(profile_rows, skill_rows)
        print(f"  Profile+Skills : 1 file, {len(skill_rows)} skills")
        summary["profile"] = n
        summary["skills"] = len(skill_rows)
        total += n

    if "LEARNING" in wanted:
        out = CORPUS_OUT / "learning"
        _mkdir(out)
        rows = fetch("LEARNING")
        n = ingest_learning(rows, out)
        print(f"  Learning       : {n} (completed only, of {len(rows)} total)")
        summary["learning"] = n
        total += n

    if "JOB_APPLICATIONS" in wanted:
        app_out, interest_out = CORPUS_OUT / "job_applications", CORPUS_OUT / "career_interests"
        _mkdir(app_out)
        _mkdir(interest_out)
        rows = fetch("JOB_APPLICATIONS")
        active, historical = ingest_job_applications(rows, app_out, interest_out)
        print(f"  Job Applications (active) : {active}  [{ACTIVE_SEARCH_CUTOFF}+]")
        print(f"  Job Applications (signal) : {historical}  [pre-{ACTIVE_SEARCH_CUTOFF}]")
        summary["job_applications_active"] = active
        summary["job_applications_signal"] = historical
        total += active + historical

    if "SAVED_JOB_ALERTS" in wanted:
        out = CORPUS_OUT / "saved_job_alerts"
        _mkdir(out)
        rows = fetch("SAVED_JOB_ALERTS")
        n = ingest_saved_job_alerts(rows, out)
        print(f"  Saved Job Alerts : {n}")
        summary["saved_job_alerts"] = n
        total += n

    if "JOB_SEEKER_PREFERENCES" in wanted:
        out = CORPUS_OUT / "job_seeker_preferences"
        _mkdir(out)
        rows = fetch("JOB_SEEKER_PREFERENCES")
        n = ingest_job_seeker_preferences(rows, out)
        print(f"  Job Seeker Preferences : {n}")
        summary["job_seeker_preferences"] = n
        total += n

    for domain, cfg in GENERIC_DOMAINS.items():
        if domain not in wanted:
            continue
        out = CORPUS_OUT / cfg.corpus_subdir
        _mkdir(out)
        rows = fetch(domain)
        n = ingest_generic_domain(domain, cfg, rows, out)
        note = "" if rows else "  (no data — either genuinely empty or field-name config needs verifying)"
        print(f"  {domain:<28}: {n} of {len(rows)} rows [{cfg.tier}]{note}")
        summary[domain.lower()] = n
        total += n

    print(f"\nTotal files written to corpus/linkedin/: {total}"
          + (" [dry run — nothing written]" if dry_run else ""))

    if json_summary:
        print("PKH_JSON_SUMMARY: " + json.dumps({
            "ok": True, "dry_run": dry_run, "sources": summary,
            "total_items": total, "new_items": len(li._NEW_PATHS),
            "updated_items": max(total - len(li._NEW_PATHS), 0),
        }))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Ingest LinkedIn Member Data Portability API into corpus/.")
    p.add_argument("--dry-run", action="store_true",
                    help="Report what would be written without writing anything")
    p.add_argument("--json-summary", action="store_true",
                    help="Print a final PKH_JSON_SUMMARY: {...} line for programmatic callers")
    p.add_argument("--domains", default="",
                    help="Comma-separated subset of domains to run (default: all)")
    args = p.parse_args()
    domains = [d.strip().upper() for d in args.domains.split(",") if d.strip()] or None
    main(domains, dry_run=args.dry_run, json_summary=args.json_summary)
