#!/usr/bin/env python3
"""
Ingest the user's real GitHub repos (READMEs + descriptions) into
corpus/github/ -- via the GitHub API (through the already-authenticated `gh`
CLI, no separate token management needed).

Distinct from project_ingest.py: that script only sees repos that happen to
be cloned locally as siblings of PKH under ~/Documents/Projects. This script
sees the user's *entire* GitHub account, including repos never cloned to
this machine -- e.g. small one-off/experiment repos. Forked repos are
excluded by default: a fork is evidence the user *looked at* something, not
that they built or used it, and including them would flood domain_tags with
noise from other people's projects (confirmed live: this account has 30+
forked `django-*` repos from browsing/reference, vs. 2 non-forked repos
that are real hands-on work -- `django_docker`, `django_docker_db`).

Usage:
  python ingest/github_ingest.py                    # auto-detect username via gh CLI
  python ingest/github_ingest.py --username X
  python ingest/github_ingest.py --include-forks
"""
import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from merge import parse_file, merge_frontmatters, write_file  # noqa: E402
from common import infer_tags, _slug, looks_garbled  # noqa: E402

ROOT = Path(__file__).parent.parent
CORPUS_OUT = ROOT / "corpus" / "github"

README_CHARS = 3000  # matches project_ingest.py's README truncation


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        ).stdout
    except Exception:
        return ""


def detect_username() -> str:
    return _run(["gh", "api", "user", "--jq", ".login"]).strip()


def list_repos(username: str, limit: int) -> list[dict]:
    raw = _run([
        "gh", "repo", "list", username,
        "--json", "name,description,url,pushedAt,isArchived,isFork,primaryLanguage",
        "--limit", str(limit),
    ])
    if not raw.strip():
        return []
    return json.loads(raw)


def fetch_readme(owner: str, name: str) -> str:
    b64 = _run(["gh", "api", f"repos/{owner}/{name}/readme", "--jq", ".content"]).strip()
    if not b64:
        return ""
    try:
        text = base64.b64decode(b64).decode("utf-8", errors="ignore")[:README_CHARS]
    except Exception:
        return ""
    # A README that isn't real UTF-8 text (e.g. it's actually binary, or in a
    # different encoding) decodes into garbage under errors="ignore" instead
    # of failing loudly -- confirmed real (corpus/github/storytelling.md).
    # Treat it the same as no README rather than passing corruption downstream.
    return "" if looks_garbled(text) else text


def ingest_repo(owner: str, repo: dict) -> dict:
    name = repo["name"]
    description = (repo.get("description") or "").strip()
    readme_text = fetch_readme(owner, name)

    evidence = " ".join([name.replace("-", " ").replace("_", " "), description, readme_text])
    tags = infer_tags(evidence)

    readme_first_line = next(
        (l.strip("# ").strip() for l in readme_text.splitlines() if l.strip()), ""
    )
    desc = description or readme_first_line or f"GitHub repo: {name}"

    return {
        "title":       name,
        "type":        "project",
        "provider":    "github",
        "date":        (repo.get("pushedAt") or "")[:10],  # date part only
        "description": desc[:400],
        "domain_tags": tags,
        "repo_url":    repo.get("url", ""),
        "is_archived": bool(repo.get("isArchived")),
    }


def main(username: str, include_forks: bool, limit: int) -> None:
    CORPUS_OUT.mkdir(parents=True, exist_ok=True)
    repos = list_repos(username, limit)
    if not include_forks:
        repos = [r for r in repos if not r.get("isFork")]

    print(f"Ingesting {len(repos)} repo(s) for {username} "
          f"({'including' if include_forks else 'excluding'} forks)")

    written = updated = skipped = 0
    for repo in repos:
        fm = ingest_repo(username, repo)
        if not fm["date"]:
            print(f"  skip {repo['name']}: no pushedAt date")
            skipped += 1
            continue

        slug = _slug(fm["title"])
        path = CORPUS_OUT / f"{slug}.md"
        body = f"\n{fm['description']}\n\nRepo: {fm['repo_url']}\n"

        if path.exists():
            old_fm, old_body = parse_file(path)
            merged_fm = merge_frontmatters([old_fm, fm])
            write_file(path, merged_fm, body)
            updated += 1
        else:
            write_file(path, fm, body)
            written += 1

        print(f"  {fm['title']}: tags={fm['domain_tags']}")

    print(f"\nNew files: {written}, updated: {updated}, skipped: {skipped}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--username", help="GitHub username; default: auto-detect via `gh api user`")
    p.add_argument("--include-forks", action="store_true", help="Include forked repos (excluded by default)")
    p.add_argument("--limit", type=int, default=300, help="Max repos to list")
    args = p.parse_args()

    username = args.username or detect_username()
    if not username:
        print("Could not detect GitHub username -- pass --username, or check `gh auth status`.")
        sys.exit(1)

    main(username, args.include_forks, args.limit)
