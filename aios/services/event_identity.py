"""Recognising the same real-world event described differently.

A research agent has no memory of yesterday's wording, so the same story
arrives reworded every day::

    day 1  Figure 发布 Helix 2
    day 2  Figure 推出新一代人形机器人智能系统 Helix 2
    day 3  Helix 2 获得新的工厂部署

Title equality answers "new event" three times and produces three unrelated
timelines - which destroys exactly the thing this product exists to provide.

So identity is treated as a *fingerprint* rather than a string. Six signals,
in descending order of how much they can be trusted:

1. **Shared canonical source URL.** Two descriptions citing the same document
   are the same story. This is the only signal strong enough to merge alone.
2. **Product/project name.** Stable across rewordings ("Helix 2" survives every
   variant above), and what a human would key on.
3. **Organisation.** Necessary but nowhere near sufficient - Huawei announces
   many unrelated things - so it only ever *supports* another signal.
4. **Event type.** Separates "发布 Helix 2" from "Helix 2 工厂部署": same
   product, genuinely different events. Used as much to *block* a merge as to
   make one.
5. **Event date proximity.** A continuing story stays warm; a year-old one does
   not.
6. **Lexical overlap** of title and summary, CJK-aware, from the existing
   matcher.

The bias is deliberate and unchanged from v2.1: **when in doubt, NEW_EVENT.**
A wrongly-split event is visible and recoverable; a wrongly-merged one corrupts
a timeline silently and is not.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

#: Score at or above which identity alone may merge without asking the model.
STRONG_IDENTITY_THRESHOLD = 0.82
#: Score below which a candidate is not even shown to the model.
IDENTITY_SHORTLIST_FLOOR = 0.18
#: Days beyond which date proximity contributes nothing.
DATE_PROXIMITY_DAYS = 21

#: Event types that describe genuinely different milestones for one product.
#: Two observations of the same product with *different* types are much more
#: likely to be two events than one, so the pair is penalised.
_DISTINCT_TYPES = {
    "product_launch",
    "partnership",
    "funding",
    "deployment",
    "research",
    "policy",
    "regulatory",
    "acquisition",
    "personnel",
    "incident",
    "financial",
}

_TYPE_ALIASES = {
    "launch": "product_launch",
    "release": "product_launch",
    "product": "product_launch",
    "announcement": "product_launch",
    "发布": "product_launch",
    "产品发布": "product_launch",
    "partnership": "partnership",
    "collaboration": "partnership",
    "合作": "partnership",
    "funding": "funding",
    "investment": "funding",
    "融资": "funding",
    "deployment": "deployment",
    "rollout": "deployment",
    "部署": "deployment",
    "商业落地": "deployment",
    "research": "research",
    "paper": "research",
    "研究": "research",
    "policy": "policy",
    "regulation": "regulatory",
    "政策": "policy",
    "acquisition": "acquisition",
    "merger": "acquisition",
    "收购": "acquisition",
    "earnings": "financial",
    "财报": "financial",
}

_TRACKING_PARAMS = re.compile(
    r"^(utm_|ref$|referrer$|source$|spm$|from$|share|fbclid$|gclid$|scm$)", re.IGNORECASE
)
_NON_WORD = re.compile(r"[^0-9a-z一-鿿]+")


def normalize_url(url: str) -> str:
    """A comparable form of a URL.

    Strips scheme, ``www.``, tracking parameters, fragments and a trailing
    slash, so the same article shared by two aggregators compares equal. The
    path is kept: two different articles on one domain must not collapse.
    """
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""

    query = "&".join(
        sorted(
            piece
            for piece in (parts.query or "").split("&")
            if piece and not _TRACKING_PARAMS.match(piece.split("=", 1)[0])
        )
    )
    path = (parts.path or "").rstrip("/")
    return urlunsplit(("", host, path, query, "")).lstrip("/") or host


def normalize_name(value: str) -> str:
    """Comparable form of an organisation or product name.

    Case, width, punctuation and spacing are all noise here: ``HarmonyOS 6``,
    ``harmonyos6`` and ``HarmonyOS　6`` are one product.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    # Corporate suffixes carry no identity: "Figure AI, Inc." is "Figure AI".
    text = re.sub(
        r"\b(inc|corp|corporation|co|ltd|limited|llc|plc|gmbh|sa|ag|group)\b\.?",
        " ",
        text,
    )
    text = text.replace("股份有限公司", "").replace("有限公司", "").replace("公司", "")
    return _NON_WORD.sub("", text)


