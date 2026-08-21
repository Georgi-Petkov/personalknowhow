"""Shared helpers for source-specific ingest scripts."""
import base64
import html.parser
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "pyyaml", "-q",
                   "--break-system-packages"], check=True)
    import yaml

GRAPH_FILE = Path(__file__).parent.parent / "graph.json"

TAG_PATTERNS: list[tuple[str, list[str]]] = [
    ("python",           ["python", "numpy", "pandas", "jupyter"]),
    ("r-lang",           [" r ", "in r ", "with r ", "tidyverse", "ggplot", "dplyr",
                          "tidyr", "purrr", "shiny", "rstudio", "rcpp", "caret"]),
    ("sql",              ["sql", "postgres", "mysql", "database", "relational database",
                          "sql server", "mongodb", "nosql"]),
    ("scala",            ["scala"]),
    ("spark",            ["spark", "pyspark", "sparklyr"]),
    ("data-science",     ["data science", "data analysis", "eda", "exploratory data",
                          "data manipulation", "data wrangling", "cleaning data",
                          "big data", "data quality", "data modeling", "data analytics"]),
    ("machine-learning", ["machine learning", "supervised learning", "unsupervised learning",
                          "classification", "regression", "clustering", "scikit-learn",
                          "sklearn", "xgboost", "gradient boosting", "random forest",
                          "decision tree", "hyperparameter", "model validation",
                          "feature engineering", "preprocessing", "predictive model"]),
    ("deep-learning",    ["deep learning", "neural network", "keras", "tensorflow",
                          "image modeling", "cnn"]),
    ("nlp",              ["nlp", "natural language", "text mining", "sentiment analysis",
                          "spacy", "bag-of-words", "text analysis", "feature engineering for nlp"]),
    ("visualization",    ["visualization", "matplotlib", "seaborn", "plotly", "ggplot",
                          "dashboard", "power bi", "tableau", "trelliscope", "leaflet",
                          "geospatial", "maps"]),
    ("statistics",       ["statistics", "statistical", "inference", "probability",
                          "bayesian", "hypothesis", "a/b testing", "simulation",
                          "statistical thinking", "regression"]),
    ("time-series",      ["time series", "arima", "garch", "forecasting", "temporal",
                          "dates and times", "manipulating time"]),
    ("finance",          ["finance", "financial", "trading", "portfolio", "risk management",
                          "credit risk", "quantitative", "investment", "forecasting",
                          "financial planning"]),
    ("power-bi",         ["power bi", "dax", "power query"]),
    ("cloud",            ["aws", "cloud", "boto", "kinesis", "lambda", "azure", "fabric",
                          "databricks", "gcp", "google cloud"]),
    ("aws",              ["aws", "amazon web services", "boto", "kinesis", " s3 ", "ec2",
                          " lambda ", "amazon rds"]),
    ("azure",            ["azure", "microsoft fabric"]),
    ("gcp",              ["gcp", "google cloud", "bigquery"]),
    ("databricks",       ["databricks", "delta lake", "lakehouse", "unity catalog"]),
    ("dbt",              [" dbt ", "dbt-core", "dbt cloud", "dbt project"]),
    ("kubernetes",       ["kubernetes", "k8s", "kubectl", "helm chart"]),
    ("docker",           ["docker", "dockerfile", "docker-compose", "container image"]),
    ("terraform",        ["terraform", "infrastructure as code", ".tf ", "hcl"]),
    ("ci-cd",            ["ci/cd", "continuous integration", "continuous deployment",
                          "github actions", "gitlab ci", "circleci", "jenkins"]),
    ("snowflake",        ["snowflake"]),
    ("airflow",          ["airflow", "dag scheduling", "apache airflow"]),
    ("kafka",            ["kafka", "apache kafka"]),
    ("data-governance",  ["data governance", "data quality", "data steward",
                          "great expectations"]),
    ("sap",              ["sap bw", "sap analysis", " sap "]),
    ("data-engineering", ["data engineering", "airflow", "etl", "pipeline", "dbt",
                          "delta lake", "lakehouse", "streaming", "ingestion",
                          "bash scripting", "shell", "data processing", "data lake",
                          "glue", "automation"]),
    ("bi",               ["power bi", "dax", "reporting", "dashboard", "business intelligence",
                          "business data", "financial statements", "key performance indicator"]),
    ("marketing",        ["marketing", "customer segmentation", "churn", "customer analytics",
                          "recommendation", "market basket", "google analytics",
                          "google tag manager", "web analytics", "campaign",
                          "customer journey", "retention strateg", "product launch",
                          "market analysis"]),
    ("excel",            ["excel", "spreadsheet", "microsoft office", "pivot table"]),
    ("tableau",          ["tableau"]),
    ("web-development",  ["api", "fastapi", "web scraping", "scrapy", "flask"]),
    ("javascript",       ["javascript", "typescript", "react", "angular", "vue", "node.js"]),
    ("devops",           ["devops", "docker", "kubernetes", "ci/cd", "terraform", "github actions"]),
    ("project-management", ["project management", "project planning", "agile", "scrum", "jira",
                          "product management", "business requirement", "stakeholder management",
                          "business process"]),
    ("leadership",       ["leader", "management", "team lead", " led ", "directed",
                          "strateg", "teamwork", "business development"]),
    ("communication",    ["communication", "writing", "presentation", "public speaking",
                          "storytelling", "business writing", "english", "interpersonal",
                          "powerpoint"]),
    ("analytical-thinking", ["analytical skills", "analytical thinking", "critical thinking",
                          "problem solving", "problem-solving"]),
    ("design",           ["design", "ux", " ui ", "figma", "photoshop", "illustrator", "canva",
                          "typography", "color"]),
    ("security",         ["security", "cybersecurity", "encryption", "gdpr", "anonymi"]),
    ("photography",      ["photography", "lightroom", "videography"]),
    ("music",            ["music", "guitar", "piano", "songwriting"]),
    ("pharma",           ["pharma", "biopharma", "clinical", "biotech", "leo pharma", "life science"]),
    ("bayesian",         ["bayesian", "probabilistic", "mcmc"]),
    ("probability",      ["probability", "statistics", "stochastic"]),
    ("git",              ["git", "bash", "shell", "command-line"]),
    ("claude",           ["claude", "anthropic"]),
    ("llm",              ["llm", "large language model", "generative ai", "genai"]),
    ("agents",           ["agent", "subagent", "multi-agent", "cowork"]),
    ("mcp",              ["model context protocol", "mcp server", "mcp client", " mcp "]),
    ("prompt-engineering", ["prompt", "prompting"]),
    ("ai-fluency",       ["ai fluency", "responsible ai", "ai literacy"]),
]


