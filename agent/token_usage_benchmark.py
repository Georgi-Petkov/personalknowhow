#!/usr/bin/env python3
"""
Measure real token-usage numbers comparing two ways of getting personal-
corpus context in front of Claude:

  Method A ("files")  -- how many tokens raw LinkedIn export CSVs would
                         occupy if attached as context, all of them, on
                         every question (the "upload files to a Project"
                         shape). Computed locally from real file content --
                         no API call, no cost, ever.
  Method B ("graph")  -- how many tokens the public personalknowhow-demo MCP
                         Worker actually returns for the specific tool calls
                         needed to answer each question. Real HTTP calls
                         against the live public Worker (same JSON-RPC
                         conventions as ingest/test_mcp_deployment.py) --
                         also no cost: that Worker is free, public,
                         unauthenticated infrastructure, not a billed API.

Built as a follow-up to linkedin/posts/2026-W35.md ("File vs. Graph: What a
LinkedIn Export Actually Becomes"), which made three qualitative claims
(search quality, evidence grounding, relationship traversal) with no
measured numbers.

**Rewritten 2026-08-27 after a real mistake**: the first version of this
script called the Anthropic Messages API directly (client.beta.messages.
create against ANTHROPIC_API_KEY) to get "real" answers from a model,
which meant billing a separate API key for a comparison this repo's own
existing session context could already answer for free -- the exact thing
this project's own standing rule (see root CLAUDE.md, "No separate paid API
spend") exists to prevent. This version makes NO calls to api.anthropic.com
at all. It measures context size and retrieval-payload size directly,
which is what the article's claims are actually about (how much has to be
loaded to answer a question), not "what does it cost an LLM to read it and
respond" -- the second question doesn't need to be asked to answer the
first.

Token counts throughout are a plain len(text)//4 character-based estimate,
not a real Claude tokenizer (none is available offline in this environment
-- no `tiktoken`, and Anthropic's exact tokenizer isn't published locally;
their only tokenizer is the billed count_tokens API endpoint, deliberately
not used here either, to keep this script genuinely zero-cost). The same
estimate function is applied to both methods, so the comparison stays fair
even though neither individual number is exact.

Method B's tool-call sequence per question is fixed in QUESTIONS below, not
decided live by a model -- there's no LLM in this script's loop anymore.
Each sequence documents the real tool calls a reasoning agent would make to
answer that question (the same calls made manually, live, earlier in the
session that designed this benchmark -- see the plan at
~/.claude/plans/that-s-going-to-be-valiant-bachman.md). This makes Method
B's numbers fully deterministic and reproducible, at the cost of not
capturing any variance in how a live model might phrase or sequence its own
queries.
"""
import argparse
import dataclasses
import datetime
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUTPUT_DIR_DEFAULT = ROOT / "data" / "token_usage_benchmark"
FILE_LIST_DEFAULT = ROOT / "agent" / "token_usage_benchmark_files.txt"

# Same public, no-auth, no-cost Worker agent/README.md and
# ingest/test_mcp_deployment.py already use. Duplicated as a constant here
# rather than imported, matching this repo's existing style.
DEMO_MCP_URL = "https://personalknowhow-demo.kxtwrdzt6g.workers.dev/mcp"

