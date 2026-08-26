#!/usr/bin/env python3
"""
Build mcp/public_entries.json + mcp/public_embeddings.json from graph.json.

Fail-closed allowlist of corpus categories safe for public, unauthenticated
reading. linkedin/job_applications/ and linkedin/career_interests/ (and any
future corpus category not explicitly listed here) are excluded by
construction, not by exception -- this Worker previously leaked exactly that
data once, patched by hand with no enforcement behind it. See
ingest/build_private_export.py for the unfiltered counterpart, which writes
to a completely separate directory (mcp-private/) that this script's output
must never share or reference.

Requires CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_AI_TOKEN in the environment
(a token scoped to Workers AI Read only) to compute embeddings. Run with
--no-embed to skip that step (e.g. for a quick allowlist/safety-check dry run).
"""
import argparse
import base64
import json
import os
import struct
import sys
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
GRAPH_JSON = ROOT / "graph.json"
CORPUS = ROOT / "corpus"
OUT_DIR = ROOT / "mcp"
ENTRIES_JSON = OUT_DIR / "public_entries.json"
EMBEDDINGS_JSON = OUT_DIR / "public_embeddings.json"

EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5"
BATCH_SIZE = 50
MAX_TEXT_CHARS = 1800  # ~450 tokens headroom under bge-base's 512 max

sys.path.insert(0, str(Path(__file__).parent))
from merge import parse_file  # noqa: E402
from common import SOURCE_NOTE_BY_TYPE, DEFAULT_SOURCE_NOTE, looks_garbled  # noqa: E402

# Prefix -> output type. "linkedin" is deliberately never used as a bare key
# -- that would also match linkedin/job_applications/ and
# linkedin/career_interests/, the exact mistake that already leaked real
# company names and application history publicly once. Any corpus category
# not listed here is dropped by default; a future new corpus/<x>/ folder (or
# a new linkedin/<x>/ subfolder) must be added deliberately, with the same
# sensitivity judgment already applied to job_applications/career_interests.
ALLOWLIST: dict[str, str] = {
    "datacamp": "course",
    "edx": "course",
    "skilljar": "course",
    "gmail": "course",
    "linkedin/learning": "course",
    "github": "project",
    "projects": "project",
    "linkedin/certifications": "certification",
    "linkedin/education": "education",
    "linkedin/endorsements": "endorsement",
    "linkedin/positions": "position",
    "linkedin/profile.md": "profile",
    "linkedin/recommendations": "recommendation",
    "linkedin/articles": "article",
    # Added 2026-08-10 alongside the LinkedIn API rebuild (see
    # ingest/linkedin_api_ingest.py) — same "demonstrated evidence" tier as
    # the categories above. "projects_li" (not "projects") deliberately
    # avoids colliding with the bare "projects" key, which already maps
    # corpus/projects/ (project_ingest.py's sibling-repo scan) to "project".
    "linkedin/projects_li": "project",
    "linkedin/organizations": "organization",
    "linkedin/languages": "language",
    "linkedin/honors": "honor",
    "linkedin/publications": "publication",
    "linkedin/patents": "patent",
    "linkedin/volunteering": "volunteering",
    "linkedin/test_scores": "test_score",
}
# Folder-based ALLOWLIST above types everything under corpus/datacamp/ as
# "course" -- wrong for skill_assessment entries (DataCamp Skill Assessments,
# added 2026-08-10), which live in the same folder but represent passing an
# assessment, not completing a course -- exactly the distinction that
# motivated adding them in the first place. Overridden via node_raw_type()
# below rather than a folder split, since both types share corpus/datacamp/.
# "track" (added 2026-08-17) is the same situation: a DataCamp Track bundles
# multiple courses and is a distinct credential from any single course in it.
TYPE_OVERRIDES_BY_RAW_TYPE = {"skill_assessment", "track"}
SENSITIVE_SUBSTRINGS = ["job_applications", "career_interests", "job_search_tracker", "master_cv"]


