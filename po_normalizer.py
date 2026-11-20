"""
Deterministic SupplyPro Purchase Order (PO) Normalization and Dual-PO Splitting Utility.

Implements the accounting team's PO parsing rules in strict sequential order:
  - Step A (Rule 3.1): Dual-PO Splitting on `/` when all segments are full PO identifiers
    (length >= 6 with alphanumeric characters, not a short 1-5 digit extension like `/0140`),
    dividing `total_fee` equally (`round(total_fee / len(segments), 2)`).
  - Step B (Rule 2.1): Strip parenthetical counters and anything inside/after them
    (e.g., `20382792-000 (1)` -> `20382792-000`, `91KJBLE9_004856(2)` -> `91KJBLE9_004856`).
  - Step C (Rule 2.2): On single (non-split) POs, strip any remaining `/` and trailing short
    extension (e.g., `59982179-000/0140` -> `59982179-000`).
  - Step D (Rule 2.3): Retain Step C result as `primary_po`. If `primary_po` ends with a
    trailing suffix code (`EO`, `OS`, or `ON`, case-insensitive, preceded by whitespace,
    `-`, `_`, or digits — e.g., `209846 ON`), populate `fallback_po` with that suffix stripped.
"""

from dataclasses import asdict, dataclass
import re
from typing import Any, Iterator, Optional

# Matches a parenthetical counter and anything inside/after it: e.g., " (1)", "(2)", "(1) - note"
_PAREN_COUNTER_RE = re.compile(r"\s*\([^)]*\).*$")

# Matches trailing suffix codes EO, OS, or ON (case-insensitive) preceded by whitespace, '-', '_', or digits
_SUFFIX_CODE_RE = re.compile(
    r"^(?P<base>.+?)(?:[\s\-_]+(?P<sep_code>EO|OS|ON)|(?<=\d)(?P<dig_code>EO|OS|ON))$",
    re.IGNORECASE,
)


@dataclass
class NormalizedPOLookup:
    """
    Normalized PO lookup descriptor supporting both attribute access (`item.primary_po`)
    and dictionary-style access (`item["primary_po"]`, `item.get("primary_po")`).
    """

    primary_po: str
    fallback_po: Optional[str]
    expected_fee: float
    is_split: bool
    raw_po: str

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(("primary_po", "fallback_po", "expected_fee", "is_split", "raw_po"))

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self):
        return asdict(self).keys()

    def values(self):
        return asdict(self).values()

    def items(self):
        return asdict(self).items()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_full_po_segment(segment: str) -> bool:
    """
    Returns True if `segment` looks like a full PO identifier:
    - After stripping any parenthetical counter, length >= 6
    - Contains alphanumeric characters
    - Is NOT a short 1-5 digit extension like `0140`
    """
    cleaned = _PAREN_COUNTER_RE.sub("", (segment or "").strip()).strip()
    if len(cleaned) < 6:
        return False
    if re.fullmatch(r"\d{1,5}", cleaned):
        return False
    return bool(re.search(r"[A-Za-z0-9]", cleaned))


def _strip_parentheses(segment: str) -> str:
    """
    Step B — Rule 2.1: Strip parenthetical counters and anything inside/after them,
    then trim whitespace.
    """
    return _PAREN_COUNTER_RE.sub("", (segment or "").strip()).strip()


def _extract_fallback_po(primary_po: str) -> Optional[str]:
    """
    Step D — Rule 2.3: If `primary_po` ends with a trailing suffix code (`EO`, `OS`, or `ON`,
    case-insensitive, preceded by whitespace, `-`, `_`, or digits), return `primary_po`
    with that suffix stripped; otherwise return None.
    """
    if not primary_po:
        return None
    match = _SUFFIX_CODE_RE.match(primary_po.strip())
    if not match:
        return None
    base = (match.group("base") or "").strip().rstrip("-_").strip()
    if base and base != primary_po:
        return base
    return None


def normalize_and_split_po(raw_po: str, total_fee: float = 0.0) -> list[NormalizedPOLookup]:
    """
    Normalizes and optionally splits a raw PO string and total fee according to
    Rules 2.1, 2.2, 2.3, and 3.1 in strict sequential order (Step A -> Step B -> Step C -> Step D).
    """
    if raw_po is None:
        return []
    original_raw = str(raw_po).strip()
    if not original_raw:
        return []

    fee_val = round(float(total_fee if total_fee is not None else 0.0), 2)

    # Step A — Rule 3.1 (Dual-PO Splitting)
    is_split = False
    segments: list[str] = [original_raw]
    expected_fee_per_item = fee_val

    if "/" in original_raw:
        candidate_segments = [seg.strip() for seg in original_raw.split("/") if seg.strip()]
        if len(candidate_segments) >= 2 and all(_is_full_po_segment(seg) for seg in candidate_segments):
            is_split = True
            segments = candidate_segments
            expected_fee_per_item = round(fee_val / len(candidate_segments), 2)

    results: list[NormalizedPOLookup] = []
    for seg in segments:
        # Step B — Rule 2.1 (Strip Parentheses & Iteration Numbers)
        # Note: For single POs where parentheses precede a trailing slash (e.g. "59982179-000 (1)/0140"),
        # stripping parentheses removes "(1)/0140" directly; if slash precedes parens or no parens exist,
        # Step C handles any remaining trailing slash extension.
        step_b = _strip_parentheses(seg)

        # Step C — Rule 2.2 (Strip Trailing Slash Extensions on non-split POs)
        if not is_split and "/" in step_b:
            step_c = _strip_parentheses(step_b.split("/", 1)[0].strip())
        else:
            step_c = step_b

        primary_po = step_c.strip()
        if not primary_po:
            continue

        # Step D — Rule 2.3 (Suffix Codes EO, OS, ON)
        fallback_po = _extract_fallback_po(primary_po)

        results.append(
            NormalizedPOLookup(
                primary_po=primary_po,
                fallback_po=fallback_po,
                expected_fee=expected_fee_per_item,
                is_split=is_split,
                raw_po=original_raw,
            )
        )

    return results
