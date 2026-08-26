#!/usr/bin/env python3
"""
Regression test suite for every deployed PersonalKnowHow MCP Worker.

Run this after any change to mcp/, mcp-private/, mcp-customer-template*/,
before AND after redeploying, and any time entries.json/embeddings.json get
regenerated.

Built after a real incident (2026-08-23): mcp/public_entries.json and
mcp/public_embeddings.json had drifted out of sync (57 entries with no
matching embedding vector), and query_knowhow crashed on EVERY query as a
result -- not caught by anyone until a user manually tried it live. This
suite exists so that class of bug is caught locally, before redeploying,
instead of in front of a real connector.

Two layers:
  1. Local data integrity -- entries.json vs embeddings.json id-set match,
     for every locally-built dataset (mcp/, mcp-private/). Fast, no network,
     catches the exact 2026-08-23 bug directly. Always runs.
  2. Live smoke tests -- calls list_by_type/query_knowhow/related_entries
     against real deployed Workers over HTTP, checking for a clean
     (non-error) JSON-RPC response. Requires network. Always tests the
     public demo (personalknowhow-demo, genuinely public infra, no PII to
     protect); additionally tests real customer Workers if they can be
     found via (a) a local, gitignored data/mcp_worker_registry.json, or
     (b) live D1 lookup when CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_D1_TOKEN are
     set. Neither path hardcodes any real customer identity into this
     script -- this file is tracked in the public repo, customer Worker
     URLs are not.

No CI wiring, on purpose -- this project's fork-and-populate personal-data
policy is explicitly no automation touching production, human-triggered
only (see CLAUDE.md). Run this by hand.

Exit code 0 = everything passed or was cleanly skipped. Exit code 1 = at
least one real failure -- see the printed report for which.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
REGISTRY_PATH = ROOT / "data" / "mcp_worker_registry.json"

DEMO_URL = "https://personalknowhow-demo.kxtwrdzt6g.workers.dev/mcp"
D1_DATABASE_ID = "934afff4-c462-41b0-91ff-2232473c5286"

# (label, entries_path, embeddings_path)
LOCAL_DATASETS = [
    ("mcp/ (public demo source)",
     ROOT / "mcp" / "public_entries.json", ROOT / "mcp" / "public_embeddings.json"),
    ("mcp-private/ (private source)",
     ROOT / "mcp-private" / "private_entries.json", ROOT / "mcp-private" / "private_embeddings.json"),
]

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    marker = {"PASS": "  ✓", "FAIL": "  ✗", "SKIP": "  -"}[status]
    print(f"{marker} {name}" + (f" -- {detail}" if detail else ""))


def check_local_data_integrity() -> None:
    print("\n== Local data integrity (entries.json vs embeddings.json) ==")
    for label, entries_path, embeddings_path in LOCAL_DATASETS:
        if not entries_path.exists() or not embeddings_path.exists():
            record(SKIP, label, "file(s) not found")
            continue
        entries = json.loads(entries_path.read_text(encoding="utf-8"))
        embeddings = json.loads(embeddings_path.read_text(encoding="utf-8"))
        entry_ids = {e["id"] for e in entries}
        vector_ids = set(embeddings.get("vectors", {}).keys())
        missing = entry_ids - vector_ids
        if missing:
            example = sorted(missing)[0]
            record(FAIL, label,
                   f"{len(missing)} entries with no embedding (e.g. '{example}') -- "
                   "query_knowhow will crash on every query. Regenerate embeddings.")
        else:
            record(PASS, label, f"{len(entry_ids)} entries, all have vectors")


def jsonrpc_call(url: str, method: str, params: dict | None = None,
                  token: str | None = None, timeout: int = 20):
    """Returns (http_status, parsed_jsonrpc_dict_or_None, error_str_or_None)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    # Cloudflare blocks Python urllib's default User-Agent with a bare 403 on
    # every request, application logic never runs -- same anti-bot behavior
    # agent/job_fit_agent.py already works around for the same reason.
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0",
    }
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return None, None, str(e)

    # Response may be plain JSON or SSE ("event: message\ndata: {...}") --
    # handle both rather than assuming one.
    line = next((l for l in raw.splitlines() if l.startswith("data: ")), None)
    payload = line[len("data: "):] if line else raw
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return status, None, raw[:300]
    return status, parsed, None


def tool_result_body(parsed: dict) -> tuple[dict | None, bool]:
    """Extracts (parsed_text_content, is_error) from a tools/call JSON-RPC result."""
    result = parsed.get("result", {})
    is_error = bool(result.get("isError"))
    content = result.get("content", [])
    text = content[0].get("text") if content else None
    if text is None:
        return None, is_error
    try:
        return json.loads(text), is_error
    except json.JSONDecodeError:
        return {"_raw": text}, is_error