# Each question's tool-call plan is a list of (tool_name, arguments_dict,
# resolve_id_from_prev) steps. "resolve_id_from_prev": True means "take the
# first entry id from the previous step's response and use it as this
# step's `id` argument" -- models the real two-step
# resolve-a-name-to-an-id-then-traverse pattern related_entries requires.
QUESTIONS = [
    {
        "id": "q1",
        "text": (
            "Do I have any hands-on experience with both Databricks and dbt "
            "specifically, and if so, is there any single course, project, "
            "or job where I used them together -- not just two separate "
            "mentions?"
        ),
        "dimension": "search_quality",
        "plan": [
            ("query_knowhow", {"topic": "Databricks"}, False),
            ("query_knowhow", {"topic": "dbt"}, False),
        ],
    },
    {
        "id": "q2",
        "text": (
            "I know I've studied data governance and separately worked with "
            "Snowflake -- is there any connection between the two in my "
            "history (same course, provider, or project covering both), or "
            "are they unrelated for me?"
        ),
        "dimension": "search_quality",
        "secondary_dimensions": ["relationship_traversal"],
        "plan": [
            ("query_knowhow", {"topic": "data governance"}, False),
            ("query_knowhow", {"topic": "Snowflake"}, False),
        ],
    },
    {
        "id": "q3",
        "text": (
            "How much real, demonstrated evidence do I have of Excel skills "
            "-- every completed course or certification that actually used "
            "it -- broken down by type, and what's the total count?"
        ),
        "dimension": "evidence_grounding",
        "plan": [("skill_evidence", {"tag": "excel"}, False)],
        "ground_truth_note": "Live-checked this session: found=true, count=12.",
    },
    {
        "id": "q4",
        "text": (
            "Across my whole history, how many distinct real, completed "
            "items (courses, certifications, positions, projects) actually "
            "demonstrate pharmaceutical / clinical / life-sciences domain "
            "knowledge, broken down by type?"
        ),
        "dimension": "evidence_grounding",
        "plan": [("skill_evidence", {"tag": "pharma"}, False)],
        "ground_truth_note": "Live-checked while building this script: found=true, count=4.",
    },
    {
        "id": "q5",
        "text": (
            "Starting from my completed 'Introduction to Excel' course on "
            "DataCamp, what else in my history shares a tag or the same "
            "content provider with it?"
        ),
        "dimension": "relationship_traversal",
        "plan": [
            ("query_knowhow", {"topic": "Introduction to Excel DataCamp"}, False),
            ("related_entries", {}, True),
        ],
    },
    {
        "id": "q6",
        "text": (
            "What other courses, certifications, or credentials in my "
            "history are connected to my Airflow experience through a "
            "shared tag or the same provider?"
        ),
        "dimension": "relationship_traversal",
        "plan": [
            ("query_knowhow", {"topic": "Airflow"}, False),
            ("related_entries", {}, True),
        ],
    },
    {
        "id": "q7",
        "text": (
            "Summarize my A/B testing / experimentation experience, and "
            "tell me what else in my history is connected to it through a "
            "shared skill tag or provider."
        ),
        "dimension": "relationship_traversal",
        "secondary_dimensions": ["search_quality"],
        "plan": [
            ("query_knowhow", {"topic": "A/B testing experimentation"}, False),
            ("related_entries", {}, True),
        ],
    },
    {
        "id": "q8",
        "text": (
            "List every course or certification I've completed that relates "
            "to data-engineering tools like Databricks, dbt, Airflow, or "
            "Snowflake, and tell me the type breakdown."
        ),
        "dimension": "evidence_grounding",
        "secondary_dimensions": ["search_quality"],
        "plan": [
            ("list_by_type", {"type": "course"}, False),
            ("list_by_type", {"type": "certification"}, False),
        ],
        "note": (
            "Public tier has no demonstrated/signal_only field at all "
            "(confirmed by reading mcp/src/index.ts vs mcp-private/src/"
            "index.ts) -- this tests type-breakdown aggregate lookup, not a "
            "literal evidence-tier split. See the plan's evidence-grounding "
            "discussion for why."
        ),
    },
]


@dataclasses.dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    response_bytes: int
    response_tokens_est: int
    found: bool | None
    error: str | None = None


@dataclasses.dataclass
class QuestionResult:
    question_id: str
    method: str
    dimension: str
    secondary_dimensions: list[str]
    total_tokens_est: int = 0
    call_count: int = 0
    tool_calls: list[ToolCallRecord] = dataclasses.field(default_factory=list)
    error: str | None = None


def estimate_tokens(text: str) -> int:
    """len//4 character-based estimate -- see module docstring for why no
    real tokenizer is used. Applied identically to both methods."""
    return len(text) // 4


