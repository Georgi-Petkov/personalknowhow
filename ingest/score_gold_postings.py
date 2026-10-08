#!/usr/bin/env python3
"""Score new AS3 postings (workspace.datajobs_gold.fct_postings_for_evaluation)
against PKH's own real, demonstrated-evidence corpus -- grounding "is this
relevant to me" in the candidate's actual body of work instead of a generic
LLM vibe-check.

Two earlier approaches were tried and empirically failed, both against the
real 404-posting Gold table -- kept here as history, not hypothetically:

1. laya-mlx scored JD relevance directly (0-2 scale) with zero grounding in
   this candidate's actual skills. Saturated near ceiling for almost
   everything: 389/405 postings scored ~2/2, including a Lean Specialist and
   a Logistics/Waste Management coordinator.
2. A PKH-grounded SKILLS-vocabulary coverage score (does the candidate have
   ANY demonstrated evidence for each skill term the JD happens to mention)
   ALSO saturated: 232/404 scored, virtually all at 1.00 -- including
   "Business HR Partner" (mentions Spark/PySpark once). Root cause: this
   candidate has broad-enough coverage across the 36-term SKILLS vocabulary
   that "do you know skill X somewhere in your history" isn't a
   discriminating question for them specifically -- grounded, but on the
   wrong axis (knowledge-presence, not role-fit).

What actually works, validated live against the same 404 postings: WHOLE-
DOCUMENT semantic similarity. Embed the full JD text and compare it against
every entry in the demonstrated-evidence corpus (local sentence-transformers,
same mechanism ingest/query_local.py already uses -- no Cloudflare account,
no new cloud dependency), averaging the top-K most similar corpus entries.
This measures "does this posting's overall content resemble my body of
work," not "does it mention a keyword I also know" -- and it separates real
roles cleanly: top-scoring real results were Data Engineer/Data
Scientist/BI roles (0.72-0.74), bottom-scoring were Talent/Rewards/Business
Continuity/Policy/Business-Development roles (0.58-0.61), no overlap.

pkh_coverage_score (skill-presence) is kept as a secondary diagnostic field
only -- informative to see which SKILLS terms were detected, not trustworthy
as a standalone score. pkh_semantic_fit_score is the primary signal.

Language: Danish-language postings (Gold's language_guess column) are
EXCLUDED outright, not just deprioritized -- explicit user policy: a JD
written in Danish is the clearest possible signal the role expects Danish
fluency, so it's never a candidate to apply to regardless of content fit.
This is deterministic on the posting's own actual language, not a soft
relevance proxy -- it is not the kind of heuristic keep/drop filter
DataJobs' stg_job_postings.sql history warns against (that one guessed
relevance from a title regex; this one reads a fact already computed from
the JD's real text). No embedding is spent scoring an excluded posting.
Excluded postings stay in data/job_relevance_scores.json (audit trail,
excluded=True + reason) rather than disappearing silently. As a secondary
note: pkh_semantic_fit_score also uses bge-small-en-v1.5, an English-only
embedding model, so even if this exclusion policy changes later, a Danish
JD's similarity score would not be a fair comparison against an English
one without swapping in a multilingual model first.

ADVISORY ONLY, same discipline as job_category/laya_category in DataJobs:
this never drops a posting. It ranks new Gold postings so job_fit_agent.py
(or you, by hand) can check the highest-scoring ones first, instead of
working through every posting in scrape order. See
DataJobs/dbt/datajobs/models/staging/stg_job_postings.sql's own history
(2026-09-21) for why a hard filter here would repeat a real mistake.

State: data/job_relevance_scores.json (gitignored, keyed by external_job_id)
so re-running only scores genuinely new postings.

laya_category/laya_relevance_score columns are queried opportunistically --
they only exist in Gold once DataJobs/ingest/laya_enrich_postings.py's output
has been uploaded and dbt has rebuilt Silver/Gold; this script degrades
gracefully (fills None) if those columns aren't live yet.

Usage:
  set -a; source ../DataJobs/dbt/datajobs/.env; set +a
  python3 ingest/score_gold_postings.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

sys.path.insert(0, str(Path(__file__).parent))
from analyze_job_postings import SKILLS  # noqa: E402
from build_public_export import build_entries  # noqa: E402
from common import load_semantic_corpus, load_skill_embeddings, semantic_coverage  # noqa: E402
import query_local as ql  # noqa: E402

ROOT = Path(__file__).parent.parent
SCORES_FILE = ROOT / "data" / "job_relevance_scores.json"
TOP_K = 10  # number of nearest corpus entries averaged into pkh_semantic_fit_score

DATABRICKS_HOST = os.environ["DATABRICKS_HOST"]
HTTP_PATH = os.environ["DATABRICKS_HTTP_PATH"]
DATABRICKS_TOKEN = os.environ["DATABRICKS_TOKEN"]

FULL_QUERY = """
SELECT external_job_id, title, employer, description, url, job_category,
       laya_category, laya_relevance_score, language_guess, scraped_date
