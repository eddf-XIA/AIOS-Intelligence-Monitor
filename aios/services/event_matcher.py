"""Deciding whether today's intelligence continues an existing event.

Two stages, as specified:

1. **Rules** narrow the whole event history down to a handful of plausible
   candidates (same module/topic, recently active, some lexical overlap).
2. **The configured LLM** decides MATCH vs NEW_EVENT among those candidates.

If the LLM is unavailable or returns nonsense, a deterministic lexical fallback
takes over so a run never dies here. Matching conservatively prefers NEW_EVENT:
a wrongly-split event is recoverable, a wrongly-merged one corrupts a timeline.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence

from ..models import IntelligenceEvent
from .llm import LLMError
from .llm.service import LLMService
from .intelligence_analyzer import CandidateIntelligence

logger = logging.getLogger(__name__)

#: Lexical overlap required before an event is even shown to the model.
SHORTLIST_THRESHOLD = 0.12
#: Overlap at which the rule-based fallback will merge without the model.
FALLBACK_MATCH_THRESHOLD = 0.55
#: Model confidence below which we ignore a proposed MATCH.
MIN_MATCH_CONFIDENCE = 0.6

MATCHER_SYSTEM_PROMPT = """你是情报事件归并判断器。

任务：判断"新情报"是否属于某个"已有事件"的后续进展。

判断标准：
- 同一个持续事件的新进展、新数据、新阶段 -> MATCH
- 同一主体但完全不同的另一件事 -> NEW_EVENT
- 仅仅是同一家公司/同一产品线，但事件本身不同 -> NEW_EVENT
- 不确定时一律返回 NEW_EVENT，不要勉强归并。

