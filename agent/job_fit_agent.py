#!/usr/bin/env python3
"""
Compare a job posting against PKH's private knowledge graph and produce a
grounded fit report.

Uses the Anthropic Messages API's native MCP connector to call the already-
deployed personalknowhow-private Cloudflare Worker directly -- the model
decides which requirements to look up and how many times, server-side, with
no local tool-execution loop. This is the genuinely agentic part: the number
and content of the query_knowhow calls depend on what the posting actually
says, not a fixed script sequence.

Read-only end to end. The only file this script writes is the report itself.
"""
import argparse
import datetime
import http.client
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import anthropic

ROOT = Path(__file__).parent.parent
REPORTS_DIR = ROOT / "data" / "job_fit_reports"

PRIVATE_MCP_URL = os.environ.get(
    "PRIVATE_MCP_URL", "https://personalknowhow-private.kxtwrdzt6g.workers.dev/mcp"
)
MODEL = "claude-opus-5"
MCP_BETA = "mcp-client-2025-11-20"
MAX_PAUSE_RETRIES = 6
FETCH_TIMEOUT_SECONDS = 15
MIN_FETCHED_TEXT_CHARS = 200
LINKEDIN_HOSTS = ("linkedin.com",)

SYSTEM_PROMPT = """You are assessing how well a job posting fits a candidate's real, \
demonstrated background, grounded entirely in the personalknowhow-private MCP \
server -- never in prior knowledge or assumption.

Process:
1. Read the job posting and list every distinct required skill, technology, \
qualification, or experience area it names.
2. For EACH one separately, call query_knowhow with that specific term (not one \
vague query for the whole posting). Use list_by_type when a requirement is best \
checked exhaustively (e.g. "do they hold any certification in X").
3. A result with "found": true is NOT automatically a match. Read each result's \
actual label, description, and type before counting it as evidence -- the \
similarity score only means "closest thing available," not "actually about \
this." A real example from this exact tool: querying "Hono" (a TypeScript web \
framework) returned a FastAPI course and an Apache Spark endorsement, both \
scoring just above the tool's floor -- neither is Hono experience, they're \
semantically adjacent (also backend/framework-shaped) but topically wrong. If \
a returned result isn't genuinely about the specific requirement you're \
checking, treat that requirement as unmatched -- do not report it as evidence \
just because the tool returned something.
4. Every result carries an evidence_tier field. NEVER treat a signal_only entry \
(career_interest, job_application, saved_job_alert, job_seeker_preferences, \
saved_answer) as proof of ability -- it shows interest or activity, not skill. \
You may mention a signal_only hit separately as a note, clearly labeled, but it \
must never count toward the fit verdict.
5. Once every requirement has been checked, write a Markdown report with this \
exact structure:

# Job Fit Report: <role/company from the posting, best guess if not explicit>

## Verdict
One of: Strong fit / Moderate fit / Weak fit -- with 1-2 sentences of justification.

## Matched requirements
For each requirement with demonstrated evidence: the requirement, the matching \
corpus entry's label, its type, and its similarity score.

## Gaps
Requirements with no demonstrated evidence (score below the tool's own floor, or \
no query result at all).

## Signal only (not counted)
Any signal_only hits surfaced along the way, if relevant -- omit this section if none.

Ground every claim in an actual tool result. If a requirement returns no evidence, \
say so plainly in Gaps rather than guessing."""


_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


class _TextExtractor(HTMLParser):
    """Strips a page down to its visible text -- drops script/style content,
    breaks lines at block-level tags only (so inline tags like <b>/<i> don't
    fragment a sentence across multiple lines)."""

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    text = "".join(extractor.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _plain_fetch(url: str) -> str:
    """Fast path: a direct GET, no third party involved. Works fine for
    ordinary server-rendered pages; comes back empty/too-short for
    JS-rendered or bot-throttled ones (the length check in fetch_url is what
    decides whether to fall through to the reader-proxy fallback)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except http.client.IncompleteRead as e:
        # Some sites stream/stall and never finish sending the response (seen
        # live against a Next.js careers page behind Cloudflare -- inconsistent
        # byte counts across identical requests, consistent with anti-bot
        # throttling of non-browser clients). Salvage whatever partial bytes
        # arrived rather than treating this as a hard failure -- the fallback
        # below is what actually recovers a page like that.
        raw = e.partial.decode("utf-8", errors="replace")
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError):
        return ""
    return html_to_text(raw)


def _reader_proxy_fetch(url: str) -> str:
    """Fallback for JS-rendered or bot-throttled pages a plain GET can't get
    through: r.jina.ai runs a real browser server-side and returns clean
    text. Still just an HTTP GET on this end -- no local browser binaries,
    so it works the same regardless of what machine runs this script (unlike
    a local Playwright/headless-browser approach, which only works wherever
    that's installed -- never on a phone, and not portably even on a laptop).
    The target URL (not corpus data) passes through this third party."""
    req = urllib.request.Request(
        "https://r.jina.ai/" + url, headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS * 2) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError):
        return ""


def _normalize_url(url: str) -> str:
    """Percent-encode non-ASCII characters so urllib/http.client (which require
    a pure-ASCII request line) don't crash on real-world URLs -- Nordic company
    career sites in particular commonly put local place names directly in the
    path (e.g. Smørum), unencoded."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        host = host.encode("idna").decode("ascii")
    netloc = host + (f":{parts.port}" if parts.port else "")
    path = urllib.parse.quote(parts.path, safe="/%")
    query = urllib.parse.quote(parts.query, safe="=&%")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, parts.fragment))


