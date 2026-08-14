#!/usr/bin/env python3
"""
Pull course/certification completion emails AND job application emails from
Gmail and write them into corpus/gmail/ using the common PKH schema.

Required environment variables (set as GitHub Secrets):
  GMAIL_CLIENT_ID
  GMAIL_CLIENT_SECRET
  GMAIL_REFRESH_TOKEN

Tracks last run in data/last_gmail_check.txt so only new emails are processed.
"""
import base64
import email.utils
import html.parser
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import TAG_PATTERNS as _SHARED_TAG_PATTERNS  # noqa: E402

ROOT = Path(__file__).parent.parent

try:
    import yaml
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
except ImportError:
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "pyyaml", "google-api-python-client", "google-auth-httplib2", "google-auth-oauthlib",
         "-q", "--break-system-packages"],
        check=True,
    )
    import yaml
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Provider detection: sender domain → canonical provider name
# ---------------------------------------------------------------------------
PROVIDER_DOMAINS: dict[str, str] = {
    "linkedin.com": "LinkedIn Learning",
    "e.linkedin.com": "LinkedIn Learning",
    "email.linkedin.com": "LinkedIn Learning",
    "coursera.org": "Coursera",
    "m.coursera.org": "Coursera",
    "masterclass.com": "MasterClass",
    "email.masterclass.com": "MasterClass",
    "bbcmaestro.com": "BBC Maestro",
    "datacamp.com": "DataCamp",
    "email.datacamp.com": "DataCamp",
    "udemy.com": "Udemy",
    "email.udemy.com": "Udemy",
    "pluralsight.com": "Pluralsight",
    "edx.org": "edX",
    "skillshare.com": "Skillshare",
    "codecademy.com": "Codecademy",
    "brilliant.org": "Brilliant",
    "khanacademy.org": "Khan Academy",
    "futurelearn.com": "FutureLearn",
}

# Gmail query — broad enough to catch variations across providers
GMAIL_QUERY = (
    "("
    "from:linkedin.com OR from:e.linkedin.com OR "
    "from:coursera.org OR from:m.coursera.org OR "
    "from:masterclass.com OR from:email.masterclass.com OR "
    "from:bbcmaestro.com OR "
    "from:datacamp.com OR from:email.datacamp.com OR "
    "from:udemy.com OR from:email.udemy.com OR "
    "from:pluralsight.com OR from:edx.org OR "
    "from:skillshare.com OR from:codecademy.com"
    ") "
    "("
    "subject:completed OR subject:certificate OR "
    "subject:congratulations OR subject:earned OR "
    "subject:\"course completion\" OR subject:finished OR "
    "subject:\"statement of accomplishment\""
    ")"
)

# ---------------------------------------------------------------------------
# Tag inference from title keywords
# ---------------------------------------------------------------------------
# Merged with ingest/common.py's shared vocabulary (databricks, dbt, claude,
# llm, mcp, etc.) rather than replacing this list outright -- gmail_sync
# covers non-tech providers too (MasterClass, Skillshare, Khan Academy) whose
# tags (photography, music, writing, marketing) common.py doesn't carry.
TAG_PATTERNS: list[tuple[str, list[str]]] = _SHARED_TAG_PATTERNS + [
    ("python", ["python"]),
    ("javascript", ["javascript", "typescript", "node.js", "nodejs", "react", "vue", "angular"]),
    ("data-science", ["data science", "data analysis", "analytics", "statistics"]),
    ("machine-learning", ["machine learning", "deep learning", "neural network", "tensorflow",
                          "pytorch", "artificial intelligence", " ai ", " nlp"]),
    ("sql", ["sql", "database", "postgres", "mysql", "snowflake", "bigquery"]),
    ("visualization", ["visualization", "tableau", "power bi", "matplotlib", "seaborn", "plotly"]),
    ("web-development", ["web development", "html", "css", "frontend", "backend", "full-stack",
                         "fullstack", "django", "flask", "fastapi"]),
    ("devops", ["devops", "docker", "kubernetes", "ci/cd", "deployment", "cloud", "aws", "azure",
                "gcp", "terraform", "github actions"]),
    ("git", ["git", "github", "gitlab", "version control"]),
    ("excel", ["excel", "spreadsheet", "google sheets"]),
    ("design", ["design", " ux", " ui ", "figma", "photoshop", "illustrator", "canva"]),
    ("leadership", ["leadership", "management", "agile", "scrum", "product management"]),
    ("communication", ["communication", "writing", "presentation", "public speaking"]),
    ("finance", ["finance", "accounting", "investment", "economics", "fintech"]),
    ("marketing", ["marketing", "seo", "social media", "content strategy", "copywriting"]),
    ("r-lang", [" r ", "rstudio", "tidyverse", "ggplot"]),
    ("scala", ["scala", "spark", "hadoop"]),
    ("security", ["security", "cybersecurity", "penetration testing", "encryption"]),
    ("photography", ["photography", "lightroom", "photoshop", "videography"]),
    ("music", ["music", "guitar", "piano", "songwriting", "production"]),
    ("writing", ["writing", "storytelling", "screenwriting", "fiction"]),
]