def match_type(source_file: str) -> str | None:
    if source_file in ALLOWLIST:
        return ALLOWLIST[source_file]
    best_prefix, best_type = None, None
    for prefix, type_ in ALLOWLIST.items():
        if prefix.endswith(".md"):
            continue
        if (source_file == prefix or source_file.startswith(prefix + "/")):
            if best_prefix is None or len(prefix) > len(best_prefix):
                best_prefix, best_type = prefix, type_
    return best_type


def node_tags(node_id: str, links: list[dict]) -> list[str]:
    # graphifyy doesn't consistently put the tag on source vs. target
    # (direction gets canonicalized per-edge) -- a tag_ id can show up on
    # either side, so both must be checked. Same logic as
    # ingest/common.py::known_tag_counts().
    tags = set()
    for edge in links:
        if edge.get("relation") != "tagged_with":
            continue
        src, tgt = str(edge.get("source", "")), str(edge.get("target", ""))
        if node_id not in (src, tgt):
            continue
        tag_id = tgt if tgt.startswith("tag_") else (src if src.startswith("tag_") else None)
        if tag_id:
            tags.add(tag_id.removeprefix("tag_").replace("_", "-"))
    return sorted(tags)


def node_provider(node_id: str, nodes_by_id: dict[str, dict], links: list[dict]) -> str | None:
    # Follows the same provided_by edge build_graph.py already writes, to the
    # provider_* node's real (un-slugged) label -- e.g. "DataCamp", not
    # "provider_datacamp". Direction isn't canonicalized for provided_by the
    # way it is for tagged_with (build_graph.py always writes source=content
    # node, target=provider node), but check both sides defensively anyway,
    # matching node_tags()'s own caution about graphifyy's edge direction.
    for edge in links:
        if edge.get("relation") != "provided_by":
            continue
        src, tgt = str(edge.get("source", "")), str(edge.get("target", ""))
        if node_id not in (src, tgt):
            continue
        prov_id = tgt if tgt.startswith("provider_") else (src if src.startswith("provider_") else None)
        if prov_id and prov_id in nodes_by_id:
            return nodes_by_id[prov_id].get("label")
    return None


def node_raw_type(node_id: str, links: list[dict]) -> str | None:
    # The raw frontmatter `type` value via the is_type edge -- more granular
    # than folder-based typing, used as an independent cross-check (safety
    # check #2) rather than as the primary type source.
    for edge in links:
        if edge.get("relation") != "is_type":
            continue
        if str(edge.get("source", "")) != node_id:
            continue
        tgt = str(edge.get("target", ""))
        if tgt.startswith("type_"):
            return tgt.removeprefix("type_")
    return None


def sanitize(text: str) -> str:
    cleaned = "".join(c for c in text if c.isprintable() or c in "\n\t").strip()
    if not cleaned:
        return ""
    # Shared with merge.py/github_ingest.py -- see common.looks_garbled's
    # docstring for why the ASCII-ratio check alone isn't enough on its own.
    if looks_garbled(cleaned):
        return ""
    return cleaned


def load_description(source_file: str) -> str:
    path = CORPUS / source_file
    if not path.exists():
        return ""
    fm, _ = parse_file(path)
    return sanitize(str(fm.get("description", "") or ""))


def build_text(label: str, type_: str, tags: list[str], description: str) -> str:
    parts = [label, type_, ", ".join(tags), description]
    return " — ".join(p for p in parts if p)[:MAX_TEXT_CHARS]


