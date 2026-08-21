#!/usr/bin/env python3
"""
Structured salary-range extraction from free-text job postings.

Grounded in the exact salary formats observed in data/linkedin_jobs_scan/ (the 7
real disclosed-salary postings as of the 2026-07-21 scrape) -- not hypothetical
formats. When a snippet doesn't cleanly match one of these known shapes, this
returns None / low confidence rather than guessing: a wrong number is worse than
an admitted "couldn't parse."

Usage (self-test against the 7 known ground-truth snippets):
  python ingest/salary_parser.py
"""
import re

CURRENCY_MAP = {
    "$": "USD", "USD": "USD",
    "DKK": "DKK", "KR.": "DKK", "KR": "DKK",
    "EUR": "EUR", "€": "EUR",
    "NOK": "NOK",
    "SEK": "SEK",
}
# Longest-first so "KR." isn't partially matched by a shorter alternative first.
_CUR_ALTS = sorted(CURRENCY_MAP.keys(), key=len, reverse=True)
CUR = r"(?:" + "|".join(re.escape(c) for c in _CUR_ALTS) + r")"

# Grouped number (thousands-separated, optional 2-decimal cents) or a plain integer.
NUM = r"\d{1,3}(?:[,.\s]\d{3})+(?:\.\d{2})?|\d+"


def numk(i: str) -> str:
    return rf"(?P<n{i}>{NUM})\s?(?P<k{i}>[Kk])?"

PER = (r"/\s?(?:yr|year|md|month|wk|week)|per\s+(?:year|month|week)"
       r"|annually|annual|monthly|weekly")
SEP = r"-|–|—|to"
QUAL_MAX = r"up\s+to|max(?:imum)?"
QUAL_MIN = r"from|min(?:imum)?|starting\s+(?:at|from)"

FX_TO_DKK = {"DKK": 1.0, "USD": 6.9, "EUR": 7.5, "NOK": 0.65, "SEK": 0.65}
# Manual, approximate rates set 2026-07 -- no live API call, update occasionally.

MIN_PLAUSIBLE = 1000  # filters stray digits (funding figures, years-of-exp, etc.)


def _norm_number(raw: str) -> float | None:
    """NUM match -> float, handling thousands separators and 2-decimal cents.
    Returns None if the grouping looks ambiguous rather than guessing.
    """
    raw = raw.strip()
    cents = ""
    m = re.search(r"\.(\d{2})$", raw)
    if m:
        cents = m.group(0)
        raw = raw[: -len(cents)]
    if re.fullmatch(r"\d+", raw):
        return float(raw)
    parts = re.split(r"[,.\s]", raw)
    if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]) and 1 <= len(parts[0]) <= 3:
        return float("".join(parts))
    return None  # ambiguous grouping -- don't guess


def _currency(raw: str) -> str | None:
    return CURRENCY_MAP.get(raw.upper().rstrip("."), CURRENCY_MAP.get(raw.upper()))


def _infer_period(currency: str, value: float) -> str:
    floor = 200_000 if currency in ("DKK", "NOK", "SEK") else 20_000
    return "annual" if value >= floor else "monthly"


# --- Range patterns: currency is mandatory (not optional) in each variant so a
# bare "20 - 25" (a date range, a headcount, etc.) can never match. -------------

RANGE_V1 = re.compile(  # currency prefixes the first number: "DKK800,000 - DKK1,100,000"
    rf"(?P<cur>{CUR})\s?"
    + numk("1") + rf"(?:\s?(?P<per1>{PER}))?"
    rf"\s?(?:{SEP})\s?(?:{CUR}\s?)?"
    + numk("2") + rf"(?:\s?(?P<per2>{PER}))?",
    re.IGNORECASE,
)

RANGE_V2 = re.compile(  # currency suffixes each number: "40K DKK/month - 50K DKK/month"
    numk("1") + rf"\s?(?P<cur>{CUR})(?:\s?(?P<per1>{PER}))?"
    rf"\s?(?:{SEP})\s?"
    + numk("2") + rf"\s?(?:{CUR})?(?:\s?(?P<per2>{PER}))?",
    re.IGNORECASE,
)

RANGE_V3 = re.compile(  # currency trails the whole range: "687 000 - 944 900 DKK"
    numk("1") + rf"(?:\s?(?P<per1>{PER}))?"
    rf"\s?(?:{SEP})\s?"
    + numk("2") + rf"(?:\s?(?P<per2>{PER}))?\s?(?P<cur>{CUR})",
    re.IGNORECASE,
)

SINGLE_RE = re.compile(  # "Up to $150K USD"
    rf"(?:(?P<qual_max>{QUAL_MAX})|(?P<qual_min>{QUAL_MIN}))?\s?"
    rf"(?P<cur_pre>{CUR})?\s?"
    + numk("1") +
    rf"\s?(?P<cur_post>{CUR})?(?:\s?(?P<per1>{PER}))?",
    re.IGNORECASE,
)


def _resolve_k(n1: float, k1: str | None, n2: float, k2: str | None) -> tuple[float, float]:
    """Shared-suffix expansion: "DKK40-45K" -> (40000, 45000), not (40, 45000) --
    if either side carries a K, both numbers are in thousands.
    """
    if k1 or k2:
        return n1 * 1000, n2 * 1000
    return n1, n2


