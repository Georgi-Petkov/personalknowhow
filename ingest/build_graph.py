#!/usr/bin/env python3
"""
Build graph.json from corpus/ using graphifyy (pip install graphifyy).
Reads YAML frontmatter from all corpus/*.md files, extracts nodes and
tag/provider/type edges, then runs graphifyy's build + cluster + export
pipeline to produce graph.json at the repo root.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
CORPUS = ROOT / "corpus"
OUT_DIR = ROOT / "graphify-out"
GRAPH_JSON = ROOT / "graph.json"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _ensure_deps() -> None:
    try:
        import yaml  # noqa: F401
        import graphify  # noqa: F401
    except ImportError:
        import subprocess
        pkgs = []
        try:
            import yaml  # noqa: F401
        except ImportError:
            pkgs.append("pyyaml")
        try:
            import graphify  # noqa: F401
        except ImportError:
            pkgs.append("graphifyy")
        if pkgs:
            print(f"Installing: {', '.join(pkgs)} ...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install"] + pkgs + ["-q", "--break-system-packages"],
                check=True,
            )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", text.lower()).strip("_")


def extract_from_corpus(corpus: Path) -> dict:
    """
    Walk corpus/ for *.md files with YAML frontmatter.
    Creates:
    - one node per learning item (title, provider, date)
    - tag nodes + tagged_with edges (domain_tags)
    - provider nodes + provided_by edges
    - type nodes + is_type edges
    Deduplicates nodes by id.
    """
    import yaml

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(node_id: str, label: str, source_file: str, captured_at=None) -> None:
        if node_id not in seen:
            seen.add(node_id)
            nodes.append({
                "id": node_id,
                "label": label,
                "file_type": "document",
                "source_file": source_file,
                "source_location": None,
                "source_url": None,
                "captured_at": captured_at,
                "author": None,
                "contributor": None,
            })

    def add_edge(source: str, target: str, relation: str, source_file: str) -> None:
        edges.append({
            "source": source,
            "target": target,
            "relation": relation,
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": source_file,
            "source_location": None,
            "weight": 1.0,
        })

    for md_file in sorted(corpus.rglob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(text)
        if not m:
            continue
        fm = yaml.safe_load(m.group(1)) or {}
        title = str(fm.get("title", "")).strip()
        if not title:
            continue

        rel = str(md_file.relative_to(corpus))
        node_id = _slug(md_file.stem)
        add_node(node_id, title, rel, captured_at=str(fm.get("date", "")) or None)

        for tag in fm.get("domain_tags") or []:
            tag_id = "tag_" + _slug(str(tag))
            add_node(tag_id, str(tag), rel)
            add_edge(node_id, tag_id, "tagged_with", rel)

        if fm.get("provider"):
            prov_id = "provider_" + _slug(fm["provider"])
            add_node(prov_id, fm["provider"], rel)
            add_edge(node_id, prov_id, "provided_by", rel)

        if fm.get("type"):
            type_id = "type_" + _slug(fm["type"])
            add_node(type_id, fm["type"], rel)
            add_edge(node_id, type_id, "is_type", rel)

    return {"nodes": nodes, "edges": edges, "hyperedges": [], "input_tokens": 0, "output_tokens": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="Override graphifyy's refuse-to-shrink safety check (#479) -- only pass "
             "this after confirming a smaller node count reflects real corpus deletions, "
             "not a partial/broken extraction.",
    )
    args = parser.parse_args()

    _ensure_deps()

    from graphify.build import build_from_json
    from graphify.cluster import cluster
    from graphify.export import to_json

    if not CORPUS.exists():
        print(f"ERROR: corpus/ not found at {CORPUS}")
        sys.exit(1)

    md_files = list(CORPUS.rglob("*.md"))
    if not md_files:
        print("No markdown files in corpus/. Nothing to build.")
        return

    print(f"Extracting from {len(md_files)} markdown files ...")
    extraction = extract_from_corpus(CORPUS)
    print(f"  {len(extraction['nodes'])} nodes, {len(extraction['edges'])} edges")

    G = build_from_json(extraction)
    if G.number_of_nodes() == 0:
        print("ERROR: Graph is empty after extraction.")
        sys.exit(1)

    communities = cluster(G)
    OUT_DIR.mkdir(exist_ok=True)
    if not to_json(G, communities, str(OUT_DIR / "graph.json"), force=args.force):
        print("graph.json NOT written (see warning above). Pass --force to override.")
        sys.exit(1)
    shutil.copy(OUT_DIR / "graph.json", GRAPH_JSON)

    print(
        f"graph.json written — {G.number_of_nodes()} nodes, "
        f"{G.number_of_edges()} edges, {len(communities)} communities"
    )


if __name__ == "__main__":
    main()