def jsonrpc_call(url: str, method: str, params: dict | None = None, timeout: int = 20):
    """Same conventions as ingest/test_mcp_deployment.py's jsonrpc_call --
    duplicated rather than imported (agent/ and ingest/ don't share code in
    this repo). Returns (raw_response_text, parsed_dict_or_None, error_or_None)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0",  # Cloudflare blocks urllib's default UA with a bare 403
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return None, None, str(e)
    line = next((l for l in raw.splitlines() if l.startswith("data: ")), None)
    payload = line[len("data: "):] if line else raw
    try:
        return payload, json.loads(payload), None
    except json.JSONDecodeError:
        return payload, None, f"unparseable response: {raw[:200]}"


def tool_result_text(parsed: dict) -> str | None:
    content = parsed.get("result", {}).get("content", [])
    return content[0].get("text") if content else None


def run_method_b(mcp_url: str, question: dict) -> QuestionResult:
    result = QuestionResult(
        question_id=question["id"], method="graph", dimension=question["dimension"],
        secondary_dimensions=question.get("secondary_dimensions", []),
    )
    prev_id = None
    for name, args, resolve_id in question["plan"]:
        call_args = dict(args)
        if resolve_id:
            if not prev_id:
                result.tool_calls.append(ToolCallRecord(
                    name=name, arguments=call_args, response_bytes=0,
                    response_tokens_est=0, found=None,
                    error="no id resolved from previous step -- skipped"))
                continue
            call_args["id"] = prev_id

        raw, parsed, err = jsonrpc_call(mcp_url, "tools/call", {"name": name, "arguments": call_args})
        if err or not parsed:
            result.tool_calls.append(ToolCallRecord(
                name=name, arguments=call_args, response_bytes=0,
                response_tokens_est=0, found=None, error=err or "no response"))
            continue

        text = tool_result_text(parsed) or ""
        tokens = estimate_tokens(text)
        result.tool_calls.append(ToolCallRecord(
            name=name, arguments=call_args, response_bytes=len(text.encode("utf-8")),
            response_tokens_est=tokens, found=None, error=None))
        result.total_tokens_est += tokens
        result.call_count += 1

        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {}
        if resolve_id is False:
            entries = body.get("evidence") or body.get("entries") or []
            prev_id = entries[0]["id"] if entries else None

    return result


def resolve_file_list(export_dir: Path, files_arg: str | None,
                       file_list_arg: Path | None, all_files: bool) -> list[Path]:
    if all_files:
        paths = sorted(p for p in export_dir.rglob("*") if p.is_file())
        if not paths:
            sys.exit(f"--all-files but no files found under {export_dir}")
        return paths

    names: list[str] = []
    if files_arg:
        names.extend(n.strip() for n in files_arg.split(",") if n.strip())
    else:
        list_path = file_list_arg or FILE_LIST_DEFAULT
        if not list_path.exists():
            sys.exit(
                f"No file list found at {list_path}, and neither --files nor "
                "--all-files was given. Pass one explicitly -- this script "
                "never silently globs a directory."
            )
        for line in list_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)

    if not names:
        sys.exit("Resolved an empty file list -- nothing to measure for Method A.")

    paths, missing = [], []
    for name in names:
        p = export_dir / name
        (paths if p.exists() and p.is_file() else missing).append(p if p.exists() else name)
    if missing:
        sys.exit(
            f"{len(missing)} file(s) from the manifest not found under {export_dir}:\n  "
            + "\n  ".join(str(m) for m in missing)
        )
    return paths


def run_method_a(file_paths: list[Path], question: dict) -> QuestionResult:
    """Same file-content token estimate for every question -- Method A
    attaches the full file set regardless of what's being asked, which is
    itself part of the article's point (no per-question retrieval at all)."""
    total_chars = 0
    for p in file_paths:
        total_chars += len(p.read_text(encoding="utf-8", errors="replace"))
    tokens = estimate_tokens("x" * total_chars) + estimate_tokens(question["text"])
    return QuestionResult(
        question_id=question["id"], method="files", dimension=question["dimension"],
        secondary_dimensions=question.get("secondary_dimensions", []),
        total_tokens_est=tokens, call_count=0, tool_calls=[],
    )