def check_public_worker(name: str, url: str) -> None:
    print(f"\n== {name} (public) -- {url} ==")

    status, parsed, err = jsonrpc_call(url, "tools/call",
                                        {"name": "list_by_type", "arguments": {"type": "course"}})
    if err or not parsed:
        record(FAIL, f"{name}: list_by_type", err or "no response")
        return
    body, is_error = tool_result_body(parsed)
    if is_error or body is None:
        record(FAIL, f"{name}: list_by_type", body or "unparseable response")
    else:
        record(PASS, f"{name}: list_by_type", f"{body.get('count')} courses")

    status, parsed, err = jsonrpc_call(url, "tools/call",
                                        {"name": "query_knowhow", "arguments": {"topic": "python"}})
    if err or not parsed:
        record(FAIL, f"{name}: query_knowhow", err or "no response")
    else:
        body, is_error = tool_result_body(parsed)
        if is_error or body is None:
            record(FAIL, f"{name}: query_knowhow", body or "unparseable response")
        else:
            record(PASS, f"{name}: query_knowhow", f"found={body.get('found')}, count={body.get('count', 0)}")

    # A nonsense topic must degrade to found:false cleanly, never error --
    # this is what actually broke on 2026-08-23 (every topic errored, not
    # just unmatched ones).
    status, parsed, err = jsonrpc_call(
        url, "tools/call", {"name": "query_knowhow", "arguments": {"topic": "underwater basket weaving"}})
    if err or not parsed:
        record(FAIL, f"{name}: query_knowhow (no-match topic)", err or "no response")
    else:
        body, is_error = tool_result_body(parsed)
        if is_error:
            record(FAIL, f"{name}: query_knowhow (no-match topic)", f"errored instead of found:false -- {body}")
        elif body and body.get("found") is False:
            record(PASS, f"{name}: query_knowhow (no-match topic)", "found=false as expected")
        else:
            record(FAIL, f"{name}: query_knowhow (no-match topic)", f"expected found=false, got {body}")

    _, tools_parsed, tools_err = jsonrpc_call(url, "tools/list")
    tool_names = {t["name"] for t in tools_parsed.get("result", {}).get("tools", [])} if tools_parsed else set()

    if "related_entries" not in tool_names:
        record(SKIP, f"{name}: related_entries", "not deployed on this Worker")
    else:
        _, list_parsed, _ = jsonrpc_call(url, "tools/call",
                                          {"name": "list_by_type", "arguments": {"type": "course"}})
        list_body, _ = tool_result_body(list_parsed) if list_parsed else (None, True)
        sample_id = list_body["entries"][0]["id"] if list_body and list_body.get("entries") else None

        if sample_id:
            status, parsed, err = jsonrpc_call(url, "tools/call",
                                                {"name": "related_entries", "arguments": {"id": sample_id}})
            if err or not parsed:
                record(FAIL, f"{name}: related_entries (real id)", err or "no response")
            else:
                body, is_error = tool_result_body(parsed)
                if is_error or body is None or "related_by_tag" not in body:
                    record(FAIL, f"{name}: related_entries (real id)", body or "unparseable response")
                else:
                    record(PASS, f"{name}: related_entries (real id)", f"id={sample_id}")
        else:
            record(SKIP, f"{name}: related_entries (real id)", "no entries to sample")

        status, parsed, err = jsonrpc_call(url, "tools/call",
                                            {"name": "related_entries", "arguments": {"id": "no_such_id_ever_xyz"}})
        if err or not parsed:
            record(FAIL, f"{name}: related_entries (unknown id)", err or "no response")
        else:
            body, is_error = tool_result_body(parsed)
            if is_error:
                record(FAIL, f"{name}: related_entries (unknown id)", f"errored instead of found:false -- {body}")
            elif body and body.get("found") is False:
                record(PASS, f"{name}: related_entries (unknown id)", "found=false as expected")
            else:
                record(FAIL, f"{name}: related_entries (unknown id)", f"expected found=false, got {body}")

    if "skill_evidence" not in tool_names:
        record(SKIP, f"{name}: skill_evidence", "not deployed on this Worker")
        return

    # "python" is a tag guaranteed to exist and only grow over time -- assert
    # found/count>0 structurally, never a hardcoded count (the corpus grows
    # monthly, a fixed number would false-fail every future run).
    status, parsed, err = jsonrpc_call(url, "tools/call",
                                        {"name": "skill_evidence", "arguments": {"tag": "python"}})
    if err or not parsed:
        record(FAIL, f"{name}: skill_evidence (real tag)", err or "no response")
    else:
        body, is_error = tool_result_body(parsed)
        if is_error or body is None:
            record(FAIL, f"{name}: skill_evidence (real tag)", body or "unparseable response")
        elif body.get("found") is True and body.get("count", 0) > 0:
            record(PASS, f"{name}: skill_evidence (real tag)", f"count={body.get('count')}")
        else:
            record(FAIL, f"{name}: skill_evidence (real tag)", f"expected found=true/count>0, got {body}")

    status, parsed, err = jsonrpc_call(
        url, "tools/call", {"name": "skill_evidence", "arguments": {"tag": "no_such_tag_ever_xyz"}})
    if err or not parsed:
        record(FAIL, f"{name}: skill_evidence (unknown tag)", err or "no response")
    else:
        body, is_error = tool_result_body(parsed)
        if is_error:
            record(FAIL, f"{name}: skill_evidence (unknown tag)", f"errored instead of found:false -- {body}")
        elif body and body.get("found") is False:
            record(PASS, f"{name}: skill_evidence (unknown tag)", "found=false as expected")
        else:
            record(FAIL, f"{name}: skill_evidence (unknown tag)", f"expected found=false, got {body}")