FROM workspace.datajobs_gold.fct_postings_for_evaluation
"""
BASIC_QUERY = """
SELECT external_job_id, title, employer, description, url, job_category,
       language_guess, scraped_date
FROM workspace.datajobs_gold.fct_postings_for_evaluation
"""


def fetch_rows(engine):
    """Prefers the laya-enriched columns; falls back cleanly if dbt hasn't
    been rebuilt with them yet (see module docstring)."""
    try:
        with engine.connect() as conn:
            return conn.execute(text(FULL_QUERY)).fetchall(), True
    except Exception:
        with engine.connect() as conn:
            return conn.execute(text(BASIC_QUERY)).fetchall(), False


def matched_skills(posting_text: str) -> set[str]:
    return {name for name, pattern in SKILLS if re.search(pattern, posting_text, re.IGNORECASE)}


def load_scores() -> dict:
    if SCORES_FILE.exists():
        return json.loads(SCORES_FILE.read_text(encoding="utf-8"))
    return {}


def save_scores(scores: dict) -> None:
    SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCORES_FILE.write_text(json.dumps(scores, indent=2, default=str), encoding="utf-8")


def diagnostic_skill_coverage(description: str, skill_vectors: dict, corpus_entries: list,
                               corpus_vectors: dict) -> dict:
    """Secondary/diagnostic only -- known to saturate near 1.0 for a
    broad-skilled candidate (see module docstring). Kept for visibility into
    which SKILLS terms were detected, not as a trustworthy standalone score."""
    jd_skills = matched_skills(description or "")
    if not jd_skills:
        return {"pkh_coverage_score": None, "detected_skills": []}
    total = 0.0
    for skill in jd_skills:
        cov = semantic_coverage(skill, skill_vectors, corpus_entries, corpus_vectors)
        total += 1.0 if cov["known"] > 0 else (0.5 if cov["peripheral"] > 0 else 0.0)
    return {"pkh_coverage_score": round(total / len(jd_skills), 3), "detected_skills": sorted(jd_skills)}


def semantic_fit(jd_text: str, model, semantic_corpus_vectors: dict) -> float:
    jd_vec = model.encode(jd_text, normalize_embeddings=True).tolist()
    sims = sorted(ql.cosine_sim(jd_vec, v) for v in semantic_corpus_vectors.values())
    top = sims[-TOP_K:]
    return round(sum(top) / len(top), 4)


def main() -> None:
    print("Loading demonstrated-evidence corpus + skill-coverage data (diagnostic tier) ...")
    corpus_entries, corpus_vectors = load_semantic_corpus()
    skill_vectors = load_skill_embeddings()
    if not corpus_entries or not skill_vectors:
        print(
            "WARNING: skill-coverage diagnostic unavailable (missing "
            "mcp-private/private_embeddings.json or data/skill_embeddings.json) -- "
            "proceeding with semantic fit only.",
            file=sys.stderr,
        )

    print("Loading local embedding model for whole-document semantic fit (primary signal) ...")
    semantic_entries = build_entries()
    model = ql.SentenceTransformer(ql.MODEL_NAME)
    semantic_corpus_vectors = ql.get_entry_vectors(model, semantic_entries)

    url = URL.create(
        "databricks", username="token", password=DATABRICKS_TOKEN,
        host=DATABRICKS_HOST, query={"http_path": HTTP_PATH},
    )
    engine = create_engine(url)
    rows, has_laya_columns = fetch_rows(engine)
    print(f"{len(rows)} postings pulled from Gold"
          + ("" if has_laya_columns else " (laya_category not live in Gold yet -- see module docstring)"))

    scores = load_scores()
    new_count = backfilled_count = excluded_count = 0
    for r in rows:
        job_id = str(r.external_job_id)
        language_guess = getattr(r, "language_guess", None)

        if job_id in scores:
            # language_guess/excluded were added in separate later runs -- each
            # backfilled independently on existing entries, never requiring a
            # full re-score.
            existing = scores[job_id]
            changed = False
            if "language_guess" not in existing:
                existing["language_guess"] = language_guess
                changed = True
            if "excluded" not in existing:
                existing["excluded"] = existing.get("language_guess") == "danish"
                if existing["excluded"]:
                    existing["exclusion_reason"] = "JD is in Danish -- explicit user policy, never applying to a Danish-language posting"
                changed = True
            if changed:
                backfilled_count += 1
            continue

        if language_guess == "danish":
            # Explicit user policy: a Danish-language JD is the clearest possible
            # signal the role expects Danish fluency -- excluded outright, not just
            # deprioritized. Deterministic on the posting's own actual language,
            # unlike the relevance-score proxies that turned out unreliable -- this
            # is not the kind of soft heuristic filter DataJobs' history warns
            # against. No embedding spent on postings that will never be considered.
            scores[job_id] = {
                "title": r.title,
                "employer": r.employer,
                "url": r.url,
                "language_guess": language_guess,
                "scraped_date": str(r.scraped_date),
                "excluded": True,
                "exclusion_reason": "JD is in Danish -- explicit user policy, never applying to a Danish-language posting",
            }
            excluded_count += 1
            continue

        jd_text = f"{r.title}\n\n{r.description or ''}"
        fit_score = semantic_fit(jd_text, model, semantic_corpus_vectors)
        diag = diagnostic_skill_coverage(r.description, skill_vectors, corpus_entries, corpus_vectors)

        scores[job_id] = {
            "title": r.title,
            "employer": r.employer,
            "url": r.url,
            "job_category": r.job_category,
            "laya_category": getattr(r, "laya_category", None),
            "laya_relevance_score": getattr(r, "laya_relevance_score", None),
            "language_guess": language_guess,
            "scraped_date": str(r.scraped_date),
            "excluded": False,
            "pkh_semantic_fit_score": fit_score,
            **diag,
        }
        new_count += 1

    save_scores(scores)

    considered = [s for s in scores.values() if not s.get("excluded")]
    excluded = [s for s in scores.values() if s.get("excluded")]
    ranked = sorted(considered, key=lambda s: -s["pkh_semantic_fit_score"])

    print(f"\n{new_count} new posting(s) scored this run ({excluded_count} newly excluded as "
          f"Danish-JD, {backfilled_count} existing entries backfilled), {len(scores)} total "
          f"tracked in {SCORES_FILE.relative_to(ROOT)}\n")

    print(f"Ranked by pkh_semantic_fit_score ({len(ranked)} postings, highest first -- check "
          f"these with job_fit_agent.py first):")
    for s in ranked:
        print(f"  [{s['pkh_semantic_fit_score']:.3f}] {s['title']} @ {s['employer']}")

    print(f"\nExcluded ({len(excluded)}) -- Danish-language JD, never considered "
          f"(explicit user policy, not a soft filter):")
    for s in excluded:
        print(f"  {s['title']} @ {s['employer']}")


if __name__ == "__main__":
    main()