def _try_range(pattern: re.Pattern, text: str) -> dict | None:
    for m in pattern.finditer(text):
        n1 = _norm_number(m.group("n1"))
        n2 = _norm_number(m.group("n2"))
        if n1 is None or n2 is None:
            continue
        currency = _currency(m.group("cur"))
        if currency is None:
            continue
        n1, n2 = _resolve_k(n1, m.group("k1"), n2, m.group("k2"))
        lo, hi = min(n1, n2), max(n1, n2)
        if lo < MIN_PLAUSIBLE:
            continue
        per_raw = (m.group("per1") or m.group("per2") or "").lower()
        if "week" in per_raw or per_raw == "/wk":
            period, period_conf = "weekly", "explicit"
        elif "month" in per_raw or per_raw == "/md":
            period, period_conf = "monthly", "explicit"
        elif per_raw:
            period, period_conf = "annual", "explicit"
        else:
            period, period_conf = _infer_period(currency, hi), "inferred"
        return {
            "salary_raw": m.group(0),
            "salary_min": lo, "salary_max": hi,
            "currency": currency,
            "period": period, "period_confidence": period_conf,
            "parse_confidence": "high",
        }
    return None


def _try_single(text: str) -> dict | None:
    for m in SINGLE_RE.finditer(text):
        cur_raw = m.group("cur_pre") or m.group("cur_post")
        if not cur_raw:
            continue
        currency = _currency(cur_raw)
        val = _norm_number(m.group("n1"))
        if val is None:
            continue
        val = val * 1000 if m.group("k1") else val
        if val < MIN_PLAUSIBLE:
            continue
        per_raw = (m.group("per1") or "").lower()
        if "week" in per_raw or per_raw == "/wk":
            period, period_conf = "weekly", "explicit"
        elif "month" in per_raw or per_raw == "/md":
            period, period_conf = "monthly", "explicit"
        elif per_raw:
            period, period_conf = "annual", "explicit"
        else:
            period, period_conf = _infer_period(currency, val), "inferred"
        lo = None if m.group("qual_max") else val
        hi = val if not m.group("qual_min") else None
        return {
            "salary_raw": m.group(0),
            "salary_min": lo, "salary_max": hi,
            "currency": currency,
            "period": period, "period_confidence": period_conf,
            "parse_confidence": "high",
        }
    return None


def parse_salary(text: str) -> dict | None:
    """Locate and parse the first plausible salary expression in `text`.
    Tries range patterns (two-sided) before the single-figure pattern, since a
    range is the more specific/informative match when both are present.
    Returns None if nothing confidently parseable is found.
    """
    for pattern in (RANGE_V1, RANGE_V2, RANGE_V3):
        result = _try_range(pattern, text)
        if result:
            return result
    return _try_single(text)


def to_annual_dkk(parsed: dict) -> tuple[float | None, float | None] | None:
    """Normalize a parse_salary() result to an approximate annual DKK range."""
    if parsed is None:
        return None
    rate = FX_TO_DKK.get(parsed["currency"])
    if rate is None:
        return None
    mult = {"monthly": 12, "weekly": 52}.get(parsed["period"], 1)

    def conv(v: float | None) -> float | None:
        return None if v is None else v * mult * rate

    return conv(parsed["salary_min"]), conv(parsed["salary_max"])


# --- Self-test against the 7 real ground-truth snippets -----------------------

_GROUND_TRUTH = [
    ("$115K/yr - $160K/yr", (115000, 160000, "USD", "annual", "explicit")),
    ("687 000 - 944 900 DKK gross", (687000, 944900, "DKK", "annual", "inferred")),
    ("DKK800,000 - DKK1,100,000 annually", (800000, 1100000, "DKK", "annual", "explicit")),
    ("Up to $150K USD + Equity", (None, 150000, "USD", "annual", "inferred")),
    ("40K DKK/month - 50K DKK/month", (40000, 50000, "DKK", "monthly", "explicit")),
    ("DKK40-45K per month", (40000, 45000, "DKK", "monthly", "explicit")),
    ("650,960.00 to 956,860.00 DKK", (650960, 956860, "DKK", "annual", "inferred")),
    ("$80/hr, up to $1,600/week", (None, 1600, "USD", "weekly", "explicit")),
]


def _run_self_test() -> None:
    failures = 0
    for text, expected in _GROUND_TRUTH:
        exp_min, exp_max, exp_cur, exp_per, exp_conf = expected
        result = parse_salary(text)
        ok = (
            result is not None
            and result["salary_min"] == exp_min
            and result["salary_max"] == exp_max
            and result["currency"] == exp_cur
            and result["period"] == exp_per
            and result["period_confidence"] == exp_conf
        )
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{status}] {text!r}")
        print(f"       expected: {expected}")
        print(f"       got:      {result}")
    print(f"\n{len(_GROUND_TRUTH) - failures}/{len(_GROUND_TRUTH)} passed")


if __name__ == "__main__":
    _run_self_test()
