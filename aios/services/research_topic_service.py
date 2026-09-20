"""Turning one sentence into a Research Brief - and editing it by talking.

Two operations, both of which write nothing:

:func:`refine`
    "帮我看看最近人形机器人有什么重要进展" → a compact brief the user can
    read and recognise.

:func:`revise`
    "以后多关注产业合作和商业化，少一点芯片参数" → the *same* brief, adjusted.
    Same topic, same id, same history. This is the part that matters: a
    conversational edit must not create a second unrelated topic and orphan
    every report the first one produced.

Routing goes through the existing :class:`~aios.services.llm.service.LLMService`
under the purposes ``research_brief`` and ``research_brief_edit``, so whichever
provider the user configured answers here too. There is no second API client.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from ..schemas.research_topic import (
    BRIEF_SCHEMA_EXAMPLE,
    MIN_DESCRIPTION_CHARS,
    BriefError,
    ResearchBrief,
    validate_brief,
)
from .llm import LLMError, ProviderNotConfigured
from .llm.service import LLMService, build_llm_service

logger = logging.getLogger(__name__)

#: Routing keys, also the ``purpose`` on every LLMUsage row written here.
PURPOSE_REFINE = "research_brief"
PURPOSE_REVISE = "research_brief_edit"

NO_PROVIDER_MESSAGE = "请先配置 AI 服务"


SYSTEM_PROMPT = f"""你是情报研究助手。用户用一句话说出他想关注什么，
你要把它整理成一份简洁、可执行的「研究主题」。

这份主题会交给一个联网研究 Agent 去执行，所以要写清楚研究什么，
但**不要**写检索式、不要写搜索语法、不要写 API、不要写数据源配置。
那些是系统的事，不是用户要看的东西。

写作要求：
1. name 要短、具体、像一个栏目名，不要写成一句话。
2. brief 说明研究目标：用户想知道什么样的情报。
3. scope 说明覆盖范围：哪些子领域、哪些技术方向、哪些主要厂商。
4. focus_areas 是 3-5 个关注维度（例如 技术进展 / 产品发布 / 商业落地）。
5. keywords 是值得重点盯的具体产品、技术或厂商名称。
6. exclusions 是应当排除的噪声（招聘、培训、促销、重复转载等）。
7. regions 用自然语言写地域范围，例如「中国 + 全球」。
8. window_hours 是时间窗口的小时数。日报类主题用 72，
   变化慢的主题可以用 168。

重要：
- 不要编造用户没有表达的兴趣。用户说的范围就是范围。
- 不要把主题写得过宽（"所有科技新闻"）或过窄（只能命中一条新闻）。
- 使用稳定的概念词，避免"最新""近期""2026年"这类会过期的措辞。
- 全部用中文书写。

只输出一个合法 JSON 对象，不要 Markdown 代码围栏，不要解释文字。"""


REVISE_SYSTEM_PROMPT = f"""你是情报研究助手，负责根据用户的一句话修改现有的「研究主题」。

规则：
1. 这是**修改**，不是重写。用户没有要求改动的部分必须原样保留。
2. 用户的意思通常是调整侧重，例如"多关注商业化、少一点芯片参数"，
   意味着 focus_areas / scope / keywords 需要相应调整，
   而不是把主题换成另一个领域。
3. 只有用户明确要求改名时才改 name。
4. 不要编造用户没有表达的新兴趣。
5. 不要写检索式或搜索语法。
6. 全部用中文书写。
7. 在 notes 里用一句话说明你改了什么，方便用户确认。

输出完整的修改后主题（所有字段都要给出，不只是改动的字段）。
只输出一个合法 JSON 对象。"""


@dataclass
class BriefResult:
    """One generated or revised brief, plus what the call cost."""

    brief: Optional[ResearchBrief] = None
    notes: str = ""
    provider_id: str = ""
    model: str = ""
    latency_ms: int = 0
    usage: list[dict] = field(default_factory=list)


class BriefGenerationError(RuntimeError):
    """Generation failed in a way worth showing the user verbatim."""


class ResearchTopicService:
    """Generates and revises research briefs through the configured provider."""

    def __init__(self, client: Optional[LLMService] = None) -> None:
        self._usage: list[dict] = []
        self.client = client or build_llm_service(usage_sink=self._usage.append)

    def readiness_error(self) -> str:
        """'' when a provider can serve brief generation, else a message."""
        try:
            return self.client.readiness_error()
        except Exception as exc:  # pragma: no cover - defensive
            return str(exc)

    def _call(self, messages: list[dict], purpose: str):
        try:
            response = self.client.complete_json(messages, purpose=purpose, max_tokens=2000)
        except ProviderNotConfigured as exc:
            raise BriefGenerationError(str(exc) or NO_PROVIDER_MESSAGE) from exc
        except LLMError as exc:
            raise BriefGenerationError(f"AI 生成失败：{exc}") from exc
        if not isinstance(response.data, dict) or not response.data:
            raise BriefGenerationError("AI 返回了空结果，请重试或换一种说法。")
        return response

    # -- one sentence -> a brief -----------------------------------------

    def refine(self, description: str) -> BriefResult:
        """Build a brief from a natural-language description. Writes nothing."""
        text = (description or "").strip()
        if len(text) < MIN_DESCRIPTION_CHARS:
            raise BriefGenerationError("请先用一句话说明你想研究什么。")

        user = f"""用户的描述：
{text}

