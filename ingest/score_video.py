#!/usr/bin/env python3
"""
Fetch a YouTube transcript, ground it against your corpus, and print a ready-made
scoring prompt — no API key needed. Paste the output into a Claude Code chat and
ask it to score the video on 4 dimensions:

  1. DIRECTION  — WATCH / SKIM / SKIP
  2. NOVELTY    — how much of this is new to you, given what's in RELATED KNOWN CONTENT
  3. VALIDITY   — are the claims made true, or exaggerated/nonsense?
  4. AUTHORITY  — who's presenting, and how credible are they on this topic?

Usage:
  python ingest/score_video.py "https://youtube.com/watch?v=..."
  python ingest/score_video.py "https://youtu.be/xxxxx"
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_WORDS = 6000   # ~35 min of speech
TOP_RELATED = 12
STOPWORDS = {
    "the", "and", "for", "with", "from", "your", "you", "this", "that", "into",
    "how", "what", "why", "who", "are", "our", "get", "getting", "using", "use",
    "learn", "introduction", "intro", "to", "in", "on", "of", "a", "an", "is",
    "it", "its", "at", "by", "or", "as", "be", "can", "will", "not", "no",
}


def _ensure_deps() -> None:
    try:
        import youtube_transcript_api  # noqa: F401
    except ImportError:
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "youtube-transcript-api",
             "-q", "--break-system-packages"],
            check=True,
        )


sys.path.insert(0, str(Path(__file__).parent))
ROOT = Path(__file__).parent.parent
CORPUS = ROOT / "corpus"


def extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc in ("youtu.be",):
        return parsed.path.lstrip("/").split("?")[0]
    qs = parse_qs(parsed.query)
    if "v" in qs:
        return qs["v"][0]
    raise ValueError(f"Cannot extract video ID from: {url}")


def fetch_metadata(video_id: str) -> dict:
    try:
        import urllib.request
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            return {
                "title":       data.get("title", video_id),
                "author_name": data.get("author_name", "unknown"),
                "author_url":  data.get("author_url", ""),
            }
    except Exception:
        return {"title": video_id, "author_name": "unknown", "author_url": ""}


def fetch_transcript(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi, CouldNotRetrieveTranscript

    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=["en"])
    except CouldNotRetrieveTranscript:
        try:
            tl = api.list(video_id)
            transcript = next(iter(tl))
            fetched = transcript.fetch()
        except Exception:
            raise RuntimeError("No transcript available for this video.")

    words = []
    for e in fetched:
        words.extend(e.text.split())
        if len(words) >= MAX_WORDS:
            break
    return " ".join(words[:MAX_WORDS])


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-]{2,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def find_related_corpus(title: str, transcript: str) -> list[tuple[float, dict]]:
    """Rank corpus entries by keyword + domain_tag overlap with the video."""
    from merge import parse_file
    from common import infer_tags

    if not CORPUS.exists():
        return []

    video_keywords = _keywords(title) | _keywords(transcript[:2000])
    video_tags = set(infer_tags(title + " " + transcript[:2000]))

    scored: list[tuple[float, dict]] = []
    for path in CORPUS.rglob("*.md"):
        fm, _ = parse_file(path)
        if not fm or not fm.get("title"):
            continue
        entry_title = str(fm["title"])
        entry_desc = str(fm.get("description") or "")
        entry_tags = set(fm.get("domain_tags") or [])

        title_keywords = _keywords(entry_title)
        title_hits = len(video_keywords & title_keywords)
        tag_hits = len(video_tags & entry_tags)

        score = title_hits * 3 + tag_hits
        if score <= 0:
            continue
        scored.append((score, {
            "title":       entry_title,
            "description": entry_desc,
            "provider":    fm.get("provider", "?"),
            "type":        fm.get("type", "?"),
        }))

    scored.sort(key=lambda x: -x[0])
    return scored[:TOP_RELATED]


def main(url: str) -> None:
    video_id   = extract_video_id(url)
    meta       = fetch_metadata(video_id)
    transcript = fetch_transcript(video_id)
    word_count = len(transcript.split())
    related    = find_related_corpus(meta["title"], transcript)

    print(f"TITLE: {meta['title']}")
    print(f"CHANNEL: {meta['author_name']}")
    print(f"CHANNEL_URL: {meta['author_url']}")
    print(f"URL: {url}")
    print(f"WORDS: {word_count:,}")
    print()
    print("RELATED KNOWN CONTENT (from your corpus, ranked by relevance):")
    if related:
        for _, e in related:
            print(f"- [{e['provider']}] {e['title']} — {e['description']}")
    else:
        print("- (none found — this looks like new territory for you)")
    print()
    print("---TRANSCRIPT---")
    print(transcript)
    print()
    print("---SCORING INSTRUCTIONS---")
    print(
        "Paste this entire output into a Claude Code chat and ask it to score the "
        "video on these 4 dimensions:\n"
        "1. DIRECTION — WATCH / SKIM / SKIP, with a one-line reason.\n"
        "2. NOVELTY FOR ME — how much of this is new given RELATED KNOWN CONTENT above "
        "vs. already covered; call out specifically what's new.\n"
        "3. VALIDITY — are the claims made true, or exaggerated/nonsense? Distinguish "
        "factual claims from marketing framing.\n"
        "4. AUTHORITY — who is CHANNEL, and how credible/authoritative are they on "
        "this specific topic?"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ingest/score_video.py <youtube-url>")
        sys.exit(1)
    _ensure_deps()
    main(sys.argv[1])