def infer_tags(title: str) -> list[str]:
    title_lower = title.lower()
    tags = []
    for tag, keywords in TAG_PATTERNS:
        if any(kw in title_lower for kw in keywords) and tag not in tags:
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# Body → title extraction
# ---------------------------------------------------------------------------
# Many providers (confirmed: DataCamp) use a generic subject line ("Your
# Statement of Accomplishment has Arrived", "Congratulations on completing
# your track!") with the actual course/track name only appearing in the
# body. Subject-only extraction silently produces the wrong title for these
# -- body extraction must be tried first, subject is the fallback.
BODY_TITLE_PATTERNS = [
    r'congratulations!?\s+you have completed the\s+(.+?)\s*track!',
    r'congratulations!?\s+you finished\s+(.+?)[\s\n]*(?:share your|$)',
    r'congrats on completing\s+(.+?)!',
]


def extract_title_from_text(text: str) -> str:
    text_norm = re.sub(r'\s+', ' ', text.strip())
    for pattern in BODY_TITLE_PATTERNS:
        m = re.search(pattern, text_norm, re.IGNORECASE)
        if m:
            title = m.group(1).strip().rstrip("!.,;")
            if len(title) > 3:
                return title
    return ""


# ---------------------------------------------------------------------------
# Subject → title extraction (fallback when body extraction finds nothing)
# ---------------------------------------------------------------------------
TITLE_PATTERNS = [
    r'congratulations[!,]?\s+you(?:\'ve)?\s+(?:completed?|finished)\s+["\']?(.+?)["\']?[!.]?\s*$',
    r'you(?:\'ve)?\s+(?:completed?|earned|finished)\s+["\']?(.+?)["\']?[!.]?\s*$',
    r'you earned a new skill[!:]\s*["\']?(.+?)["\']?[!.]?\s*$',
    r'your certificate (?:for|in|of)\s+["\']?(.+?)["\']?[!.]?\s*$',
    r'certificate of completion[:\s]+["\']?(.+?)["\']?[!.]?\s*$',
    r'["\'](.+?)["\'][\s\-–]+certificate',
    r'you completed\s+["\']?(.+?)["\']?[!.]?\s*$',
    r'completed[:\s]+["\']?(.+?)["\']?[!.]?\s*$',
]


