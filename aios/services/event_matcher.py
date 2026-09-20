"""Deciding whether today's intelligence continues an existing event.

Three stages:

1. **Rules** narrow the whole event history down to a handful of plausible
   candidates (same module/topic/research topic, recently active, some overlap
   of identity or wording).
2. **Identity** (:mod:`aios.services.event_identity`) scores each shortlisted
   event on a fingerprint - shared source URL, product/project, organisation,
   event type, event date - not on its title. A shared source URL, or a
   fingerprint strong enough and free of conflicts, merges on its own without
   spending a model call.
3. **The configured LLM** decides MATCH vs NEW_EVENT among whatever identity
   left genuinely ambiguous.

If the LLM is unavailable or returns nonsense, a deterministic fallback takes
over so a run never dies here. Matching conservatively prefers NEW_EVENT:
a wrongly-split event is recoverable, a wrongly-merged one corrupts a timeline.

Stage 2 is what makes Research Agent runs viable. An agent rewords the same
story every day, so title equality would start a new timeline each time; a
Classic candidate carries no identity fields at all and therefore scores purely
lexically, exactly as it did in v2.1.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence

from ..models import IntelligenceEvent
from .event_identity import (
    EventIdentity,
    IdentityMatch,
    compare_identity,
    identity_from_candidate,
    identity_from_event,
)
from .llm import LLMError
from .llm.service import LLMService
from .intelligence_analyzer import CandidateIntelligence

logger = logging.getLogger(__name__)

#: Lexical overlap required before an event is even shown to the model.
SHORTLIST_THRESHOLD = 0.12
#: Combined identity score required to reach the shortlist at all.
IDENTITY_SHORTLIST_FLOOR = 0.18
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

注意：同一件事在不同日期常被用完全不同的措辞描述。
标题不同不等于是两件事，标题相似也不等于是同一件事。
请优先比较 organization（主体）、product_or_project（产品/项目）、
event_type（事件类型）和 event_date，而不是标题的字面相似度。

典型判断：
- 「Figure 发布 Helix 2」与「Figure 推出新一代人形机器人智能系统 Helix 2」
  -> 同一产品、同一类型事件 -> MATCH
- 「Figure 发布 Helix 2」与「Helix 2 获得新的工厂部署」
  -> 同一产品但里程碑不同 -> NEW_EVENT

identity_score / identity_notes 是系统给出的参考信号，不是结论。

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
    method: str  # "identity" | "llm" | "rules" | "no_candidates"

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


def lexical_score(
    candidate: CandidateIntelligence, event: IntelligenceEvent
) -> float:
    """CJK-aware title/summary overlap between a candidate and an event."""
    text = f"{candidate.title} {candidate.fact_summary}"
    return max(
        overlap_score(candidate.title, event.title),
        overlap_score(text, f"{event.title} {event.summary}") * 0.9,
    )


def shortlist(
    candidate: CandidateIntelligence,
    events: Sequence[IntelligenceEvent],
    limit: int = 6,
) -> list[tuple[IntelligenceEvent, float]]:
    """Stage 1: rank recent same-scope events by lexical overlap.

    Kept as-is for the Classic path and for callers that only want a lexical
    ranking. :func:`shortlist_with_identity` is the richer version the matcher
    itself uses.
    """
    scored: list[tuple[IntelligenceEvent, float]] = []
    for event in events:
        score = lexical_score(candidate, event)
        if candidate.topic_id and event.topic_id == candidate.topic_id:
            score += 0.05
        if score >= SHORTLIST_THRESHOLD:
            scored.append((event, round(score, 3)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def shortlist_with_identity(
    candidate: CandidateIntelligence,
    events: Sequence[IntelligenceEvent],
    limit: int = 6,
) -> list[tuple[IntelligenceEvent, float, IdentityMatch]]:
    """Stage 1+2: rank by fingerprint, falling back to wording.

    An event is shortlisted when *either* signal is interesting, so a
    reworded-but-same-product story survives a low lexical score and a
    Classic candidate with no fingerprint still matches on wording alone.
    """
    candidate_identity = identity_from_candidate(candidate)
    scored: list[tuple[IntelligenceEvent, float, IdentityMatch]] = []

    for event in events:
        lexical = lexical_score(candidate, event)
        identity = compare_identity(
            candidate_identity, identity_from_event(event), lexical=lexical
        )

        score = identity.score
        if candidate.topic_id and getattr(event, "topic_id", None) == candidate.topic_id:
            score += 0.03
        if (
            getattr(candidate, "research_topic_id", None)
            and getattr(event, "research_topic_id", None) == candidate.research_topic_id
        ):
            score += 0.03
        score = round(min(1.0, score), 3)

        # Either route in: a strong fingerprint, or plain lexical similarity.
        if score >= IDENTITY_SHORTLIST_FLOOR or lexical >= SHORTLIST_THRESHOLD:
            scored.append((event, score, identity))

    scored.sort(key=lambda entry: entry[1], reverse=True)
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
        scored = shortlist_with_identity(candidate, events)
        if not scored:
            return MatchDecision(
                decision="NEW_EVENT",
                event_id=None,
                confidence=1.0,
                reason="No recent event in this scope resembles this item.",
                method="no_candidates",
            )

        # Stage 2. A shared source URL, or a fingerprint strong enough and
        # free of conflicting signals, is better evidence than anything a
        # model could add - and it costs nothing.
        best_event, best_score, best_identity = scored[0]
        if best_identity.is_strong:
            return MatchDecision(
                decision="MATCH",
                event_id=best_event.id,
                confidence=round(min(best_score, 0.99), 2),
                reason=best_identity.reason_text or f"身份特征高度一致（{best_score:.2f}）",
                method="identity",
            )

        # The deterministic fallback judges on wording alone, exactly as in
        # v2.1: a Classic candidate contributes no fingerprint, so feeding it a
        # blended score would move a threshold that was tuned against lexical
        # overlap.
        shortlisted = [
            (event, identity.lexical) for event, _, identity in scored
        ]
        shortlisted.sort(key=lambda pair: pair[1], reverse=True)

        if self.use_llm:
            decision = self._llm_decision(candidate, scored)
            if decision is not None:
                return decision

        return self._rule_decision(shortlisted)

    # -- stage 2 -------------------------------------------------------------

    def _llm_decision(
        self,
        candidate: CandidateIntelligence,
        scored: list[tuple[IntelligenceEvent, float, IdentityMatch]],
    ) -> Optional[MatchDecision]:
        """Stage 3: ask the model about what identity left ambiguous.

        The fingerprint is included in the prompt rather than only the title,
        so the model is judging "same product, different milestone?" instead of
        guessing from two differently-worded headlines.
        """
        assert self.client is not None
        existing = []
        for event, score, identity in scored:
            entry = {
                "event_id": event.id,
                "title": event.title,
                "summary": (event.summary or "")[:400],
                "first_seen": event.first_seen_date.isoformat() if event.first_seen_date else "",
                "last_seen": event.last_seen_date.isoformat() if event.last_seen_date else "",
                "observations": event.observation_count,
            }
            # getattr: an event-shaped object without the v2.2 identity
            # columns (a legacy row, a test double) must still be matchable.
            for key in ("organization", "product_or_project", "event_type"):
                value = getattr(event, key, "")
                if value:
                    entry[key] = value
            if identity.reasons or identity.conflicts:
                entry["identity_score"] = score
                entry["identity_notes"] = identity.reason_text
            existing.append(entry)

        new_item = {
            "title": candidate.title,
            "fact_summary": candidate.fact_summary[:800],
            "topic": candidate.topic_name,
        }
        for key in ("organization", "product_or_project", "event_type"):
            value = getattr(candidate, key, "")
            if value:
                new_item[key] = value
        if getattr(candidate, "event_date", None):
            new_item["event_date"] = candidate.event_date.isoformat()
        if getattr(candidate, "entities", None):
            new_item["entities"] = list(candidate.entities)[:8]
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
        allowed = {event.id for event, _, _ in scored}
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