def build_entries() -> list[dict]:
    graph = json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
    links = graph.get("links", [])
    nodes_by_id = {str(n.get("id", "")): n for n in graph.get("nodes", [])}
    entries = []
    mismatches = []
    for node in graph.get("nodes", []):
        node_id = str(node.get("id", ""))
        if node_id.startswith(("tag_", "provider_", "type_")):
            continue
        source_file = str(node.get("source_file", ""))
        type_ = match_type(source_file)
        if type_ is None:
            continue
        raw_type = node_raw_type(node_id, links)
        if raw_type in ("job_application", "career_interest"):
            mismatches.append(f"{node_id} (source={source_file}, is_type={raw_type})")
            continue
        if raw_type in TYPE_OVERRIDES_BY_RAW_TYPE:
            type_ = raw_type
        tags = node_tags(node_id, links)
        description = load_description(source_file)
        source_url = node.get("source_url")
        entries.append({
            "id": node_id,
            "label": node.get("label", ""),
            "source": source_file,
            "type": type_,
            "tags": tags,
            "description": description,
            "source_url": source_url,
            "captured_at": node.get("captured_at"),
            "provider": node_provider(node_id, nodes_by_id, links),
            "source_note": None if source_url else SOURCE_NOTE_BY_TYPE.get(type_, DEFAULT_SOURCE_NOTE),
            "text": build_text(node.get("label", ""), type_, tags, description),
        })

    if mismatches:
        print("SAFETY CHECK #2 FAILED — folder allowlist passed but is_type is sensitive:", file=sys.stderr)
        for m in mismatches:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)

    entries.sort(key=lambda e: e["source"])

    offenders = [e for e in entries if any(s in e["source"] for s in SENSITIVE_SUBSTRINGS)]
    if offenders:
        print("SAFETY CHECK #1 FAILED — sensitive substring in output:", file=sys.stderr)
        for e in offenders:
            print(f"  {e['source']}", file=sys.stderr)
        sys.exit(1)

    return entries


def embed_batch(texts: list[str], account_id: str, token: str) -> list[list[float]]:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{EMBEDDING_MODEL}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"text": texts}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
    if not body.get("success", False):
        print(f"Embedding request failed: {body.get('errors')}", file=sys.stderr)
        sys.exit(1)
    return body["result"]["data"]


def vector_to_b64(vec: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def build_embeddings(entries: list[dict]) -> dict:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    token = os.environ.get("CLOUDFLARE_AI_TOKEN")
    if not account_id or not token:
        print("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_AI_TOKEN not set — skipping embeddings.", file=sys.stderr)
        sys.exit(1)

    vectors: dict[str, str] = {}
    dim = None
    for i in range(0, len(entries), BATCH_SIZE):
        batch = entries[i:i + BATCH_SIZE]
        embeddings = embed_batch([e["text"] for e in batch], account_id, token)
        for e, vec in zip(batch, embeddings):
            dim = dim or len(vec)
            vectors[e["id"]] = vector_to_b64(vec)
        print(f"  embedded {min(i + BATCH_SIZE, len(entries))}/{len(entries)}")

    return {"model": EMBEDDING_MODEL, "dim": dim, "vectors": vectors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-embed", action="store_true", help="Skip the embedding step")
    args = parser.parse_args()

    if not GRAPH_JSON.exists():
        print(f"ERROR: {GRAPH_JSON} not found. Run build_graph.py first.", file=sys.stderr)
        sys.exit(1)

    entries = build_entries()
    OUT_DIR.mkdir(exist_ok=True)

    if ENTRIES_JSON.exists():
        ENTRIES_JSON.with_suffix(".json.bak").write_text(ENTRIES_JSON.read_text(encoding="utf-8"), encoding="utf-8")

    ENTRIES_JSON.write_text(
        json.dumps([{k: v for k, v in e.items() if k != "text"} for e in entries], indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{ENTRIES_JSON} written — {len(entries)} entries")
    print(Counter(e["type"] for e in entries))

    if args.no_embed:
        return

    if EMBEDDINGS_JSON.exists():
        EMBEDDINGS_JSON.with_suffix(".json.bak").write_text(EMBEDDINGS_JSON.read_text(encoding="utf-8"), encoding="utf-8")

    embeddings = build_embeddings(entries)
    EMBEDDINGS_JSON.write_text(json.dumps(embeddings) + "\n", encoding="utf-8")
    print(f"{EMBEDDINGS_JSON} written — {len(embeddings['vectors'])} vectors, dim={embeddings['dim']}")


if __name__ == "__main__":
    main()