def extract_title_from_subject(subject: str) -> str:
    subject_clean = subject.strip()
    for pattern in TITLE_PATTERNS:
        m = re.search(pattern, subject_clean, re.IGNORECASE)
        if m:
            title = m.group(1).strip().rstrip("!.,;")
            if len(title) > 3:
                return title
    # Fallback: strip common prefixes
    prefixes = [
        r"^congratulations[!,]?\s*",
        r"^you(?:'ve)?\s+(?:completed?|earned|finished)\s+",
        r"^certificate of completion[:\s]+",
        r"^your certificate\s+",
        r"^re:\s*",
    ]
    result = subject_clean
    for prefix in prefixes:
        result = re.sub(prefix, "", result, flags=re.IGNORECASE).strip()
    return result.rstrip("!.,;").strip() or subject_clean


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _get_sender_domain(headers: list[dict]) -> str:
    for h in headers:
        if h["name"].lower() == "from":
            m = re.search(r"@([\w.\-]+)", h["value"])
            if m:
                return m.group(1).lower()
    return ""


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode_body(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    mime = part.get("mimeType", "")
    if "html" in mime:
        class _S(html.parser.HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []
            def handle_data(self, d):
                self.text.append(d)
        s = _S()
        s.feed(raw)
        return " ".join(s.text)
    return raw


def get_message_text(msg: dict) -> str:
    payload = msg.get("payload", {})
    if payload.get("body", {}).get("data"):
        return _decode_body(payload)
    for part in payload.get("parts", []):
        if part.get("mimeType") in ("text/plain", "text/html"):
            return _decode_body(part)
    return ""


# ---------------------------------------------------------------------------
# Corpus writer
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:55]


def write_corpus_entry(title: str, provider: str, date_str: str, description: str,
                       domain_tags: list[str], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_slug(title)}_{date_str.replace('-', '')}.md"
    path = out_dir / filename
    # Idempotent: skip if already exists (merge.py handles dedup across sources)
    if path.exists():
        return path
    fm = {
        "title": title,
        "type": "course",
        "provider": provider,
        "date": date_str,
        "description": description,
        "domain_tags": domain_tags,
    }
    fm_yaml = yaml.dump(fm, allow_unicode=True, sort_keys=True, default_flow_style=False)
    path.write_text(f"---\n{fm_yaml}---\n\n{description}\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def build_gmail_service() -> object:
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def load_last_checked() -> str:
    ts_file = ROOT / "data" / "last_gmail_check.txt"
    if ts_file.exists():
        ts = ts_file.read_text().strip()
        if ts:
            return ts
    return "2020-01-01T00:00:00Z"


def save_last_checked(ts: str) -> None:
    ts_file = ROOT / "data" / "last_gmail_check.txt"
    ts_file.parent.mkdir(parents=True, exist_ok=True)
    ts_file.write_text(ts + "\n", encoding="utf-8")


def _gmail_date(iso_ts: str) -> str:
    """Convert ISO timestamp to Gmail query date format YYYY/MM/DD."""
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return dt.strftime("%Y/%m/%d")


def sync(dry_run: bool = False) -> int:
    last_checked = load_last_checked()
    print(f"Checking Gmail for completions since {last_checked} ...")

    service = build_gmail_service()
    query = GMAIL_QUERY + f" after:{_gmail_date(last_checked)}"
    print(f"Query: {query}")

    results = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = results.get("messages", [])
    print(f"Found {len(messages)} matching emails.")

    added = 0
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="full"
        ).execute()

        headers = msg["payload"].get("headers", [])
        subject = _get_header(headers, "subject")
        date_raw = _get_header(headers, "date")
        sender_domain = _get_sender_domain(headers)

        provider = PROVIDER_DOMAINS.get(sender_domain, "")
        if not provider:
            continue  # not from a known course platform

        body_text = get_message_text(msg)
        title = extract_title_from_text(body_text) or extract_title_from_subject(subject)
        if not title or len(title) < 4:
            print(f"  Skipping (no title): {subject!r}")
            continue

        # Parse date
        try:
            dt = email.utils.parsedate_to_datetime(date_raw)
            date_str = dt.strftime("%Y-%m-%d")
        except Exception:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        domain_tags = infer_tags(title)
        description = f"Completed via {provider}."

        out_path = ROOT / "corpus" / "gmail"
        if dry_run:
            print(f"  [dry-run] Would add: {title!r} ({provider}, {date_str}) tags={domain_tags}")
            continue

        path = write_corpus_entry(title, provider, date_str, description, domain_tags, out_path)
        print(f"  + {path.name}  [{provider}]  {title!r}")
        added += 1

    # Update last_checked to now
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not dry_run:
        save_last_checked(now)
        print(f"Updated last_gmail_check.txt → {now}")

    print(f"Gmail sync complete — {added} new items added.")
    return added


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Sync Gmail course completion emails to corpus/.")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    args = p.parse_args()

    missing = [v for v in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
               if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        print("See README.md for Gmail API setup instructions.")
        sys.exit(1)

    sync(dry_run=args.dry_run)