def check_private_worker_auth_gate(name: str, url: str) -> None:
    print(f"\n== {name} (private) -- {url} ==")
    status, _, err = jsonrpc_call(url, "tools/list")
    if status == 401:
        record(PASS, f"{name}: auth gate", "401 with no token, as expected")
    else:
        record(FAIL, f"{name}: auth gate", f"expected 401, got {status or err} -- SECURITY ISSUE if this is live")


def load_local_registry() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"\n(( {REGISTRY_PATH} exists but isn't valid JSON -- skipping it ))")
        return []


def discover_via_d1() -> list[dict]:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    token = os.environ.get("CLOUDFLARE_D1_TOKEN")
    if not account_id or not token:
        return []
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{D1_DATABASE_ID}/query"
    body = json.dumps({
        "sql": "SELECT mcp_url_public, mcp_url_private FROM upload_invites "
               "WHERE deleted_at IS NULL AND (mcp_url_public IS NOT NULL OR mcp_url_private IS NOT NULL)"
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"\n(( D1 discovery failed ({e}) -- skipping dynamic customer discovery ))")
        return []
    rows = result.get("result", [{}])[0].get("results", [])
    out = []
    for row in rows:
        if row.get("mcp_url_public"):
            out.append({"label": "customer (via D1)", "url": row["mcp_url_public"], "tier": "public"})
        if row.get("mcp_url_private"):
            out.append({"label": "customer (via D1)", "url": row["mcp_url_private"], "tier": "private"})
    return out


def main() -> None:
    local_only = "--local-only" in sys.argv

    check_local_data_integrity()

    if not local_only:
        check_public_worker("personalknowhow-demo", DEMO_URL)

        registry = load_local_registry()
        if registry:
            print(f"\n(( loaded {len(registry)} Worker(s) from {REGISTRY_PATH} ))")
        else:
            print(f"\n(( no local registry at {REGISTRY_PATH} -- create one to test your own/customer "
                  "Workers, e.g. [{\"label\": \"self-test\", \"url\": \"https://...\", \"tier\": \"public\"}] ))")

        discovered = discover_via_d1()
        if discovered:
            print(f"(( discovered {len(discovered)} Worker(s) via live D1 lookup ))")
        elif not os.environ.get("CLOUDFLARE_D1_TOKEN"):
            print("(( CLOUDFLARE_D1_TOKEN not set -- skipping live customer discovery via D1 ))")

        seen_urls = set()
        for w in registry + discovered:
            if w["url"] in seen_urls:
                continue
            seen_urls.add(w["url"])
            if w["tier"] == "public":
                check_public_worker(w["label"], w["url"])
            else:
                check_private_worker_auth_gate(w["label"], w["url"])
    else:
        print("\n(( --local-only: skipping all live HTTP checks ))")

    failed = [r for r in _results if r[0] == FAIL]
    passed = [r for r in _results if r[0] == PASS]
    skipped = [r for r in _results if r[0] == SKIP]
    print(f"\n{'=' * 60}\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped\n{'=' * 60}")
    if failed:
        print("\nFAILURES:")
        for _, name, detail in failed:
            print(f"  - {name}: {detail}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