def save_results(results: list[QuestionResult], meta: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    json_path, md_path = output_dir / f"{date}.json", output_dir / f"{date}.md"
    if json_path.exists() or md_path.exists():
        suffix = f"_{datetime.datetime.now():%H%M%S}"
        json_path, md_path = output_dir / f"{date}{suffix}.json", output_dir / f"{date}{suffix}.md"

    payload = {"methodology": meta, "results": [dataclasses.asdict(r) for r in results]}
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# Token usage benchmark (zero-cost: local file-size estimate vs. real public-Worker payloads)",
        "", f"Run: {meta['run_timestamp']}", "",
        "| question | method | dimension | est. tokens | tool/API calls |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r.question_id} | {r.method} | {r.dimension} | "
                      f"{r.total_tokens_est} | {r.call_count} |")

    by_method: dict[str, list[QuestionResult]] = {}
    for r in results:
        by_method.setdefault(r.method, []).append(r)
    lines += ["", "## Averages by method", "",
              "| method | avg tokens | avg calls |", "|---|---|---|"]
    for method, rs in by_method.items():
        n = len(rs) or 1
        lines.append(f"| {method} | {sum(r.total_tokens_est for r in rs)/n:.0f} | "
                      f"{sum(r.call_count for r in rs)/n:.1f} |")

    if "files" in by_method and "graph" in by_method:
        a_tok = sum(r.total_tokens_est for r in by_method["files"])
        b_tok = sum(r.total_tokens_est for r in by_method["graph"])
        lines += ["", "## Efficiency ratio (files / graph)", "",
                  f"- Token ratio: {a_tok / b_tok:.1f}x" if b_tok else "- Token ratio: n/a"]

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export-dir", type=Path,
                         help="Unpacked LinkedIn export directory (required for Method A)")
    parser.add_argument("--files", help="Comma-separated filenames, relative to --export-dir")
    parser.add_argument("--file-list", type=Path, help=f"Manifest file (default: {FILE_LIST_DEFAULT})")
    parser.add_argument("--all-files", action="store_true",
                         help="Use every file under --export-dir (explicit opt-in)")
    parser.add_argument("--mcp-url", default=DEMO_MCP_URL)
    parser.add_argument("--questions", dest="question_ids", help="Comma-separated question ids (default: all)")
    parser.add_argument("--methods", choices=["both", "files", "graph"], default="both")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR_DEFAULT)
    args = parser.parse_args()

    questions = QUESTIONS
    if args.question_ids:
        wanted = set(q.strip() for q in args.question_ids.split(","))
        questions = [q for q in QUESTIONS if q["id"] in wanted]
        missing = wanted - {q["id"] for q in questions}
        if missing:
            sys.exit(f"Unknown question id(s): {', '.join(sorted(missing))}")

    file_paths: list[Path] = []
    if args.methods in ("both", "files"):
        if not args.export_dir:
            sys.exit("--export-dir is required when Method A runs (--methods both|files)")
        file_paths = resolve_file_list(args.export_dir, args.files, args.file_list, args.all_files)
        print(f"Method A file set: {len(file_paths)} files, "
              f"{sum(p.stat().st_size for p in file_paths):,} bytes", file=sys.stderr)

    results: list[QuestionResult] = []
    for q in questions:
        print(f"\n=== {q['id']}: {q['text'][:70]}... ===", file=sys.stderr)
        if args.methods in ("both", "files"):
            results.append(run_method_a(file_paths, q))
            print(f"  Method A: ~{results[-1].total_tokens_est:,} tokens (local estimate)", file=sys.stderr)
        if args.methods in ("both", "graph"):
            r = run_method_b(args.mcp_url, q)
            results.append(r)
            print(f"  Method B: ~{r.total_tokens_est:,} tokens across {r.call_count} real tool call(s)",
                  file=sys.stderr)
            for tc in r.tool_calls:
                status = f"error: {tc.error}" if tc.error else f"{tc.response_bytes}B"
                print(f"    - {tc.name}({tc.arguments}) -> {status}", file=sys.stderr)

    meta = {
        "run_timestamp": datetime.datetime.now().isoformat(),
        "methods": args.methods,
        "token_estimate_method": "len(text)//4 character heuristic, no real tokenizer or API call used",
        "method_a_file_manifest": [str(p) for p in file_paths],
        "method_b_mcp_url": args.mcp_url,
        "method_b_note": (
            "Tool-call sequence per question is fixed in QUESTIONS, not decided live by a "
            "model -- see module docstring. Real HTTP calls against the live public Worker, "
            "zero cost (no auth, no billing -- public demo infrastructure)."
        ),
        "cost_usd": 0.0,
    }
    json_path, md_path = save_results(results, meta, args.output_dir)
    print(f"\nSaved: {json_path}\n       {md_path}")


if __name__ == "__main__":
    main()