def infer_tags(text: str) -> list[str]:
    t = " " + text.lower() + " "
    return [tag for tag, kws in TAG_PATTERNS if any(kw in t for kw in kws)]


# Generic role-title fallback tags. LinkedIn's Positions.csv export has an
# empty Description for every row, so infer_tags(title + company) usually
# finds nothing -- "Data Scientist" doesn't hit any TAG_PATTERNS keyword,
# which was silently making real job history invisible to any topic search
# while auto-discovered personal projects (rich README/code text) dominated.
# These are coarse, title-only tags -- real per-job tool evidence comes from
# load_position_notes() below, not from this fallback.
ROLE_TAG_PATTERNS: list[tuple[str, list[str]]] = [
    ("data-science",     ["data scientist"]),
    ("machine-learning", ["data scientist"]),
    ("statistics",       ["data scientist"]),
    ("data-engineering", ["data engineer"]),
    ("sql",              ["data engineer", "data analyst"]),
    ("bi",               ["data analyst", "bi analyst", "sfe analyst",
                          "business intelligence analyst"]),
    ("visualization",    ["bi analyst", "sfe analyst"]),
]


def infer_role_tags(title: str) -> list[str]:
    t = " " + title.lower() + " "
    return [tag for tag, kws in ROLE_TAG_PATTERNS if any(kw in t for kw in kws)]


POSITION_NOTES_FILE = Path(__file__).parent.parent / "data" / "position_notes.json"


def load_position_notes() -> dict[str, str]:
    """Hand-authored per-job notes, keyed by 'title @ company' (lowercased).

    LinkedIn's Positions.csv export has no real description text for any
    position -- this file is the only source of what was actually done
    (tools, responsibilities) at each job, same idiom as data/edx_completed.json
    and cv/master_cv.md: content that doesn't exist in any export, so a human
    authors it directly rather than it being derived.
    """
    if not POSITION_NOTES_FILE.exists():
        return {}
    entries = json.loads(POSITION_NOTES_FILE.read_text(encoding="utf-8"))
    return {
        f"{e['title'].strip().lower()} @ {e['company'].strip().lower()}": e.get("notes", "").strip()
        for e in entries
        if e.get("notes", "").strip()
    }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


# ---------------------------------------------------------------------------
# Gmail message parsing — used by linkedin_export_email.py
# ---------------------------------------------------------------------------
# gmail_sync.py has its own private copies of these three (_get_header,
# _decode_body, get_message_text) for a different Gmail-polling flow
# (non-interactive CI OAuth, course-completion emails). Deliberately not
# importing from gmail_sync.py here and not changing gmail_sync.py to import
# from here either -- it's a working, independently-scheduled script solving
# a different problem, not worth coupling to a new one.

def get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def decode_email_body(part: dict) -> str:
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
        return decode_email_body(payload)
    for part in payload.get("parts", []):
        if part.get("mimeType") in ("text/plain", "text/html"):
            return decode_email_body(part)
    return ""


