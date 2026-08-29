#!/usr/bin/env python3
"""
Local semantic search over your own knowledge graph -- no Cloudflare account,
no deployed MCP server required. Mirrors the deployed public Worker's
query_knowhow tool (mcp/src/index.ts): same allowlist-filtered entry set,
same cosine-similarity ranking -- but embeds locally with sentence-transformers
instead of Cloudflare Workers AI.

Reuses ingest/build_public_export.py's build_entries() directly (imported,
never invoked as a subprocess) to get the exact same fail-closed public
entry set the real Worker serves -- no separate allowlist logic here, and
nothing is ever written into mcp/. Local entries + embeddings are cached in
data/local_query_cache.json (data/ is already gitignored repo-wide).

Usage:
  python ingest/query_local.py "do I have Django experience"
  python ingest/query_local.py --json "aws"
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
GRAPH_JSON = ROOT / "graph.json"
CACHE_FILE = ROOT / "data" / "local_query_cache.json"

MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Empirically chosen by spot-checking bge-small-en-v1.5's real cosine-score
# spread against this repo's own real corpus. No-match probes ("underwater
# basket weaving" -- already validated elsewhere in this repo, see
# ingest/test_mcp_deployment.py -- plus "medieval falconry husbandry" and
# "competitive figure skating judging") topped out at 0.55-0.613 (lexical
# false positives, e.g. "competition" matching a Kaggle course). Genuine
# matches for real corpus topics (django, aws, docker, terraform, machine
# learning) all scored >= 0.64. 0.62 sits in that gap with margin on both
# sides. NOT the same value as mcp/src/index.ts's SIM_FLOOR (0.65) -- that
# was tuned for a different model (bge-base-en-v1.5) and its score
# distribution does not transfer.
SIM_FLOOR = 0.62
TOP_N = 10

sys.path.insert(0, str(Path(__file__).parent))

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    import subprocess
    print("Installing sentence-transformers (one-time download, no Cloudflare account needed) ...",
          file=sys.stderr)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "sentence-transformers", "-q", "--break-system-packages"],
        check=True,
    )
    from sentence_transformers import SentenceTransformer

from build_public_export import build_entries  # noqa: E402


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(vectors_by_id: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"model": MODEL_NAME, "vectors": vectors_by_id}), encoding="utf-8")


def get_entry_vectors(model: "SentenceTransformer", entries: list[dict]) -> dict[str, list[float]]:
    """Returns {entry_id: vector}, embedding only entries that are new or
    whose text changed since the last cached run (incremental, same idiom
    as this repo's Cloudflare-backed re-embed workflow)."""
    cache = load_cache()
    cached_vectors = cache.get("vectors", {}) if cache.get("model") == MODEL_NAME else {}

    vectors: dict[str, list[float]] = {}
    to_embed = []
    for e in entries:
        h = text_hash(e["text"])
        cached = cached_vectors.get(e["id"])
        if cached and cached.get("text_hash") == h:
            vectors[e["id"]] = cached["vector"]
        else:
            to_embed.append(e)

    if to_embed:
        print(
            f"Embedding {len(to_embed)} new/changed entr{'y' if len(to_embed) == 1 else 'ies'} "
            f"(of {len(entries)} total) ...",
            file=sys.stderr,
        )
        embedded = model.encode(
            [e["text"] for e in to_embed], show_progress_bar=len(to_embed) > 20, normalize_embeddings=True
        )
        for e, vec in zip(to_embed, embedded):
            vectors[e["id"]] = vec.tolist()

    new_cache_vectors = {e["id"]: {"text_hash": text_hash(e["text"]), "vector": vectors[e["id"]]} for e in entries}
    save_cache(new_cache_vectors)
    return vectors


def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("topic", help="A skill, technology, or topic to check, e.g. 'django' or 'aws'")
    parser.add_argument("--json", action="store_true", dest="as_json",
                         help="Print machine-readable JSON instead of a formatted list")
    args = parser.parse_args()

    if not GRAPH_JSON.exists():
        print(f"ERROR: {GRAPH_JSON} not found. Run `python quickstart.py` first.", file=sys.stderr)
        sys.exit(1)

    entries = build_entries()
    if not entries:
        print("No public-tier entries found in graph.json -- nothing to search.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading local embedding model ({MODEL_NAME}) ...", file=sys.stderr)
    model = SentenceTransformer(MODEL_NAME)

    vectors = get_entry_vectors(model, entries)
    query_vec = model.encode(args.topic, normalize_embeddings=True).tolist()

    scored = []
    for e in entries:
        vec = vectors.get(e["id"])
        if vec is None:
            continue
        score = round(cosine_sim(query_vec, vec), 3)
        if score >= SIM_FLOOR:
            scored.append({**{k: v for k, v in e.items() if k != "text"}, "score": score})

    scored.sort(key=lambda e: e["score"], reverse=True)
    scored = scored[:TOP_N]

    if scored:
        result = {"found": True, "count": len(scored), "evidence": scored}
    else:
        result = {
            "found": False,
            "note": "No grounded evidence for this topic in the corpus -- do not assume or guess.",
        }

    if args.as_json:
        print(json.dumps(result, indent=2))
        return

    if not result["found"]:
        print(f'\nNo grounded evidence found for "{args.topic}".')
        return

    print(f'\n{result["count"]} result(s) for "{args.topic}":\n')
    for e in scored:
        print(f"  [{e['score']:.3f}] {e['label']}  ({e['type']})")
        if e.get("tags"):
            print(f"           tags: {', '.join(e['tags'])}")
        if e.get("description"):
            desc = e["description"]
            print(f"           {desc[:140]}{'...' if len(desc) > 140 else ''}")
        print()


if __name__ == "__main__":
    main()
