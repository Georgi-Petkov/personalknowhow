#!/usr/bin/env python3
"""
Cross-checks cv/master_cv.md's Education/Certifications/Experience claims against
PKH's corpus/ -- read-only, advisory. Never edits master_cv.md; the user resolves
flagged discrepancies by hand.

This exists because master_cv.md is user-authored (not derived from corpus/), so
it can drift out of date -- e.g. a MicroMasters that was "in progress" when the
CV was written but has since been completed (or attempted-and-not-passed) needs
a human to notice and update the wording.

Usage:
  python ingest/cv_reconcile.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
MASTER_CV = ROOT / "cv" / "master_cv.md"
CERTS_DIR = ROOT / "corpus" / "linkedin" / "certifications"
POSITIONS_DIR = ROOT / "corpus" / "linkedin" / "positions"
EDX_DIR = ROOT / "corpus" / "edx"

# Company-name normalization: CV wording -> corpus `provider` wording, since the
# same employer is spelled differently between the two (legal suffixes, DK vs
# ApS, "MemorixDK" vs "Memorix").
COMPANY_ALIASES = {
    "leo pharma": ["leo pharma"],
    "biites aps": ["biites"],
    "memorix": ["memorixdk", "memorix"],
    "ups denmark a/s": ["ups"],
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def parse_master_positions(text: str) -> list[dict]:
    """Extract (company, period_start, period_end) from master_cv.md's
    '### Company, Location -- Mon YYYY - Mon YYYY' headers.
    """
    positions = []
    for m in re.finditer(
        r"^### (.+?), .+? — (\w+ \d{4}) [–-] (\w+ \d{4}|Present)$",
        text, re.MULTILINE,
    ):
        positions.append({"company": m.group(1).strip(), "start": m.group(2), "end": m.group(3)})
    return positions


def parse_master_certifications(text: str) -> list[str]:
    m = re.search(r"^## Certifications\n(.*?)(?:\n## |\Z)", text, re.DOTALL | re.MULTILINE)
    if not m:
        return []
    return [line.lstrip("- ").strip() for line in m.group(1).strip().splitlines() if line.strip()]


def parse_master_education(text: str) -> list[dict]:
    m = re.search(r"^## Education\n(.*?)(?:\n## |\Z)", text, re.DOTALL | re.MULTILINE)
    if not m:
        return []
    entries = []
    blocks = re.split(r"\n(?=\*\*)", m.group(1).strip())
    for block in blocks:
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if len(lines) >= 2:
            entries.append({"title": lines[0].strip("*"), "detail": lines[1].strip("*")})
    return entries


def check_positions(master_positions: list[dict]) -> None:
    print("=== EXPERIENCE: master_cv.md vs corpus/linkedin/positions/ ===")
    corpus_files = sorted(POSITIONS_DIR.glob("*.md"))
    corpus_entries = []
    for f in corpus_files:
        fm_text = _read(f)
        title_m = re.search(r"^title: (.+)$", fm_text, re.MULTILINE)
        provider_m = re.search(r"^provider: (.+)$", fm_text, re.MULTILINE)
        period_m = re.search(r"\*\*Period:\*\* (\S.*?\S) [–-] (\S.*)$", fm_text, re.MULTILINE)
        if not provider_m or "seeking" in (provider_m.group(1) or "").lower():
            continue  # LinkedIn placeholder entry, not a real position
        corpus_entries.append({
            "file": f.name,
            "title": (title_m.group(1) if title_m else "").strip(),
            "provider": (provider_m.group(1) if provider_m else "").strip(),
            "period": (period_m.group(0) if period_m else "").replace("**Period:** ", "").strip(),
        })

    matched_files = set()
    for mp in master_positions:
        aliases = COMPANY_ALIASES.get(mp["company"].lower(), [mp["company"].lower()])
        match = next((c for c in corpus_entries
                      if any(a in c["provider"].lower() for a in aliases)), None)
        if match:
            matched_files.add(match["file"])
            cv_period = f"{mp['start']} - {mp['end']}"
            print(f"  [{mp['company']}] CV: {cv_period} | corpus ({match['file']}): {match['period']}")
        else:
            print(f"  [{mp['company']}] CV entry has NO matching corpus position -- unverified")

    unmatched = [c for c in corpus_entries if c["file"] not in matched_files]
    if unmatched:
        print("\n  Corpus positions NOT reflected anywhere in master_cv.md:")
        for c in unmatched:
            print(f"    - {c['title']} ({c['provider']}, {c['period']}) -- {c['file']}")


def check_certifications(master_certs: list[str]) -> None:
    print("\n=== CERTIFICATIONS: master_cv.md vs corpus/linkedin/certifications/ ===")
    corpus_files = sorted(CERTS_DIR.glob("*.md"))
    corpus_titles = []
    for f in corpus_files:
        title_m = re.search(r"^title: ['\"]?(.+?)['\"]?$", _read(f), re.MULTILINE)
        if title_m:
            corpus_titles.append((title_m.group(1).strip(), f.name))

    matched_files = set()
    for cert in master_certs:
        cert_words = set(re.findall(r"[a-z]+", cert.lower())) - {
            "microsoft", "certified", "the", "of", "in", "a", "and"
        }
        best = None
        for title, fname in corpus_titles:
            title_words = set(re.findall(r"[a-z]+", title.lower()))
            overlap = cert_words & title_words
            # Require most of the CV cert's own distinctive words to be present,
            # not just 2 words shared by chance (e.g. "power"+"bi" alone matching
            # two genuinely different Power BI certifications).
            if cert_words and len(overlap) / len(cert_words) >= 0.6 and len(overlap) >= 2 \
                    and (best is None or len(overlap) > best[2]):
                best = (title, fname, len(overlap))
        if best:
            matched_files.add(best[1])
            print(f"  [MATCH] CV: {cert!r}")
            print(f"          corpus: {best[0]!r} ({best[1]})")
            if "preview" in best[0].lower() or "exam prep" in best[0].lower():
                print("          NOTE: corpus entry is exam-prep/preview material, not confirmed "
                      "proof of passing the real exam -- verify before claiming "
                      "\"(Microsoft Certified)\" specifically.")
        else:
            print(f"  [NO MATCH] CV: {cert!r} -- no corresponding corpus/linkedin/certifications/ "
                  "entry found. Not necessarily false, but unverified against this repo.")

    extra = [(t, f) for t, f in corpus_titles if f not in matched_files]
    if extra:
        print(f"\n  {len(extra)} corpus certifications NOT listed in master_cv.md's Certifications "
              "section (real, verified additions to consider):")
        for title, fname in extra:
            print(f"    - {title} ({fname})")


def check_education(master_edu: list[dict]) -> None:
    print("\n=== EDUCATION: MITx MicroMasters status check ===")
    mitx_entry = next((e for e in master_edu if "micromasters" in e["title"].lower()), None)
    if not mitx_entry:
        print("  No MITx MicroMasters entry found in master_cv.md Education section.")
        return
    print(f"  CV claims: {mitx_entry['title']} -- {mitx_entry['detail']}")

    edx_courses = sorted(EDX_DIR.glob("*.md"))
    sds_courses = []
    for f in edx_courses:
        text = _read(f)
        if re.search(r"Statistics and Data Science", text, re.IGNORECASE):
            grade_m = re.search(r"^grade: (.+)$", text, re.MULTILINE)
            date_m = re.search(r"^date: ['\"]?([\d-]+)", text, re.MULTILINE)
            title_m = re.search(r"^title: (.+)$", text, re.MULTILINE)
            sds_courses.append({
                "title": title_m.group(1) if title_m else f.stem,
                "date": date_m.group(1) if date_m else "?",
                "grade": grade_m.group(1) if grade_m else "?",
            })
    if sds_courses:
        print(f"  corpus/edx/ shows {len(sds_courses)} SDS MicroMasters courses already earned:")
        for c in sorted(sds_courses, key=lambda c: c["date"]):
            print(f"    - {c['title']}: {c['grade']} (earned {c['date']})")
        print('  MISMATCH: CV says "in progress, capstone underway" but all listed courses '
              "above are already earned. If the capstone has since been attempted, update "
              "the CV to reflect the actual current status rather than the original "
              '"in progress" wording (verify current capstone result before editing).')
    else:
        print("  No Statistics and Data Science courses found in corpus/edx/ -- cannot verify.")


def main() -> None:
    if not MASTER_CV.exists():
        print(f"ERROR: {MASTER_CV} not found")
        sys.exit(1)
    text = _read(MASTER_CV)

    check_positions(parse_master_positions(text))
    check_certifications(parse_master_certifications(text))
    check_education(parse_master_education(text))

    print("\n=== NOTE ===")
    print("This report is advisory only -- nothing here is auto-applied to master_cv.md. "
          "Review each flagged item and edit the master file by hand where the CV is stale "
          "or under/over-claiming relative to the corpus evidence.")


if __name__ == "__main__":
    main()