LANGUAGES_DIR = Path(__file__).parent.parent / "corpus" / "linkedin" / "languages"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# LinkedIn's LANGUAGES domain returns free-text language names ("Danish",
# "English"), not ISO codes -- this maps the common ones to ISO 639-1 so they
# can be compared against langdetect's output. Not exhaustive; an unmapped
# name falls back to a lowercase substring match in known_language_codes().
LANGUAGE_NAME_TO_CODE: dict[str, str] = {
    "english": "en", "danish": "da", "swedish": "sv", "norwegian": "no",
    "german": "de", "dutch": "nl", "french": "fr", "spanish": "es",
    "italian": "it", "portuguese": "pt", "polish": "pl", "russian": "ru",
    "ukrainian": "uk", "bulgarian": "bg", "romanian": "ro", "greek": "el",
    "turkish": "tr", "czech": "cs", "slovak": "sk", "finnish": "fi",
    "hungarian": "hu", "chinese": "zh-cn", "japanese": "ja", "korean": "ko",
    "arabic": "ar", "hindi": "hi", "hebrew": "he", "vietnamese": "vi",
    "thai": "th", "indonesian": "id",
}


def known_languages() -> list[str]:
    """Language names the user has listed on LinkedIn (LANGUAGES API domain,
    corpus/linkedin/languages/*.md -- see ingest/linkedin_api_ingest.py).
    Empty until the user adds Languages on their profile and re-runs
    ingestion; no hardcoded fallback here on purpose -- this must come from
    the API, not a guess."""
    if not LANGUAGES_DIR.exists():
        return []
    names = []
    for md_file in sorted(LANGUAGES_DIR.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue
        fm = yaml.safe_load(m.group(1)) or {}
        title = str(fm.get("title", "")).strip()
        if title:
            names.append(title)
    return names


def known_language_codes() -> set[str]:
    """known_languages() normalized to ISO 639-1 codes for comparison against
    langdetect output. Falls back to the lowercased name itself for anything
    not in LANGUAGE_NAME_TO_CODE, so an unmapped language still gets a
    reasonable shot at matching rather than being silently dropped."""
    codes = set()
    for name in known_languages():
        key = name.strip().lower()
        codes.add(LANGUAGE_NAME_TO_CODE.get(key, key))
    return codes


# ---------------------------------------------------------------------------
# Semantic coverage (replaces exact-tag-match "known coverage" lookups)
# ---------------------------------------------------------------------------
# Corrected 2026-08-10: analyze_job_postings.py originally answered "how much
# known coverage does skill X have" via TAG_PATTERNS substring matching --
# structurally binary (a course either has the exact tag or doesn't) even
# though real coverage is graded (e.g. a Databricks data-management course
# touches data governance concepts without ever using that exact phrase).
# The corpus already has real embeddings computed for the MCP servers'
# semantic search (mcp-private/private_embeddings.json) -- this reuses that
# same model/vectors instead of adding a second, inconsistent matching
# mechanism. SIM_FLOOR/PERIPHERAL_FLOOR mirror mcp-private/src/index.ts's own
# empirically-tuned 0.65 "real match" floor for this exact model, plus a
# looser second tier for exactly the "some exposure, not exact-tag-worthy"
# case that motivated this change.
PRIVATE_ENTRIES_FILE = Path(__file__).parent.parent / "mcp-private" / "private_entries.json"
PRIVATE_EMBEDDINGS_FILE = Path(__file__).parent.parent / "mcp-private" / "private_embeddings.json"
SKILL_EMBEDDINGS_FILE = Path(__file__).parent.parent / "data" / "skill_embeddings.json"

# Evidence types that represent real exposure (formal or hands-on) to a
# skill. Deliberately excludes career_interest/job_application/saved_answer/
# saved_job_alert/job_seeker_preferences -- those are intent/application
# signal, not proof of having actually learned or used something (same
# evidence_tier distinction mcp-private/src/index.ts already makes).
DEMONSTRATED_TYPES = {
    "course", "project", "certification", "education", "position", "profile",
    "recommendation", "article", "endorsement", "organization", "language",
    "honor", "publication", "patent", "volunteering", "test_score",
    "skill_assessment", "track",
}

SIM_FLOOR = 0.65        # "known" -- same floor validated live against this model/corpus
PERIPHERAL_FLOOR = 0.50  # "peripheral" -- related but not solid coverage

# Human-readable explanation shown in place of a source_url when a node's
# type has no real URL field in its corpus frontmatter (confirmed empty for
# these types across every ingest script) -- so a disputed claim gets an
# honest "here's why there's no link" instead of silence. Shared by
# build_public_export.py and build_private_export.py's build_entries().
SOURCE_NOTE_BY_TYPE = {
    "endorsement": "LinkedIn peer endorsement — LinkedIn's export doesn't include a direct "
                    "link to the endorsement or endorser.",
    "position": "LinkedIn work-history entry — LinkedIn's export doesn't include a direct link "
                "to the position listing.",
    "recommendation": "LinkedIn peer recommendation — LinkedIn's export doesn't include a "
                       "direct link to the recommendation.",
    "education": "LinkedIn education entry — LinkedIn's export doesn't include a direct link "
                 "to the school/program listing.",
    "profile": "LinkedIn profile summary — not a linkable item on its own.",
}
DEFAULT_SOURCE_NOTE = "No direct source link available for this entry."


def _b64_to_vec(b64: str) -> list[float]:
    raw = base64.b64decode(b64)
    n = len(raw) // 4
    import struct
    return list(struct.unpack(f"<{n}f", raw))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def load_semantic_corpus() -> tuple[list[dict], dict[str, list[float]]]:
    """Demonstrated-evidence entries + their decoded vectors, from the same
    export mcp-private/ actually serves. Returns ([], {}) if either file is
    missing (caller should treat that as semantic scoring being unavailable,
    not as zero coverage)."""
    if not PRIVATE_ENTRIES_FILE.exists() or not PRIVATE_EMBEDDINGS_FILE.exists():
        return [], {}
    entries = json.loads(PRIVATE_ENTRIES_FILE.read_text(encoding="utf-8"))
    emb = json.loads(PRIVATE_EMBEDDINGS_FILE.read_text(encoding="utf-8"))
    demonstrated = [e for e in entries if e.get("type") in DEMONSTRATED_TYPES and e["id"] in emb["vectors"]]
    vectors = {e["id"]: _b64_to_vec(emb["vectors"][e["id"]]) for e in demonstrated}
    return demonstrated, vectors


def load_skill_embeddings() -> dict[str, list[float]]:
    """Precomputed query embeddings for analyze_job_postings.py's SKILLS
    names -- cached to a file (not computed live) since the embedding model
    isn't reachable from a plain script without the same Cloudflare-account
    tooling used for the corpus embeddings. Regenerate by re-running the
    skill-embedding batch (see PKH/CLAUDE.md) whenever SKILLS changes."""
    if not SKILL_EMBEDDINGS_FILE.exists():
        return {}
    data = json.loads(SKILL_EMBEDDINGS_FILE.read_text(encoding="utf-8"))
    return {name: _b64_to_vec(b64) for name, b64 in data["vectors"].items()}


def semantic_coverage(skill_name: str, skill_vectors: dict[str, list[float]],
                       corpus_entries: list[dict], corpus_vectors: dict[str, list[float]],
                       top_n: int = 3) -> dict:
    """Returns {known: int, peripheral: int, top: [(label, score), ...]} for
    one skill name against the demonstrated-evidence corpus. `known` uses the
    same 0.65 floor as the deployed MCP search; `peripheral` is the softer
    0.50-0.65 band this whole mechanism exists to represent honestly instead
    of collapsing to a misleading 0."""
    vec = skill_vectors.get(skill_name)
    if vec is None or not corpus_entries:
        return {"known": 0, "peripheral": 0, "top": [], "available": False}

    scored = sorted(
        ((e, _cosine(vec, corpus_vectors[e["id"]])) for e in corpus_entries),
        key=lambda x: -x[1],
    )
    known = sum(1 for _, s in scored if s >= SIM_FLOOR)
    peripheral = sum(1 for _, s in scored if PERIPHERAL_FLOOR <= s < SIM_FLOOR)
    top = [(e["label"], round(s, 3)) for e, s in scored[:top_n] if s >= PERIPHERAL_FLOOR]
    return {"known": known, "peripheral": peripheral, "top": top, "available": True}


def known_tag_counts() -> Counter:
    """Count how many known nodes carry each domain_tags value, via graph.json's
    tagged_with edges — the same graph the rest of the corpus feeds into.

    graphifyy doesn't consistently put the tag on source vs. target (direction
    gets canonicalized per-edge, likely based on node degree) — a tag_ id can
    show up on either side, so both must be checked.
    """
    if not GRAPH_FILE.exists():
        return Counter()
    graph = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
    counts = Counter()
    for edge in graph.get("links", []):
        if edge.get("relation") != "tagged_with":
            continue
        src, tgt = str(edge.get("source", "")), str(edge.get("target", ""))
        tag_id = tgt if tgt.startswith("tag_") else (src if src.startswith("tag_") else None)
        if tag_id:
            # tag ids are underscore-normalized (tag_data_science); domain_tags
            # use hyphens (data-science) — normalize back to match.
            counts[tag_id.removeprefix("tag_").replace("_", "-")] += 1
    return counts
