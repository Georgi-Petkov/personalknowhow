#!/usr/bin/env python3
"""
One-command local quickstart: detects available data sources, ingests them,
merges, and builds graph.json -- entirely local, no Cloudflare account needed.

Usage:
  python quickstart.py
  python quickstart.py --linkedin ~/Downloads/LinkedInDataExport.zip
  python quickstart.py --linkedin ~/Downloads/already_extracted_folder/

After this finishes, query your own graph locally with no cloud account:
  python ingest/query_local.py "what do I know about X"
"""
import argparse
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
INGEST = ROOT / "ingest"
PY = sys.executable


def run(script: str, *args: str) -> bool:
    print(f"\n--- {script} {' '.join(args)} ---".rstrip())
    result = subprocess.run([PY, str(INGEST / script), *args])
    return result.returncode == 0


def resolve_linkedin_export(path_arg: str) -> Path:
    """Accepts either a raw LinkedIn export .zip or an already-extracted
    folder; returns a directory containing the export's CSVs, extracting the
    zip first if needed. Exits with a clear, actionable message on anything
    that doesn't look like a real export -- this is the flow a non-technical
    visitor is most likely to hit trouble on first.
    """
    path = Path(path_arg).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: --linkedin path not found: {path}")
        sys.exit(1)

    if path.is_file():
        if path.suffix.lower() != ".zip":
            print(f"ERROR: --linkedin expects a .zip file or a folder, got: {path}")
            sys.exit(1)
        extract_dir = ROOT / "data" / "incoming" / "extracted" / f"linkedin_{int(time.time())}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        print(f"Extracting {path.name} -> {extract_dir}")
        with zipfile.ZipFile(path) as zf:
            zf.extractall(extract_dir)
    else:
        extract_dir = path

    # LinkedIn's export is normally flat (CSVs at the top level) -- but if
    # everything landed one level down inside a wrapper folder, look there too
    # before giving up.
    if not any(extract_dir.glob("*.csv")):
        for d in extract_dir.iterdir():
            if d.is_dir() and any(d.glob("*.csv")):
                extract_dir = d
                break

    if not any(extract_dir.glob("*.csv")):
        print(
            f"ERROR: no .csv files found under {extract_dir} -- does this look like a "
            f"LinkedIn 'Download your data' export? Request one at linkedin.com > Settings & "
            f"Privacy > Data privacy > Get a copy of your data, then re-run with the path to "
            f"the downloaded .zip."
        )
        sys.exit(1)

    return extract_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--linkedin", metavar="PATH",
        help="Path to your LinkedIn export .zip (or an already-extracted folder) -- request one "
             "at linkedin.com > Settings & Privacy > Data privacy > Get a copy of your data",
    )
    args = parser.parse_args()

    print("Installing/checking dependencies (pip install -r requirements.txt) ...")
    subprocess.run(
        [PY, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt"), "--break-system-packages"],
        check=False,
    )

    ran: list[str] = []
    skipped: list[str] = []

    # 1. project_ingest.py -- pure local git/filesystem introspection, always safe.
    if run("project_ingest.py"):
        ran.append("project evidence (sibling repos)")
    else:
        skipped.append("project evidence (project_ingest.py failed -- see output above)")

    # 2. github_ingest.py -- zero-credential only if `gh` is already authenticated.
    try:
        gh_ok = subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
    except FileNotFoundError:
        gh_ok = False
    if gh_ok:
        if run("github_ingest.py"):
            ran.append("GitHub repos")
    else:
        skipped.append("GitHub repos (run `gh auth login`, then re-run this script)")

    # 3. LinkedIn -- an explicit --linkedin path always wins over auto-detect.
    linkedin_token = ROOT / "connector" / "linkedin_token.json"
    if args.linkedin:
        extract_dir = resolve_linkedin_export(args.linkedin)
        if run("linkedin_ingest.py", "--extract-dir", str(extract_dir)):
            ran.append("LinkedIn export (CSV, from --linkedin)")
    elif linkedin_token.exists():
        if run("linkedin_api_ingest.py"):
            ran.append("LinkedIn (API)")
    else:
        auto_dir = None
        extracted = ROOT / "data" / "incoming" / "extracted"
        candidates = sorted(extracted.glob("linkedin*"), reverse=True) if extracted.exists() else []
        incoming = ROOT / "data" / "incoming"
        if candidates:
            auto_dir = candidates[0]
        elif incoming.exists():
            for d in sorted(incoming.iterdir(), reverse=True):
                if d.is_dir() and (d / "Certifications.csv").exists():
                    auto_dir = d
                    break
        if auto_dir:
            if run("linkedin_ingest.py", "--extract-dir", str(auto_dir)):
                ran.append("LinkedIn export (CSV, auto-detected)")
        else:
            skipped.append(
                "LinkedIn (no connector/linkedin_token.json, no export under data/incoming/ -- "
                "pass --linkedin <path-to-export.zip> to include your own LinkedIn history)"
            )

    # 4. edx_ingest.py -- needs a hand-populated/scraped JSON (no export API exists).
    edx_input = ROOT / "data" / "edx_completed.json"
    if edx_input.exists():
        if run("edx_ingest.py"):
            ran.append("edX")
    else:
        skipped.append("edX (no data/edx_completed.json -- see README)")

    # 5. datacamp_ingest.py -- same idiom as edX.
    dc_input = ROOT / "data" / "datacamp_completed.json"
    if dc_input.exists():
        if run("datacamp_ingest.py"):
            ran.append("DataCamp")
    else:
        skipped.append("DataCamp (no data/datacamp_completed.json -- see README)")

    # 6. gmail_sync.py -- needs a one-time Google OAuth app + consent flow.
    if all(os.environ.get(v) for v in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")):
        if run("gmail_sync.py"):
            ran.append("Gmail")
    else:
        skipped.append(
            "Gmail (GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET/GMAIL_REFRESH_TOKEN not set -- see README)"
        )

    print("\n=== Sources ===")
    for s in ran:
        print(f"  ran:     {s}")
    for s in skipped:
        print(f"  skipped: {s}")

    if not ran:
        print(
            "\nNo data sources available -- nothing to build. At minimum, authenticate the "
            "GitHub CLI (`gh auth login`) or pass --linkedin <path-to-your-export.zip>, then "
            "re-run this script."
        )
        sys.exit(1)

    if not run("merge.py"):
        print("\nmerge.py failed -- see output above.")
        sys.exit(1)

    if not run("build_graph.py"):
        print(
            "\nbuild_graph.py failed -- see its output above. If this is because the node "
            "count would shrink vs. a prior run, only re-run with `python ingest/build_graph.py "
            "--force` if a smaller graph is actually expected (e.g. you removed a source)."
        )
        sys.exit(1)

    graph_json = ROOT / "graph.json"
    node_count = "?"
    if graph_json.exists():
        try:
            node_count = len(json.loads(graph_json.read_text(encoding="utf-8")).get("nodes", []))
        except Exception:
            pass

    print(f"\nDone -- graph.json built with {node_count} nodes from: {', '.join(ran)}.")
    print('Next: python ingest/query_local.py "what do I know about X"')


if __name__ == "__main__":
    main()