只输出合法 JSON：
{"decision":"MATCH","event_id":123,"confidence":0.91,"reason":"..."}
或
{"decision":"NEW_EVENT","event_id":null,"confidence":0.8,"reason":"..."}"""

_TOKEN_SPLIT = re.compile(r"[^0-9a-z一-鿿]+")
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "will",
    "发布", "推出", "宣布", "表示", "公司", "相关", "进行", "已经", "可以", "通过",
}


@dataclass
class MatchDecision:
    """Outcome of matching one candidate against event history."""

    decision: str  # "MATCH" | "NEW_EVENT"
    event_id: Optional[int]
    confidence: float
    reason: str
    method: str  # "llm" | "rules" | "no_candidates"

    @property
    def is_match(self) -> bool:
        return self.decision == "MATCH" and self.event_id is not None


def tokenize(text: str) -> set[str]:
    """Words for CJK-and-Latin lexical comparison.

    CJK has no spaces, so Han runs are additionally broken into bigrams, which
    is what actually makes the overlap score meaningful for Chinese titles.
    """
    normalised = unicodedata.normalize("NFKC", text or "").lower()
    tokens: set[str] = set()
    for chunk in _TOKEN_SPLIT.split(normalised):
        if not chunk or chunk in _STOPWORDS:
            continue
        if re.fullmatch(r"[一-鿿]+", chunk):
            if len(chunk) <= 2:
                tokens.add(chunk)
            else:
                tokens.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
        elif len(chunk) >= 2:
            tokens.add(chunk)
    return tokens


def overlap_score(left: str, right: str) -> float:
    """Jaccard-style overlap normalised by the smaller token set."""
    a, b = tokenize(left), tokenize(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def shortlist(
    candidate: CandidateIntelligence,
    events: Sequence[IntelligenceEvent],
    limit: int = 6,
) -> list[tuple[IntelligenceEvent, float]]:
    """Stage 1: rank recent same-scope events by lexical overlap."""
    text = f"{candidate.title} {candidate.fact_summary}"
    scored: list[tuple[IntelligenceEvent, float]] = []
    for event in events:
        score = max(
            overlap_score(candidate.title, event.title),
            overlap_score(text, f"{event.title} {event.summary}") * 0.9,
        )
        if candidate.topic_id and event.topic_id == candidate.topic_id:
            score += 0.05
        if score >= SHORTLIST_THRESHOLD:
            scored.append((event, round(score, 3)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def slugify_event_key(module_key: str, title: str, suffix: str = "") -> str:
    """Stable, readable key such as ``mobile-harmonyos7-ecosystem``."""
    normalised = unicodedata.normalize("NFKC", title or "").lower()
    ascii_words = re.findall(r"[a-z0-9]+", normalised)
    han = re.findall(r"[一-鿿]+", normalised)
    parts = ascii_words[:5] or ([han[0][:8]] if han else [])
    slug = "-".join(parts)[:80] or "event"
    key = f"{module_key or 'general'}-{slug}"
    return f"{key}-{suffix}" if suffix else key


def unique_event_key(base: str, exists) -> str:
    """Append a counter until ``exists(key)`` is False."""
    key = base[:150]
    if not exists(key):
        return key
    for n in range(2, 60):
        candidate = f"{key}-{n}"[:160]
        if not exists(candidate):
            return candidate
    import uuid

    return f"{key[:140]}-{uuid.uuid4().hex[:8]}"


class EventMatcher:
    """Two-stage matcher with a deterministic fallback."""

    def __init__(self, client: Optional[LLMService] = None, use_llm: bool = True) -> None:
        self.client = client
        self.use_llm = use_llm and client is not None

    def match(
        self, candidate: CandidateIntelligence, events: Sequence[IntelligenceEvent]
    ) -> MatchDecision:
        """Decide whether ``candidate`` belongs to one of ``events``."""
        shortlisted = shortlist(candidate, events)
        if not shortlisted:
            return MatchDecision(
                decision="NEW_EVENT",
                event_id=None,
                confidence=1.0,
                reason="No recent event in this module/topic is lexically related.",
                method="no_candidates",
            )

        if self.use_llm:
            decision = self._llm_decision(candidate, shortlisted)
            if decision is not None:
                return decision

        return self._rule_decision(shortlisted)

    # -- stage 2 -------------------------------------------------------------

    def _llm_decision(
        self,
        candidate: CandidateIntelligence,
        shortlisted: list[tuple[IntelligenceEvent, float]],
    ) -> Optional[MatchDecision]:
        assert self.client is not None
        existing = [
            {
                "event_id": event.id,
                "title": event.title,
                "summary": (event.summary or "")[:400],
                "first_seen": event.first_seen_date.isoformat() if event.first_seen_date else "",
                "last_seen": event.last_seen_date.isoformat() if event.last_seen_date else "",
                "observations": event.observation_count,
            }
            for event, _ in shortlisted
        ]
        new_item = {
            "title": candidate.title,
            "fact_summary": candidate.fact_summary[:800],
            "topic": candidate.topic_name,
        }
        user = (
            "已有事件：\n"
            + json.dumps(existing, ensure_ascii=False)
            + "\n\n新情报：\n"
            + json.dumps(new_item, ensure_ascii=False)
            + "\n\n请判断这条新情报是已有事件的后续进展（MATCH），还是一个全新事件（NEW_EVENT）。"
        )
        try:
            result = self.client.complete_json(
                [
                    {"role": "system", "content": MATCHER_SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                purpose="event_matching",
                max_tokens=400,
                topic_id=candidate.topic_id,
                module_id=candidate.module_id,
                retries=2,
            )
        except LLMError as exc:
            logger.info("Event matching LLM unavailable, falling back to rules: %s", exc)
            return None

        data = result.data
        decision = str(data.get("decision") or "").strip().upper()
        if decision not in {"MATCH", "NEW_EVENT"}:
            return None

        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(data.get("reason") or "")[:500]

        if decision == "NEW_EVENT":
            return MatchDecision("NEW_EVENT", None, confidence, reason, "llm")

        event_id = data.get("event_id")
        allowed = {event.id for event, _ in shortlisted}
        if not isinstance(event_id, int) or event_id not in allowed:
            logger.info("Model proposed an event_id outside the shortlist; treating as new.")
            return MatchDecision(
                "NEW_EVENT", None, confidence, "Model returned an unknown event_id.", "llm"
            )
        if confidence < MIN_MATCH_CONFIDENCE:
            return MatchDecision(
                "NEW_EVENT",
                None,
                confidence,
                f"Match confidence {confidence:.2f} below threshold.",
                "llm",
            )
        return MatchDecision("MATCH", event_id, confidence, reason, "llm")

    # -- fallback ------------------------------------------------------------

    @staticmethod
    def _rule_decision(shortlisted: list[tuple[IntelligenceEvent, float]]) -> MatchDecision:
        """Deterministic fallback: merge only on strong lexical overlap."""
        event, score = shortlisted[0]
        if score >= FALLBACK_MATCH_THRESHOLD:
            return MatchDecision(
                decision="MATCH",
                event_id=event.id,
                confidence=round(min(score, 0.95), 2),
                reason=f"Lexical overlap {score:.2f} with existing event.",
                method="rules",
            )
        return MatchDecision(
            decision="NEW_EVENT",
            event_id=None,
            confidence=round(1 - score, 2),
            reason=f"Best lexical overlap only {score:.2f}.",
            method="rules",
        )
