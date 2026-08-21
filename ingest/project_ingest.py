#!/usr/bin/env python3
"""
Ingest hands-on personal-project evidence into corpus/projects/.

Unlike the other ingest scripts, there's no export file here -- the source is
this user's own git repos living as siblings of PKH under ~/Documents/Projects.
Course/cert ingestion (datacamp_ingest.py, edx_ingest.py, ...) only captures
formal learning; real skill built by shipping a project (e.g. months of
hands-on Databricks/dbt work on healthkit-medallion-pipeline) was invisible to
graph.json until this script existed -- confirmed 2026-08-02 when a job-market
gap analysis flagged Databricks/dbt/Azure/CI-CD as "0 known coverage" despite
extensive real project use, purely because corpus/github/ was never populated.

Evidence per repo, all from data already tracked by git (no network calls):
- README* text (first ~3000 chars)
- tracked filenames (git ls-files) -- presence of dbt_project.yml, *.tf,
  Dockerfile, .github/workflows/*.yml etc. is itself a tag signal
- git grep across tracked files for a short list of high-signal keywords
  (kept short deliberately -- this is a coarse pass, not static analysis)
- commit count + first/last commit date, from git log

Usage:
  python ingest/project_ingest.py                 # auto-discover sibling git repos
  python ingest/project_ingest.py --repo ../foo    # ingest one repo explicitly
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from merge import parse_file, merge_frontmatters, write_file  # noqa: E402
from common import infer_tags, _slug  # noqa: E402

ROOT = Path(__file__).parent.parent
PROJECTS_ROOT = ROOT.parent  # ~/Documents/Projects
CORPUS_OUT = ROOT / "corpus" / "projects"

# Directories under ~/Documents/Projects that are never real projects, even if
# they happen to contain a .git (PKH itself is intentionally un-versioned, see
# CLAUDE.md, so it will never match anyway).
EXCLUDE_NAMES = {"PKH", "graphify-out", "logs", ".venv", ".venv_py_3.12"}

# Short, high-signal keyword grep -- deliberately coarse (a git grep pass, not
# real static analysis). Maps grep hit -> domain_tags-compatible evidence text
# fed into infer_tags() alongside the README and filenames.
GREP_KEYWORDS = [
    "pyspark", "databricks", "dbt", "kubernetes", "docker", "terraform",
    "azure", "boto3", "google.cloud", "airflow", "snowflake",
]


def _run(cmd: list[str], cwd: Path) -> str:
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=30
        ).stdout
    except Exception:
        return ""


def discover_repos() -> list[Path]:
    repos = []
    for child in sorted(PROJECTS_ROOT.iterdir()):
        if not child.is_dir() or child.name in EXCLUDE_NAMES:
            continue
        if (child / ".git").exists():
            repos.append(child)
    return repos


# PKH itself has no git (deliberate, personal-data policy -- see CLAUDE.md), so
# discover_repos()'s git-based scan can never see anything built *inside* PKH,
# no matter what's in EXCLUDE_NAMES. Confirmed real gap 2026-08-10: two MCP
# servers (mcp/, mcp-private/) built and deployed the same day showed up as
# "known: 1" in a job-market gap analysis -- same failure class as the
# Databricks/dbt/Azure bug this script was originally built to fix, just in a
# spot the git-only design structurally can't reach. Evidenced here via plain
# filesystem signals (mtimes, file listing, source-file snippets) instead of
# git log/ls-files/grep.
PKH_SUBPROJECT_MARKERS = ("package.json", "wrangler.toml")
PKH_SUBPROJECT_SKIP = {
    "node_modules", "corpus", "data", "cv", "linkedin", "graphify-out", "logs",
    ".git", ".claude", ".wrangler", ".playwright-mcp", "ingest",
}


def discover_pkh_subprojects() -> list[Path]:
    found = []
    for child in sorted(ROOT.iterdir()):
        if not child.is_dir() or child.name in PKH_SUBPROJECT_SKIP:
            continue
        if any((child / marker).exists() for marker in PKH_SUBPROJECT_MARKERS):
            found.append(child)
    return found


def ingest_pkh_subproject(path: Path) -> dict | None:
    files = [
        p for p in path.rglob("*")
        if p.is_file() and "node_modules" not in p.parts
        and ".wrangler" not in p.parts and not p.name.startswith(".")
    ]
    if not files:
        return None

    readme_text = ""
    for candidate in sorted(path.glob("README*")):
        if candidate.is_file():
            readme_text = candidate.read_text(encoding="utf-8", errors="ignore")[:3000]
            break

    src_snippets = []
    pkg = path / "package.json"
    if pkg.exists():
        src_snippets.append(pkg.read_text(encoding="utf-8", errors="ignore")[:500])
    for src in sorted(path.rglob("*.ts"))[:6]:
        if "node_modules" in src.parts:
            continue
        src_snippets.append(src.read_text(encoding="utf-8", errors="ignore")[:1500])

    filenames_blob = " ".join(
        str(p.relative_to(path)).replace("/", " ").replace("_", " ").replace(".", " ")
        for p in files
    )
    # path.name itself (e.g. "mcp", "mcp-private") carries real signal that
    # relative filenames alone don't -- include it explicitly, hyphens
    # normalized to spaces so "mcp-private" still yields a standalone "mcp"
    # token for infer_tags' word-boundary matching.
    name_signal = path.name.replace("-", " ").replace("_", " ")

    import datetime
    mtimes = [p.stat().st_mtime for p in files]
    last_date = datetime.date.fromtimestamp(max(mtimes)).isoformat()
    first_date = datetime.date.fromtimestamp(min(mtimes)).isoformat()

    evidence = " ".join([readme_text, name_signal] + src_snippets + [filenames_blob])
    tags = infer_tags(evidence)

    readme_first_line = next(
        (l.strip("# ").strip() for l in readme_text.splitlines() if l.strip()), ""
    ) if readme_text else ""
    desc = (
        f"Personal project, built inside PKH ({len(files)} files, "
        f"last touched {last_date}). {readme_first_line}"
    ).strip()

    return {
        "title":         path.name,
        "type":          "project",
        "provider":      "personal-project",
        "date":          last_date,
        "description":   desc[:400],
        "domain_tags":   tags,
        "repo_path":     str(path),
        "commits":       None,
        "date_range":    f"{first_date} to {last_date}",
        "grep_evidence": [],
    }


def ingest_repo(repo: Path) -> dict | None:
    commits_raw = _run(["git", "log", "--format=%ad", "--date=short"], repo)
    commit_dates = [l for l in commits_raw.splitlines() if l.strip()]
    if not commit_dates:
        return None  # no commits, nothing to evidence

    last_date = commit_dates[0]
    first_date = commit_dates[-1]
    n_commits = len(commit_dates)

    readme_text = ""
    for candidate in sorted(repo.glob("README*")):
        if candidate.is_file():
            readme_text = candidate.read_text(encoding="utf-8", errors="ignore")[:3000]
            break

    tracked = _run(["git", "ls-files"], repo)
    filenames_blob = tracked.replace("/", " ").replace("_", " ").replace(".", " ")

    # .github/workflows/*.yml etc. is unambiguous structural CI/CD evidence,
    # but tokenizing the path (above) splits it into disconnected words
    # ("github", "workflows", "deploy", "yml") that never reassemble into the
    # literal "github actions" phrase TAG_PATTERNS' ci-cd entry requires --
    # so real, wired-up CI (e.g. MyHealthData's 4 workflow files) went
    # undetected unless the README also happened to say "CI/CD" in prose.
    # Detect the marker files directly instead of relying on that coincidence.
    ci_cd_markers = (
        ".github/workflows/", ".gitlab-ci.yml", ".circleci/config.yml",
        "Jenkinsfile", "azure-pipelines.yml",
    )
    has_ci_cd_config = any(m in tracked for m in ci_cd_markers)

    grep_hits = []
    for kw in GREP_KEYWORDS:
        out = _run(
            ["git", "grep", "-ilw", kw, "--",
             ".", ":!*docs_site*", ":!*/target/*", ":!*.min.js"],
            repo,
        )
        if out.strip():
            grep_hits.append(kw)
    if has_ci_cd_config:
        grep_hits.append("ci-cd-config")

    evidence = " ".join([
        readme_text, filenames_blob, " ".join(grep_hits),
        "github actions ci/cd" if has_ci_cd_config else "",
    ])
    tags = infer_tags(evidence)

    readme_first_line = next(
        (l.strip("# ").strip() for l in readme_text.splitlines() if l.strip()), ""
    )
    desc = (
        f"Personal project ({n_commits} commits, {first_date} to {last_date}). "
        f"{readme_first_line}"
    ).strip()

    return {
        "title":       repo.name,
        "type":        "project",
        "provider":    "personal-project",
        "date":        last_date,
        "description": desc[:400],
        "domain_tags": tags,
        "repo_path":   str(repo),
        "commits":     n_commits,
        "date_range":  f"{first_date} to {last_date}",
        "grep_evidence": grep_hits,
    }


def _write_project(fm: dict, source_note: str, counters: dict) -> None:
    slug = _slug(fm["title"])
    path = CORPUS_OUT / f"{slug}.md"
    body = f"\n{fm['description']}\n\nTags inferred from: {source_note}.\n"

    if path.exists():
        old_fm, old_body = parse_file(path)
        merged_fm = merge_frontmatters([old_fm, fm])
        write_file(path, merged_fm, body)
        counters["updated"] += 1
    else:
        write_file(path, fm, body)
        counters["written"] += 1

    print(f"  {fm['title']}: tags={fm['domain_tags']}")


def main(repos: list[Path], subprojects: list[Path] | None = None) -> None:
    CORPUS_OUT.mkdir(parents=True, exist_ok=True)
    counters = {"written": 0, "updated": 0, "skipped": 0}

    for repo in repos:
        fm = ingest_repo(repo)
        if fm is None:
            print(f"  skip {repo.name}: no commits found")
            counters["skipped"] += 1
            continue
        note = f"README, tracked filenames, git grep ({', '.join(fm['grep_evidence']) or 'none'})"
        _write_project(fm, note, counters)

    for sub in subprojects or []:
        fm = ingest_pkh_subproject(sub)
        if fm is None:
            print(f"  skip {sub.name}: no files found")
            counters["skipped"] += 1
            continue
        _write_project(fm, "README, directory name, package.json, source-file snippets, tracked filenames", counters)

    print(f"\nNew files: {counters['written']}, updated: {counters['updated']}, skipped: {counters['skipped']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", action="append", help="Explicit repo path (repeatable); default: auto-discover siblings")
    args = p.parse_args()

    if args.repo:
        target_repos = [Path(r).resolve() for r in args.repo]
        target_subprojects = []
    else:
        target_repos = discover_repos()
        target_subprojects = discover_pkh_subprojects()

    print(f"Ingesting {len(target_repos)} sibling repo(s): {[r.name for r in target_repos]}")
    print(f"Ingesting {len(target_subprojects)} PKH sub-project(s): {[r.name for r in target_subprojects]}")
    main(target_repos, target_subprojects)