def fetch_url(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(host == h or host.endswith("." + h) for h in LINKEDIN_HOSTS):
        sys.exit(
            "Refusing to auto-fetch a LinkedIn URL: the full posting is behind a "
            "login wall anyway, and this project never makes automated requests "
            "against LinkedIn on a real account (job data here is always collected "
            "interactively). Paste the posting text into a file instead."
        )

    url = _normalize_url(url)
    text = _plain_fetch(url)
    if len(text) < MIN_FETCHED_TEXT_CHARS:
        print(
            f"Direct fetch got only {len(text)} chars -- trying the reader-proxy "
            "fallback for JS-rendered/protected pages...",
            file=sys.stderr,
        )
        text = _reader_proxy_fetch(url)

    if len(text) < MIN_FETCHED_TEXT_CHARS:
        sys.exit(
            f"Could not get usable content from {url} -- neither a direct fetch "
            "nor the reader-proxy fallback worked. Paste the posting text into a "
            "file instead."
        )
    return text


def read_posting(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    if path.startswith("http://") or path.startswith("https://"):
        return fetch_url(path)
    return Path(path).read_text(encoding="utf-8")


def trace_response(response) -> None:
    """Print any tool-call-shaped content block so the chaining is visible."""
    for block in response.content:
        btype = getattr(block, "type", "?")
        if "mcp" in btype or "tool_use" in btype:
            name = getattr(block, "name", None)
            inp = getattr(block, "input", None)
            print(f"  [tool call] {btype} name={name!r} input={inp!r}", file=sys.stderr)


def run_agent(posting_text: str, token: str) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": f"Job posting:\n\n{posting_text}"}]
    mcp_servers = [{
        "type": "url",
        "url": PRIVATE_MCP_URL,
        "name": "personalknowhow-private",
        "authorization_token": f"Bearer {token}",
    }]
    tools = [{"type": "mcp_toolset", "mcp_server_name": "personalknowhow-private"}]

    response = None
    for _ in range(MAX_PAUSE_RETRIES):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=8000,
            betas=[MCP_BETA],
            system=SYSTEM_PROMPT,
            mcp_servers=mcp_servers,
            tools=tools,
            messages=messages,
        )
        trace_response(response)
        if response.stop_reason == "pause_turn":
            messages = messages[:1] + [{"role": "assistant", "content": response.content}]
            continue
        break
    else:
        raise RuntimeError("Gave up after repeated pause_turn -- posting may be too complex")

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined the request: {response.stop_details}")

    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def save_report(text: str, posting_path: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", Path(posting_path).stem.lower()).strip("_") or "posting"
    date = datetime.date.today().isoformat()
    out_path = REPORTS_DIR / f"{slug}_{date}.md"
    if out_path.exists():
        out_path = REPORTS_DIR / f"{slug}_{date}_{datetime.datetime.now():%H%M%S}.md"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "posting",
        help="Path to a text file with the job posting, a job URL (not LinkedIn -- see README), or '-' for stdin",
    )
    args = parser.parse_args()

    token = os.environ.get("PRIVATE_MCP_TOKEN")
    if not token:
        sys.exit(
            "PRIVATE_MCP_TOKEN is not set. The raw token is in mcp-private/URLs.txt "
            "-- export it: export PRIVATE_MCP_TOKEN=<token>"
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set.")

    posting_text = read_posting(args.posting)
    print(f"Assessing fit against {len(posting_text)} chars of posting text...", file=sys.stderr)
    report = run_agent(posting_text, token)
    out_path = save_report(report, args.posting)

    print(report)
    print(f"\nSaved to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