def names_agree(left: str, right: str, min_length: int = 3) -> tuple[bool, bool]:
    """``(exact, related)`` for two normalised names.

    Agents are inconsistent about how much of a company name they include -
    "Figure", "Figure AI" and "Figure AI, Inc." are one organisation - so a
    containment relationship counts as *related*. The minimum length stops
    short keys matching half the world: "AI" must not be "related" to
    "AIOS", "OpenAI" and "Alibaba" all at once.
    """
    if not left or not right:
        return False, False
    if left == right:
        return True, True
    if len(left) < min_length or len(right) < min_length:
        return False, False
    return False, left in right or right in left


def normalize_type(value: str) -> str:
    """Canonical event type, or '' when the agent did not give a usable one."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    if not text:
        return ""
    if text in _DISTINCT_TYPES:
        return text
    mapped = _TYPE_ALIASES.get(text)
    if mapped:
        return mapped
    # A compound like "product_launch_announcement" still resolves.
    for known in _DISTINCT_TYPES:
        if known in text:
            return known
    for alias, mapped in _TYPE_ALIASES.items():
        if alias in text:
            return mapped
    return "other"


@dataclass
class EventIdentity:
    """The durable fingerprint of one event.

    Every field is optional. A Classic-pipeline candidate supplies none of them
    and therefore scores purely on lexical overlap, which is exactly how v2.1
    behaved - the new signals can only ever *add* confidence, never remove it.
    """

    title: str = ""
    summary: str = ""
    organization: str = ""
    product_or_project: str = ""
    event_type: str = ""
    event_date: Optional[dt.date] = None
    entities: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    # -- normalised views ------------------------------------------------

    @property
    def org_key(self) -> str:
        return normalize_name(self.organization)

    @property
    def product_key(self) -> str:
        return normalize_name(self.product_or_project)

    @property
    def type_key(self) -> str:
        return normalize_type(self.event_type)

    @property
    def url_keys(self) -> set[str]:
        return {key for key in (normalize_url(u) for u in self.urls) if key}

    @property
    def entity_keys(self) -> set[str]:
        keys = {normalize_name(e) for e in self.entities}
        keys.discard("")
        return keys

    @property
    def has_signals(self) -> bool:
        """Whether anything beyond free text is available to match on."""
        return bool(self.org_key or self.product_key or self.url_keys or self.entity_keys)

    def merged_urls(self, extra: Sequence[str]) -> list[str]:
        """This identity's URLs plus ``extra``, de-duplicated and capped.

        Capped because the list is persisted on the event and a long-running
        storyline would otherwise accumulate hundreds of URLs, most of them
        far too old to still be useful as a matching signal.
        """
        out: list[str] = []
        seen: set[str] = set()
        for url in list(self.urls) + list(extra or []):
            key = normalize_url(url)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(url)
            if len(out) >= 40:
                break
        return out


@dataclass
class IdentityMatch:
    """How strongly one candidate resembles one existing event."""

    score: float
    reasons: list[str] = field(default_factory=list)
    #: A signal that actively argues *against* merging (e.g. same product but a
    #: clearly different milestone).
    conflicts: list[str] = field(default_factory=list)
    #: The pure lexical overlap that fed into ``score``. Carried through so the
    #: deterministic fallback keeps judging on wording alone - blending the
    #: fingerprint into its threshold would change v2.1 behaviour for every
    #: Classic candidate, which has no fingerprint to contribute.
    lexical: float = 0.0

    @property
    def is_strong(self) -> bool:
        """Strong enough to merge without consulting the model."""
        return self.score >= STRONG_IDENTITY_THRESHOLD and not self.conflicts

    @property
    def reason_text(self) -> str:
        parts = list(self.reasons)
        if self.conflicts:
            parts.append("但：" + "、".join(self.conflicts))
        return "；".join(parts)


def _date_proximity(left: Optional[dt.date], right: Optional[dt.date]) -> Optional[float]:
    """0..1 closeness of two event dates, or None when either is unknown.

    None rather than 0 on purpose: "we do not know when this happened" is not
    evidence that the dates differ, and scoring it as a mismatch would suppress
    correct merges for every agent that omits dates.
    """
    if left is None or right is None:
        return None
    days = abs((left - right).days)
    if days == 0:
        return 1.0
    if days >= DATE_PROXIMITY_DAYS:
        return 0.0
    return 1.0 - (days / DATE_PROXIMITY_DAYS)


def compare_identity(
    candidate: EventIdentity, existing: EventIdentity, lexical: float = 0.0
) -> IdentityMatch:
    """Score how likely it is that these two descriptions are one event.

    ``lexical`` is the CJK-aware title/summary overlap from
    :mod:`aios.services.event_matcher`, passed in so the two scorers agree on
    one number rather than each inventing its own.
    """
    reasons: list[str] = []
    conflicts: list[str] = []

    shared_urls = candidate.url_keys & existing.url_keys
    if shared_urls:
        # The same source document. Nothing else can outrank this.
        return IdentityMatch(
            score=1.0,
            reasons=[f"引用了同一篇来源（{len(shared_urls)} 个 URL 相同）"],
            lexical=lexical,
        )

    score = 0.0

    same_product, related_product = names_agree(
        candidate.product_key, existing.product_key
    )
    same_org, related_org = names_agree(candidate.org_key, existing.org_key)
    shared_entities = candidate.entity_keys & existing.entity_keys

    if same_product:
        score += 0.46
        reasons.append(
            f"同一产品/项目（{existing.product_or_project or candidate.product_or_project}）"
        )
    elif related_product:
        score += 0.30
        reasons.append("产品/项目名相关")
    elif candidate.product_key and existing.product_key:
        # Both named a product and they are unrelated. That is a real signal
        # that these are separate stories, not merely a missing one.
        conflicts.append("产品/项目不同")

    if same_org or related_org:
        # Supporting evidence only: one company does many unrelated things.
        weight = 0.20 if same_product or related_product else 0.12
        score += weight if same_org else weight * 0.75
        reasons.append(f"同一主体（{existing.organization or candidate.organization}）")

    if shared_entities:
        score += min(0.12, 0.04 * len(shared_entities))
        reasons.append(f"共同涉及 {len(shared_entities)} 个相关主体")

    candidate_type, existing_type = candidate.type_key, existing.type_key
    if candidate_type and existing_type:
        if candidate_type == existing_type:
            score += 0.10
            if candidate_type != "other":
                reasons.append("同类事件")
        elif "other" not in (candidate_type, existing_type):
            # Same product, different milestone: "发布" then "工厂部署" are two
            # events in one storyline, and conflating them would lose the
            # second one's own timeline.
            conflicts.append("事件类型不同")

    proximity = _date_proximity(candidate.event_date, existing.event_date)
    if proximity is not None:
        score += 0.12 * proximity
        if proximity >= 0.9:
            reasons.append("事件日期接近")
        elif proximity == 0.0:
            conflicts.append("事件日期相差较远")

    # Lexical overlap still counts, but it can no longer carry a merge alone.
    score += 0.30 * max(0.0, min(1.0, lexical))
    if lexical >= 0.5:
        reasons.append(f"标题高度重合（{lexical:.2f}）")

    return IdentityMatch(
        score=round(max(0.0, min(1.0, score)), 3),
        reasons=reasons,
        conflicts=conflicts,
        lexical=round(max(0.0, min(1.0, lexical)), 3),
    )


# --- building identities from the domain objects ----------------------------

def identity_from_event(event, urls: Optional[Sequence[str]] = None) -> EventIdentity:
    """The stored fingerprint of an :class:`~aios.models.IntelligenceEvent`."""
    stored_urls = list(getattr(event, "canonical_urls_json", None) or [])
    return EventIdentity(
        title=getattr(event, "title", "") or "",
        summary=getattr(event, "summary", "") or "",
        organization=getattr(event, "organization", "") or "",
        product_or_project=getattr(event, "product_or_project", "") or "",
        event_type=getattr(event, "event_type", "") or "",
        event_date=getattr(event, "last_seen_date", None),
        entities=[str(x) for x in (getattr(event, "entities_json", None) or [])],
        urls=[str(u) for u in stored_urls] + [str(u) for u in (urls or [])],
    )


def identity_from_candidate(candidate) -> EventIdentity:
    """The fingerprint of a :class:`CandidateIntelligence` about to be recorded.

    Reads through ``getattr`` so a candidate built by the Classic pipeline -
    which has none of these attributes populated - yields an identity with no
    signals rather than raising.
    """
    return EventIdentity(
        title=getattr(candidate, "title", "") or "",
        summary=getattr(candidate, "fact_summary", "") or "",
        organization=getattr(candidate, "organization", "") or "",
        product_or_project=getattr(candidate, "product_or_project", "") or "",
        event_type=getattr(candidate, "event_type", "") or "",
        event_date=getattr(candidate, "event_date", None),
        entities=list(getattr(candidate, "entities", None) or []),
        urls=list(getattr(candidate, "source_urls", None) or []),
    )


def describe_identity(identity: EventIdentity) -> dict[str, Any]:
    """Storable form of a fingerprint, for the event's identity columns."""
    return {
        "organization": (identity.organization or "")[:200],
        "product_or_project": (identity.product_or_project or "")[:200],
        "event_type": normalize_type(identity.event_type)[:64] if identity.event_type else "",
        "entities_json": identity.entities[:12] or None,
        "canonical_urls_json": identity.merged_urls([]) or None,
    }
