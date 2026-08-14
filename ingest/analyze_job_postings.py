#!/usr/bin/env python3
"""
Career Agent: structured market-side analysis over scraped LinkedIn job-posting
snapshots (data/linkedin_jobs_scan/<date>.json, one file per monthly scrape).

Read-only analysis -- no scoring/ranking judgment baked in here, mirrors the
pattern in recommend_courses.py (which does the same for the DataCamp catalog
side). Prints a structured report; the actual "what should I actually do"
judgment call happens by pasting this output into a Claude Code chat.

What this does:
- Skill-frequency by category, annotated with known PKH coverage (graph.json).
- Structured salary parsing (ingest/salary_parser.py) -- all figures shown as
  approximate annual DKK, converting USD/EUR/etc where needed.
- Skill-to-salary correlation among disclosed-salary postings only (always
  caveated -- disclosure is rare, so n is small).
- Month-over-month skill-frequency trend, once a second snapshot exists.
- A final SKILL GAP PRIORITIES + NEXT STEPS section for the Claude Code chat step.

Market/job data is environmental, not a personal accomplishment -- this script
only ever reads graph.json, it never writes to corpus/ or graph.json.

Usage:
  python ingest/analyze_job_postings.py
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (  # noqa: E402
    known_language_codes, load_semantic_corpus, load_skill_embeddings, semantic_coverage,
)
from salary_parser import parse_salary, to_annual_dkk  # noqa: E402

try:
    from langdetect import DetectorFactory, LangDetectException, detect
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "langdetect", "-q",
                   "--break-system-packages"], check=True)
    from langdetect import DetectorFactory, LangDetectException, detect
DetectorFactory.seed = 0  # deterministic detection across runs

ROOT = Path(__file__).parent.parent
SNAPSHOT_DIR = ROOT / "data" / "linkedin_jobs_scan"

# (display name, regex) -- word-boundary, case-insensitive
SKILLS: list[tuple[str, str]] = [
    ("Python", r"\bpython\b"),
    ("SQL", r"\bsql\b"),
    ("Databricks", r"\bdatabricks\b"),
    ("Power BI / DAX", r"\bpower\s*bi\b|\bdax\b"),
    ("dbt", r"\bdbt\b"),
    ("Snowflake", r"\bsnowflake\b"),
    ("Azure", r"\bazure\b"),
    ("AWS", r"\baws\b|\bamazon web services\b"),
    ("Google Cloud", r"\bgcp\b|\bgoogle cloud\b|\bbigquery\b"),
    ("Microsoft Fabric", r"\bmicrosoft fabric\b|\bfabric\b"),
    ("Airflow", r"\bairflow\b"),
    ("Kafka", r"\bkafka\b"),
    ("Terraform / IaC", r"\bterraform\b|\binfrastructure as code\b"),
    ("Kubernetes", r"\bkubernetes\b|\bk8s\b"),
    ("Docker", r"\bdocker\b"),
    ("CI/CD", r"\bci/cd\b|\bcontinuous integration\b|\bcontinuous deployment\b"),
    ("Git", r"\bgit\b"),
    ("Excel", r"\bexcel\b"),
    ("Tableau", r"\btableau\b"),
    ("R (language)", r"\br programming\b|\busing r\b|\bin r\b"),
    ("Scala", r"\bscala\b"),
    ("Java", r"\bjava\b"),
    ("Spark / PySpark", r"\bspark\b|\bpyspark\b"),
    ("ETL/ELT connectors (Fivetran/Airbyte/Weld)", r"\bfivetran\b|\bairbyte\b|\bweld\b"),
    ("Statistics / forecasting", r"\bforecast\w*\b|\btime series\b|\bstatistical\b"),
    ("OpenAI API", r"\bopenai\b"),
    ("LangChain", r"\blangchain\b"),
    ("Claude / Anthropic", r"\bclaude\b|\banthropic\b"),
    ("ChatGPT", r"\bchatgpt\b"),
    ("Gemini", r"\bgemini\b"),
    ("LLM / Generative AI", r"\bllm\b|\blarge language model\b|\bgenerative ai\b|\bgen ai\b"),
    ("RAG / Vector DB", r"\brag\b|\bretrieval.augmented\b|\bvector database\b|\bpinecone\b|\bembeddings?\b"),
    ("MCP", r"\bmodel context protocol\b|\bmcp\b"),
    ("AI Agents", r"\bai agent\b|\bagentic\b"),
    ("Prompt engineering", r"\bprompt engineering\b|\bprompting\b"),
    ("Data governance/quality", r"\bdata governance\b|\bdata quality\b"),
]

# Coverage used to be looked up via a hand-maintained SKILLS-name -> TAG_PATTERNS
# slug map -- structurally binary (a course either has the exact tag or it
# doesn't) and required a manual mapping entry (often None) for every new
# skill. Replaced 2026-08-10 with semantic_coverage() (common.py): compares
# each skill name's embedding against the corpus's real embeddings (same
# model/vectors the deployed MCP search already uses), so a course that
# clearly touches a skill without using its exact phrase still shows up --
# graded (known/peripheral), not a false binary. No per-skill mapping table
# needed any more; every name in SKILLS is looked up directly.


def list_snapshots() -> list[Path]:
    return sorted(SNAPSHOT_DIR.glob("*.json"))


def load_snapshot(path: Path) -> list[dict]:
    postings = json.loads(path.read_text(encoding="utf-8"))
    for p in postings:
        p["_snapshot_date"] = path.stem
    return postings


def detect_posting_language(posting: dict) -> str | None:
    """Best-effort ISO 639-1 code for a posting's text, or None if too short
    to detect reliably. Description is preferred over cardText -- more text,
    less likely to be dominated by a company name/location in another
    language than the posting itself is written in."""
    text = (posting.get("description") or "").strip() or (posting.get("cardText") or "").strip()
    if len(text) < 30:
        return None
    try:
        return detect(text[:2000])
    except LangDetectException:
        return None


def filter_by_language(postings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Flags postings written in a language the user hasn't listed on
    LinkedIn (LANGUAGES API domain) as irrelevant and excludes them from
    everything downstream -- a posting in a language the user doesn't speak
    isn't a real candidate opportunity, so it shouldn't influence skill/
    salary signal either.

    If known_language_codes() is empty (LANGUAGES domain not yet populated
    -- see corpus/linkedin/languages/), filtering is skipped entirely rather
    than guessing at a language list: better to show everything than to
    silently drop postings against an assumption nobody confirmed.
    """
    known_codes = known_language_codes()
    if not known_codes:
        return postings, []
    relevant, flagged = [], []
    for p in postings:
        lang = detect_posting_language(p)
        p["_detected_language"] = lang
        if lang is None or lang in known_codes:
            relevant.append(p)
        else:
            flagged.append(p)
    return relevant, flagged