请整理成一份研究主题。输出结构必须与下面一致（字段名不可更改，不可增加字段）：
{json.dumps(BRIEF_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)}
"""
        response = self._call(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            PURPOSE_REFINE,
        )

        try:
            brief = validate_brief(response.data)
        except BriefError as exc:
            logger.info("Generated brief rejected: %s", exc)
            raise BriefGenerationError(f"AI 生成的主题不完整：{exc}") from exc

        # A brief with no research goal is useless to the agent; fall back to
        # the user's own words rather than sending an empty instruction.
        if not brief.brief:
            brief = brief.model_copy(update={"brief": text[:2000]})

        return BriefResult(
            brief=brief,
            notes=brief.notes,
            provider_id=response.provider_id,
            model=response.model,
            latency_ms=response.latency_ms,
            usage=list(self._usage),
        )

    # -- natural-language edit of an existing brief ----------------------

    def revise(self, current: ResearchBrief, instruction: str) -> BriefResult:
        """Adjust an existing brief. The caller keeps the same topic id."""
        text = (instruction or "").strip()
        if len(text) < MIN_DESCRIPTION_CHARS:
            raise BriefGenerationError("请说明你想怎么调整这个主题。")

        user = f"""现有研究主题：
{json.dumps(_brief_payload(current), ensure_ascii=False, indent=2)}

用户要求的调整：
{text}

请输出修改后的完整主题，结构与下面一致（字段名不可更改，不可增加字段）：
{json.dumps(BRIEF_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)}
"""
        response = self._call(
            [
                {"role": "system", "content": REVISE_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            PURPOSE_REVISE,
        )

        try:
            revised = validate_brief(response.data)
        except BriefError as exc:
            logger.info("Revised brief rejected: %s", exc)
            raise BriefGenerationError(f"AI 修改结果不完整：{exc}") from exc

        # Anything the model dropped keeps its previous value: an edit must not
        # silently delete the parts of the brief the user did not mention.
        merged = revised.model_copy(
            update={
                "brief": revised.brief or current.brief,
                "scope": revised.scope or current.scope,
                "focus_areas": revised.focus_areas or current.focus_areas,
                "exclusions": revised.exclusions or current.exclusions,
                "keywords": revised.keywords or current.keywords,
                "regions": revised.regions or current.regions,
                "depth": revised.depth or current.depth,
            }
        )
        return BriefResult(
            brief=merged,
            notes=merged.notes,
            provider_id=response.provider_id,
            model=response.model,
            latency_ms=response.latency_ms,
            usage=list(self._usage),
        )


def _brief_payload(brief: ResearchBrief) -> dict:
    return {
        "name": brief.name,
        "brief": brief.brief,
        "scope": brief.scope,
        "focus_areas": brief.focus_areas,
        "keywords": brief.keywords,
        "exclusions": brief.exclusions,
        "regions": brief.regions,
        "window_hours": brief.window_hours,
    }


# --- a usable brief without a model -----------------------------------------

def fallback_brief(description: str) -> ResearchBrief:
    """A brief built from the user's words alone, with no model call.

    Used when the provider is unavailable but the user still pressed 开始研究.
    Deliberately literal: it reflects back exactly what they typed rather than
    inventing scope they never asked for. Worse than a generated brief, and far
    better than blocking the product on a model being reachable.
    """
    text = " ".join((description or "").split())[:2000]
    name = text[:24] or "新研究主题"
    return ResearchBrief.model_validate(
        {
            "name": name,
            "brief": text or name,
            "scope": "",
            "focus_areas": [],
            "exclusions": [],
            "keywords": [],
            "regions": "",
            "window_hours": 72,
        }
    )


# --- usage accounting -------------------------------------------------------

_USAGE_COLUMNS = {
    "purpose", "module_id", "topic_id", "provider_id", "model",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "latency_ms", "attempts", "success", "error_message", "extra_json",
}


def record_usage(session: Session, usage_rows: list[dict]) -> int:
    """Persist brief-generation LLM usage.

    ``run_id`` stays NULL: shaping a topic is not part of a research run, but it
    is still a real model call the user paid for. Professional mode shows it;
    Simple mode never mentions tokens.
    """
    from ..repositories import runs as runs_repo

    written = 0
    for fields in usage_rows or []:
        payload = {k: v for k, v in fields.items() if k in _USAGE_COLUMNS}
        payload.setdefault("purpose", PURPOSE_REFINE)
        try:
            runs_repo.add_usage(session, run_id=None, **payload)
            written += 1
        except Exception:  # accounting must never break the feature
            logger.debug("Could not record brief usage", exc_info=True)
    return written
