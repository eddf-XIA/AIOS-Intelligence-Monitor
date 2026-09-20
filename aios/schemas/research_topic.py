"""The validated shape of a Research Brief.

A brief is what 简易版 generates instead of a monitoring module. The difference
is the whole point of Simple mode: the Classic generator produces modules,
topics, queries and preferred sources - nine screens of configuration a normal
user has no way to evaluate. A brief is five fields they can read in ten
seconds and recognise as "yes, that is what I meant":

    全球人形机器人产业与技术进展
    全球 · 最近72小时
    技术 · 产品 · 开源 · 商业部署
    关注：机器人OS / VLA / 世界模型 / 主要厂商
    排除：招聘 · 培训 · 重复转载

Search expressions are deliberately absent. Deciding how to search is the
agent's job; the user's job is to say what they care about.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: Caps. A brief the user cannot read at a glance has failed at its one job.
MAX_FOCUS_AREAS = 8
MAX_EXCLUSIONS = 10
MAX_KEYWORDS = 12

MIN_WINDOW_HOURS = 6
MAX_WINDOW_HOURS = 24 * 30
DEFAULT_WINDOW_HOURS = 72

#: Shortest description we will attempt to build a brief from.
MIN_DESCRIPTION_CHARS = 4

_WS = re.compile(r"\s+")


def _clean(value: Any, limit: int) -> str:
    return _WS.sub(" ", str(value if value is not None else "").strip())[:limit]


def _list_field(value: Any, cap: int, item_limit: int = 80) -> list[str]:
    """A user-facing chip list: de-duplicated, trimmed, capped.

    Accepts the newline-separated text the edit form posts as well as the JSON
    array the model returns, so the same validator serves both paths.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[\n,，、;；]+", value)
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if isinstance(raw, dict):
            raw = raw.get("name") or raw.get("text") or raw.get("value") or ""
        text = _clean(raw, item_limit)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def parse_window(value: Any) -> int:
    """Read a time window from whatever the model or the form supplied.

    Accepts an hour count, ``"最近72小时"``, ``"7天"``, ``"3 days"`` - because
    a model asked for "time window" will produce all of those, and failing the
    whole brief over a unit would be absurd.
    """
    if value is None or value == "":
        return DEFAULT_WINDOW_HOURS
    if isinstance(value, bool):
        return DEFAULT_WINDOW_HOURS
    if isinstance(value, (int, float)):
        hours = int(value)
        return max(MIN_WINDOW_HOURS, min(hours, MAX_WINDOW_HOURS))

    text = _clean(value, 40).lower()
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return DEFAULT_WINDOW_HOURS
    number = float(match.group(1))
    if any(unit in text for unit in ("天", "day", "d")) and "小时" not in text:
        number *= 24
    elif any(unit in text for unit in ("周", "week", "w")):
        number *= 24 * 7
    elif any(unit in text for unit in ("月", "month")):
        number *= 24 * 30
    return max(MIN_WINDOW_HOURS, min(int(number), MAX_WINDOW_HOURS))