def print_language_filter(relevant: list[dict], flagged: list[dict], known_codes: set[str]) -> None:
    print("=== LANGUAGE FILTER ===")
    if not known_codes:
        print("No languages found in corpus/linkedin/languages/ yet (LinkedIn LANGUAGES "
              "domain empty) -- skipping language filtering, showing all postings. Add "
              "Languages on your LinkedIn profile and re-run ingest/linkedin_api_ingest.py "
              "to enable this.")
        return
    print(f"Known languages (from LinkedIn): {sorted(known_codes)}")
    if not flagged:
        print(f"All {len(relevant)} postings are in a known language (or too short to detect).")
        return
    print(f"{len(flagged)} of {len(relevant) + len(flagged)} postings flagged as irrelevant "
          f"(language not in known list) and excluded from analysis below:")
    for p in flagged:
        snippet = (p.get("cardText") or "")[:80]
        print(f"  [{p.get('category')}] detected={p.get('_detected_language')!r} — {snippet}")


def skill_frequency(postings: list[dict]) -> dict[str, tuple[Counter, int]]:
    """Returns {category: (Counter(skill_name -> count), n_postings_in_category)}."""
    by_category: dict[str, list[dict]] = defaultdict(list)
    for p in postings:
        by_category[p.get("category", "Unknown")].append(p)
    result = {}
    for cat, items in by_category.items():
        counts: Counter = Counter()
        for p in items:
            text = (p.get("description") or "") + " " + (p.get("cardText") or "")
            for name, pattern in SKILLS:
                if re.search(pattern, text, re.IGNORECASE):
                    counts[name] += 1
        result[cat] = (counts, len(items))
    return result


def _coverage_note(name: str, semantic_ctx: dict) -> str:
    skill_vectors, corpus_entries, corpus_vectors = semantic_ctx["skill_vectors"], \
        semantic_ctx["corpus_entries"], semantic_ctx["corpus_vectors"]
    result = semantic_coverage(name, skill_vectors, corpus_entries, corpus_vectors)
    if not result["available"]:
        return "no cached embedding for this skill -- see PKH/CLAUDE.md to regenerate data/skill_embeddings.json"
    note = f"known: {result['known']}"
    if result["peripheral"]:
        note += f", peripheral: {result['peripheral']}"
    if result["top"]:
        top_str = ", ".join(f"{label!r} {score}" for label, score in result["top"])
        note += f" (top: {top_str})"
    return note


def print_skill_frequency(freq: dict[str, tuple[Counter, int]], semantic_ctx: dict) -> None:
    print("=== SKILL FREQUENCY BY CATEGORY (with known PKH coverage, semantic) ===")
    for cat, (counts, n) in freq.items():
        print(f"\n--- {cat} (n={n}) ---")
        for name, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            if c == 0:
                continue
            print(f"  {name}: {c}/{n} ({round(100*c/n)}%) | {_coverage_note(name, semantic_ctx)}")


