#!/usr/bin/env python3
"""
Build mcp-private/private_entries.json + mcp-private/private_embeddings.json
from graph.json. NOT an allowlist -- every real content node passes through,
including linkedin/job_applications/ and linkedin/career_interests/. This
output must never be imported by mcp/src/index.ts (the public Worker) or
committed/deployed anywhere reachable without authentication. See
ingest/build_public_export.py for the filtered counterpart.

Requires CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_AI_TOKEN in the environment
to compute embeddings. Run with --no-embed to skip that step.
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
OUT_DIR = ROOT / "mcp-private"
ENTRIES_JSON = OUT_DIR / "private_entries.json"
EMBEDDINGS_JSON = OUT_DIR / "private_embeddings.json"

EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5"
BATCH_SIZE = 50
MAX_TEXT_CHARS = 1800

sys.path.insert(0, str(Path(__file__).parent))
from merge import parse_file  # noqa: E402
from common import SOURCE_NOTE_BY_TYPE, DEFAULT_SOURCE_NOTE  # noqa: E402

# Same folder->type mapping as build_public_export.py's ALLOWLIST, but this
# script has no exclusions -- career_interests/job_applications get their own
# real types below instead of being dropped.
TYPE_MAP: dict[str, str] = {
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
    "linkedin/career_interests": "career_interest",
    "linkedin/job_applications": "job_application",
    # Added 2026-08-10 alongside the LinkedIn API rebuild (see
    # ingest/linkedin_api_ingest.py) — private-tier by the same reasoning as
    # career_interests/job_applications: reveals job-search intent/activity,
    # not demonstrated skill.
    "linkedin/saved_job_alerts": "saved_job_alert",
    "linkedin/job_seeker_preferences": "job_seeker_preferences",
    "linkedin/job_applicant_saved_answers": "saved_answer",
    "linkedin/talent_question_saved_responses": "saved_answer",
}
# corpus/datacamp/ is folder-mapped to "course" above, but skill_assessment
# entries (DataCamp Skill Assessments, added 2026-08-10) live in that same
# folder and represent passing an assessment, not completing a course --
# overridden via each node's real frontmatter type instead of a folder split.
# "track" (added 2026-08-17) is the same situation: a DataCamp Track bundles
# multiple courses and is a distinct credential from any single course in it.
TYPE_OVERRIDES_BY_RAW_TYPE = {"skill_assessment", "track"}


def node_provider(node_id: str, nodes_by_id: dict[str, dict], links: list[dict]) -> str | None:
    # Same as build_public_export.py's node_provider -- follows provided_by to
    # the provider_* node's real (un-slugged) label, e.g. "DataCamp".
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
    for edge in links:
        if edge.get("relation") != "is_type":
            continue
        if str(edge.get("source", "")) != node_id:
            continue
        tgt = str(edge.get("target", ""))
        if tgt.startswith("type_"):
            return tgt.removeprefix("type_")
    return None


def match_type(source_file: str) -> str | None:
    if source_file in TYPE_MAP:
        return TYPE_MAP[source_file]
    best_prefix, best_type = None, None
    for prefix, type_ in TYPE_MAP.items():
        if prefix.endswith(".md"):
            continue
        if source_file == prefix or source_file.startswith(prefix + "/"):
            if best_prefix is None or len(prefix) > len(best_prefix):
                best_prefix, best_type = prefix, type_
    return best_type


def node_tags(node_id: str, links: list[dict]) -> list[str]:
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


def sanitize(text: str) -> str:
    cleaned = "".join(c for c in text if c.isprintable() or c in "\n\t").strip()
    if not cleaned:
        return ""
    ascii_printable = sum(1 for c in cleaned if c.isascii() and c.isprintable())
    if ascii_printable / len(cleaned) < 0.7:
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
    content_nodes = [
        n for n in graph.get("nodes", [])
        if not str(n.get("id", "")).startswith(("tag_", "provider_", "type_"))
    ]
    entries = []
    unmapped = []
    for node in content_nodes:
        node_id = str(node.get("id", ""))
        source_file = str(node.get("source_file", ""))
        type_ = match_type(source_file)
        if type_ is None:
            unmapped.append(source_file)
            type_ = "unknown"
        raw_type = node_raw_type(node_id, links)
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

    entries.sort(key=lambda e: e["source"])

    # Over-filter guard, inverted from the public script's under-filter guard:
    # fail loudly if this run somehow produced fewer entries than the corpus
    # actually has.
    if len(entries) != len(content_nodes):
        print(f"SAFETY CHECK FAILED — {len(content_nodes)} content nodes in graph.json "
              f"but only {len(entries)} entries built.", file=sys.stderr)
        sys.exit(1)
    # Only fail if the corpus actually HAS source files for a sensitive category
    # but none of them survived into the output under the right type -- that's a
    # real TYPE_MAP/matching regression. Not every real customer has job-
    # application or career-interest history in their LinkedIn export at all,
    # and an empty category is normal, not a bug -- this used to assert
    # SENSITIVE_TYPES must always be present, which only held because this
    # script had only ever run against the developer's own (rich) corpus.
    present_types = {e["type"] for e in entries}
    for sensitive_type, prefix in (("career_interest", "linkedin/career_interests"),
                                    ("job_application", "linkedin/job_applications")):
        corpus_has_it = any(str(n.get("source_file", "")).startswith(prefix) for n in content_nodes)
        if corpus_has_it and sensitive_type not in present_types:
            print(f"SAFETY CHECK FAILED — corpus has {prefix} source files but none mapped "
                  f"to type '{sensitive_type}' in the output. This indicates a TYPE_MAP "
                  f"regression, not an empty corpus.", file=sys.stderr)
            sys.exit(1)
    if unmapped:
        print(f"NOTE: {len(unmapped)} node(s) had no TYPE_MAP entry, typed 'unknown': "
              f"{unmapped[:5]}{'...' if len(unmapped) > 5 else ''}", file=sys.stderr)

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