class ResearchBrief(BaseModel):
    """One research topic, in the terms a normal user thinks in."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=2, max_length=200)
    #: The research goal in prose - what the agent is actually asked to do.
    brief: str = Field(default="", max_length=2000)
    scope: str = Field(default="", max_length=2000)
    focus_areas: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    #: Named things to watch: 鸿蒙 / AI PC / VLA / 具身智能.
    keywords: list[str] = Field(default_factory=list)
    regions: str = Field(default="", max_length=200)
    window_hours: int = DEFAULT_WINDOW_HOURS
    depth: str = "standard"
    #: Optional note from the generator, shown once under the preview.
    notes: str = Field(default="", max_length=800)

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, value: Any) -> str:
        return _clean(value, 200)

    @field_validator("brief", "scope", mode="before")
    @classmethod
    def clean_prose(cls, value: Any) -> str:
        return _clean(value, 2000)

    @field_validator("regions", mode="before")
    @classmethod
    def clean_regions(cls, value: Any) -> str:
        if isinstance(value, (list, tuple)):
            value = " + ".join(str(v) for v in value if str(v).strip())
        return _clean(value, 200)

    @field_validator("notes", mode="before")
    @classmethod
    def clean_notes(cls, value: Any) -> str:
        return _clean(value, 800)

    @field_validator("focus_areas", mode="before")
    @classmethod
    def clean_focus(cls, value: Any) -> list[str]:
        return _list_field(value, MAX_FOCUS_AREAS)

    @field_validator("exclusions", mode="before")
    @classmethod
    def clean_exclusions(cls, value: Any) -> list[str]:
        return _list_field(value, MAX_EXCLUSIONS)

    @field_validator("keywords", mode="before")
    @classmethod
    def clean_keywords(cls, value: Any) -> list[str]:
        return _list_field(value, MAX_KEYWORDS)

    @field_validator("window_hours", mode="before")
    @classmethod
    def clean_window(cls, value: Any) -> int:
        return parse_window(value)

    @field_validator("depth", mode="before")
    @classmethod
    def clean_depth(cls, value: Any) -> str:
        text = _clean(value, 16).lower()
        return text if text in {"standard", "deep"} else "standard"

    # -- presentation ----------------------------------------------------

    @property
    def window_text(self) -> str:
        hours = self.window_hours or DEFAULT_WINDOW_HOURS
        # Hours up to a week, days beyond it. 72 reads as 最近72小时, which is
        # how a daily-brief user thinks about it; 最近3天 is the same duration
        # and the wrong unit. A week or more is genuinely easier to read in
        # days, so 168 becomes 最近7天.
        if hours >= 168 and hours % 24 == 0:
            return f"最近{hours // 24}天"
        return f"最近{hours}小时"

    @property
    def scope_line(self) -> str:
        """``全球 · 最近72小时`` - the compact preview's second line."""
        parts = [part for part in (self.regions, self.window_text) if part]
        return " · ".join(parts)

    @property
    def focus_line(self) -> str:
        return " · ".join(self.focus_areas)

    @property
    def exclusion_line(self) -> str:
        return " · ".join(self.exclusions)

    @property
    def keyword_line(self) -> str:
        return " / ".join(self.keywords)

    def as_changes(self) -> dict[str, Any]:
        """The subset :func:`...repositories.research_topics.update_topic` takes."""
        return {
            "name": self.name,
            "brief": self.brief,
            "scope": self.scope,
            "focus_areas": self.focus_areas,
            "exclusions": self.exclusions,
            "keywords": self.keywords,
            "regions": self.regions,
            "window_hours": self.window_hours,
            "depth": self.depth,
        }

    @classmethod
    def from_topic(cls, topic) -> "ResearchBrief":
        """Read a stored :class:`~aios.models.ResearchTopic` back into a brief."""
        return cls.model_validate(
            {
                "name": topic.name,
                "brief": topic.brief,
                "scope": topic.scope,
                "focus_areas": topic.focus_areas,
                "exclusions": topic.exclusions,
                "keywords": topic.keywords,
                "regions": topic.regions,
                "window_hours": topic.window_hours,
                "depth": topic.depth,
            }
        )


def coerce_brief_payload(payload: Any) -> dict:
    """Normalise a model's brief into the shape :class:`ResearchBrief` wants."""
    if not isinstance(payload, dict):
        return {}
    data = dict(payload)

    inner = data.get("topic") or data.get("brief_object") or data.get("research_topic")
    if isinstance(inner, dict):
        merged = dict(inner)
        for key in ("notes", "optional_notes"):
            if key in data and key not in merged:
                merged[key] = data[key]
        data = merged

    def pick(*names: str) -> Any:
        for name in names:
            if name in data and data[name] not in (None, ""):
                return data[name]
        return None

    return {
        "name": pick("name", "title", "topic_name", "research_topic") or "",
        "brief": pick("brief", "goal", "objective", "research_goal", "description") or "",
        "scope": pick("scope", "research_scope", "coverage") or "",
        "focus_areas": pick("focus_areas", "focus", "priorities", "重点") or [],
        "exclusions": pick("exclusions", "excluded", "exclude", "排除") or [],
        "keywords": pick("keywords", "watch_list", "entities", "关注") or [],
        "regions": pick("regions", "region", "geography", "地域") or "",
        "window_hours": pick("window_hours", "time_window", "window", "lookback_hours"),
        "depth": pick("depth", "research_depth") or "standard",
        "notes": pick("notes", "optional_notes", "note") or "",
    }


class BriefError(ValueError):
    """The brief could not be built or repaired into something usable."""


def validate_brief(payload: Any) -> ResearchBrief:
    """Repair then validate a generated brief."""
    try:
        return ResearchBrief.model_validate(coerce_brief_payload(payload))
    except ValidationError as exc:
        raise BriefError(first_error(exc)) from exc


def first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error.get("loc", ())) or "brief"
    return f"{location}: {error.get('msg', '内容无效')}"


#: The output contract shown to the model. An annotated example rather than a
#: JSON Schema, for the same reason as the research result: models follow
#: examples far more reliably, and the validator is what enforces correctness.
BRIEF_SCHEMA_EXAMPLE: dict[str, Any] = {
    "name": "简短的主题名称，6-16 个字，例如「全球人形机器人产业与技术进展」",
    "brief": "60-140字，说明这个主题要研究什么、希望得到什么样的情报",
    "scope": "60-160字，说明覆盖哪些子领域、哪些厂商、哪些技术方向",
    "focus_areas": ["技术进展", "产品发布", "开源项目", "商业落地"],
    "keywords": ["需要重点关注的产品/技术/厂商名称"],
    "exclusions": ["招聘", "培训", "重复转载", "纯营销"],
    "regions": "中国 + 全球",
    "window_hours": 72,
    "notes": "给用户的一句补充说明，可以为空",
}