def parse_salaries(postings: list[dict]) -> list[dict]:
    """Attach a parsed salary to postings where one is found; return only those."""
    salaried = []
    for p in postings:
        text = (p.get("cardText") or "") + " " + (p.get("description") or "")
        parsed = parse_salary(text)
        if parsed:
            p["_salary"] = parsed
            salaried.append(p)
    return salaried


def print_salaries(salaried: list[dict], total: int) -> None:
    print("\n=== SALARY FINDINGS (all figures converted to approx. annual DKK) ===")
    pct = round(100 * len(salaried) / total) if total else 0
    print(f"{len(salaried)} of {total} postings disclose a salary ({pct}%).")
    for p in salaried:
        parsed = p["_salary"]
        dkk_lo, dkk_hi = to_annual_dkk(parsed)
        fx_note = "" if parsed["currency"] == "DKK" else " (FX-converted, approx.)"
        period_note = "" if parsed["period_confidence"] == "explicit" else " [period inferred]"
        lo_s = f"{dkk_lo:,.0f}" if dkk_lo is not None else "?"
        hi_s = f"{dkk_hi:,.0f}" if dkk_hi is not None else "?"
        print(f"  [{p.get('category')}] {lo_s}-{hi_s} DKK/yr{fx_note}{period_note} "
              f"(raw: {parsed['salary_raw']!r})")


def compute_salary_correlation(salaried: list[dict]) -> list[tuple[str, int, float, int, float]]:
    """For each skill with n>=2 on both sides, (name, n_with, avg_with, n_without,
    avg_without) using annual-DKK midpoints. Skills below the n>=2/n>=2 bar are
    omitted entirely -- shown with n=1 vs n=6 would look precise but isn't.
    """
    midpoints: list[tuple[dict, float]] = []
    for p in salaried:
        lo, hi = to_annual_dkk(p["_salary"])
        vals = [v for v in (lo, hi) if v is not None]
        if vals:
            midpoints.append((p, sum(vals) / len(vals)))

    rows = []
    for name, pattern in SKILLS:
        with_skill, without_skill = [], []
        for p, mid in midpoints:
            text = (p.get("description") or "") + " " + (p.get("cardText") or "")
            (with_skill if re.search(pattern, text, re.IGNORECASE) else without_skill).append(mid)
        if len(with_skill) >= 2 and len(without_skill) >= 2:
            rows.append((name, len(with_skill), sum(with_skill) / len(with_skill),
                         len(without_skill), sum(without_skill) / len(without_skill)))
    return rows


def print_salary_correlation(salaried: list[dict], rows: list) -> None:
    print("\n=== SKILL-TO-SALARY CORRELATION (annual DKK midpoints) ===")
    print(f"NOTE: only {len(salaried)} postings disclose salary. Any correlation below is "
          "directional at best, not statistically meaningful -- treat as a lead to "
          "investigate, not a conclusion.")
    if not rows:
        print(f"  No skill met the n>=2-vs-n>=2 bar out of {len(SKILLS)} evaluated -- "
              "sample too small.")
        return
    print(f"  {len(rows)} of {len(SKILLS)} skills met the n>=2-vs-n>=2 bar:")
    for name, n_with, avg_with, n_without, avg_without in sorted(rows, key=lambda r: -(r[2] - r[4])):
        delta = avg_with - avg_without
        sign = "+" if delta >= 0 else ""
        print(f"  {name}: with (n={n_with}, avg {avg_with:,.0f} DKK) vs without "
              f"(n={n_without}, avg {avg_without:,.0f} DKK) -- {sign}{delta:,.0f} DKK, "
              "n too small for significance")


def print_trends(freq_latest: dict, freq_prev: dict, latest_date: str, prev_date: str) -> None:
    print(f"\n=== TREND ({prev_date} -> {latest_date}) ===")
    moves = []
    for cat in freq_latest:
        counts_latest, n_latest = freq_latest[cat]
        counts_prev, n_prev = freq_prev.get(cat, (Counter(), 0))
        for skill in set(counts_latest) | set(counts_prev):
            c_latest, c_prev = counts_latest.get(skill, 0), counts_prev.get(skill, 0)
            if c_latest == 0 and c_prev == 0:
                continue
            pct_latest = 100 * c_latest / n_latest if n_latest else 0
            pct_prev = 100 * c_prev / n_prev if n_prev else 0
            moves.append((cat, skill, pct_prev, pct_latest, pct_latest - pct_prev))
    moves.sort(key=lambda m: -abs(m[4]))
    for cat, skill, pct_prev, pct_latest, delta in moves:
        direction = "steady" if abs(delta) < 5 else ("rising" if delta > 0 else "falling")
        sign = "+" if delta >= 0 else ""
        print(f"  {cat} -- {skill}: {round(pct_prev)}% -> {round(pct_latest)}% "
              f"({direction}, {sign}{round(delta)}pp)")


