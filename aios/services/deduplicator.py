"""Candidate de-duplication.

Three layers, cheapest first:

1. canonical URL identity (exact),
2. content hash (same body republished at a different URL),
3. title similarity (the original ``SequenceMatcher`` heuristic at 0.86).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from .article_extractor import canonicalize_url, content_hash
from .collector import Candidate

DEFAULT_THRESHOLD = 0.86

_PUNCT = re.compile(r"[\s\-_|｜–—·、,，.。:：!！?？\"'“”‘’()（）\[\]【】]+")


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and the trailing ' - Publisher' suffix."""
    text = (title or "").strip().lower()
    text = re.sub(r"\s+[-–—|]\s+[^-–—|]{1,40}$", "", text)
    return _PUNCT.sub(" ", text).strip()


def title_similarity(left: str, right: str) -> float:
    """Ratio in [0, 1]; 1.0 means identical after normalisation."""
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def dedupe_candidates(
    items: Sequence[Candidate], threshold: float = DEFAULT_THRESHOLD
) -> list[Candidate]:
    """Collapse duplicates within one collection pass, keeping the first seen."""
    kept: list[Candidate] = []
    seen_urls: set[str] = set()
    seen_content: set[str] = set()

    for item in items:
        title = (item.title or "").strip()
        if not title or not item.url:
            continue

        canonical = canonicalize_url(item.url)
        if canonical in seen_urls:
            continue

        digest = content_hash(item.body_text) if item.body_text else ""
        if digest and digest in seen_content:
            continue

        if any(title_similarity(title, other.title) >= threshold for other in kept):
            continue

        seen_urls.add(canonical)
        if digest:
            seen_content.add(digest)
        kept.append(item)

    return kept


def filter_excluded(items: Iterable[Candidate], keywords: Sequence[str]) -> list[Candidate]:
    """Drop candidates whose title or snippet contains a banned keyword."""
    banned = [k.strip().lower() for k in keywords if k and k.strip()]
    if not banned:
        return list(items)

    kept = []
    for item in items:
        haystack = f"{item.title} {item.snippet}".lower()
        if any(word in haystack for word in banned):
            continue
        kept.append(item)
    return kept