def print_gap_priorities(freq: dict, semantic_ctx: dict, correlation_rows: list) -> None:
    skill_vectors, corpus_entries, corpus_vectors = semantic_ctx["skill_vectors"], \
        semantic_ctx["corpus_entries"], semantic_ctx["corpus_vectors"]
    salary_notes = {r[0]: f"{'+' if r[2]-r[4]>=0 else ''}{r[2]-r[4]:,.0f} DKK signal (n too small)"
                     for r in correlation_rows}
    print("\n=== SKILL GAP PRIORITIES (market demand x known coverage, semantic) ===")
    for cat, (counts, n) in freq.items():
        print(f"\n--- {cat} ---")
        rows = []
        for name, c in counts.items():
            if c == 0:
                continue
            result = semantic_coverage(name, skill_vectors, corpus_entries, corpus_vectors)
            rows.append((name, 100 * c / n, result))
        # priority: highest demand first, then least covered (known, then peripheral)
        rows.sort(key=lambda r: (-r[1], r[2]["known"], r[2]["peripheral"]))
        for name, pct, result in rows:
            coverage_note = _coverage_note(name, semantic_ctx)
            salary_note = salary_notes.get(name, "no salary signal")
            print(f"  {name}: {round(pct)}% demand | {coverage_note} | {salary_note}")


def print_next_steps() -> None:
    print("\n=== NEXT STEPS ===")
    print(
        "Paste this entire output into a Claude Code chat along with your current goal\n"
        "(e.g. \"pivot toward AI Engineering while keeping Data Engineer skills sharp\").\n"
        "Also paste the output of `python ingest/recommend_courses.py` in the same chat -- "
        "that script covers DataCamp catalog gaps; this one covers market demand. Ask it to:\n"
        "1. Cross-reference SKILL GAP PRIORITIES here against recommend_courses.py's\n"
        "   NOT STARTED groupings -- which market-demanded, zero/low-coverage skills also\n"
        "   have a concrete DataCamp candidate available right now?\n"
        "2. Weigh any salary-correlation signal above, but treat it as a lead only --\n"
        "   n is too small for it to be conclusive on its own.\n"
        "3. Recommend a short, specific shortlist, not \"cover everything.\""
    )


def main() -> None:
    snapshots = list_snapshots()
    if not snapshots:
        print(f"ERROR: no snapshots found in {SNAPSHOT_DIR}")
        sys.exit(1)

    latest_path = snapshots[-1]
    raw_postings = load_snapshot(latest_path)
    print(f"SNAPSHOT: {latest_path.stem} -- {len(raw_postings)} postings")
    for cat, c in Counter(p.get("category", "Unknown") for p in raw_postings).items():
        print(f"  {cat}: {c}")
    print()

    known_codes = known_language_codes()
    postings, flagged = filter_by_language(raw_postings)
    print_language_filter(postings, flagged, known_codes)
    print()

    corpus_entries, corpus_vectors = load_semantic_corpus()
    skill_vectors = load_skill_embeddings()
    if not corpus_entries or not skill_vectors:
        print("WARNING: semantic coverage unavailable (missing mcp-private/private_embeddings.json "
              "or data/skill_embeddings.json) -- coverage will show as unavailable for every skill. "
              "See PKH/CLAUDE.md for how to regenerate.\n")
    semantic_ctx = {"skill_vectors": skill_vectors, "corpus_entries": corpus_entries,
                     "corpus_vectors": corpus_vectors}

    freq_latest = skill_frequency(postings)
    print_skill_frequency(freq_latest, semantic_ctx)

    salaried = parse_salaries(postings)
    print_salaries(salaried, len(postings))
    correlation_rows = compute_salary_correlation(salaried)
    print_salary_correlation(salaried, correlation_rows)

    if len(snapshots) >= 2:
        prev_path = snapshots[-2]
        prev_relevant, _ = filter_by_language(load_snapshot(prev_path))
        freq_prev = skill_frequency(prev_relevant)
        print_trends(freq_latest, freq_prev, latest_path.stem, prev_path.stem)
    else:
        print("\n=== TREND ===")
        print(f"Only 1 snapshot on disk ({latest_path.stem}) -- trend reporting activates "
              f"once a second monthly scrape is saved to "
              f"{SNAPSHOT_DIR.relative_to(ROOT)}/.")

    print_gap_priorities(freq_latest, semantic_ctx, correlation_rows)
    print_next_steps()


if __name__ == "__main__":
    main()
