"""Turn one sentence into a monitoring configuration draft.

This is an *accelerator* for the manual editor, not a replacement for it. The
model proposes; :mod:`aios.schemas.config_generation` validates and budgets;
the user reviews and edits; only :func:`apply_module_draft` writes anything.

Routing goes through the existing :class:`~aios.services.llm.service.LLMService`
under the purpose key ``config_generation``, so whichever provider the user has
configured - DeepSeek, Qwen, GLM, Kimi, Doubao, MiniMax, OpenAI, Claude,
Gemini, a local Ollama - answers here too. No second API client exists.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..repositories import modules as modules_repo
from ..repositories import runs as runs_repo
from ..repositories import topics as topics_repo
from ..schemas.config_generation import (
    LIMITS,
    MAX_QUERIES_PER_TOPIC,
    MAX_TOPICS_PER_MODULE,
    MAX_TOTAL_QUERIES,
    GeneratedMonitorConfig,
    GeneratedTopic,
    GeneratedTopicOnly,
    normalize_query,
    slugify_key,
)
from .llm import LLMError, ProviderNotConfigured
from .llm.service import LLMService, build_llm_service

logger = logging.getLogger(__name__)

#: Routing key. Also the ``purpose`` recorded on every LLMUsage row.
PURPOSE = "config_generation"

NO_PROVIDER_MESSAGE = "请先配置 AI 模型"


SYSTEM_PROMPT = f"""你是情报监测系统的配置助手。用户用一句话描述想监测的领域，
你要输出一份可直接执行的监测配置。

必须遵守的规则：

1. 生成的是**检索式**，不是文章、不是句子。每条检索式是能直接投给新闻搜索引擎的
   关键词组合，通常 2-6 个词。不要写成问句或论述。
2. 当被监测领域具有国际相关性时，中文检索式与英文检索式都要有；
   纯本地化议题可以只用中文。
3. 不要生成大量互相重叠的检索式。同一主题内的检索式必须覆盖不同角度。
4. 不要过窄（只能命中一条特定新闻），也不要过宽（大部分结果与该领域无关）。
5. 优先使用稳定的概念词（技术名、产品线、标准名、厂商名），
   避免"最新""近期""2026 年"这类会过期的措辞。
6. preferred_sources 填真实存在的媒体/厂商/机构域名，只写域名本身
   （例如 huawei.com、reuters.com）。这些只是建议来源，系统不会声称它们已被验证可用，
   也不要声称它们提供 API。
7. 绝对不要编造 API Key、账号、令牌，不要给出包含任何凭据的 URL，
   不要假设用户拥有付费数据源或内部数据源。
8. excluded_keywords 用于排除招聘、促销、二手交易一类噪声，可以为空。
9. analysis_instructions 用中文写清楚该主题应该关注什么、忽略什么。

预算约束（硬性）：
- 每个模块最多 {MAX_TOPICS_PER_MODULE} 个主题，建议 3-4 个。
- 每个主题最多 {MAX_QUERIES_PER_TOPIC} 条检索式，**建议 2-3 条**。
- 一次生成最多 {MAX_TOTAL_QUERIES} 条检索式。
每条检索式都会产生真实的网络请求，所以宁可少而准，不要多而重复。

只输出一个合法 JSON 对象，不要 Markdown 代码围栏，不要解释文字。"""


MODULE_SCHEMA: dict[str, Any] = {
    "name": "模块名称，例如「人形机器人操作系统」",
    "key": "lowercase_ascii_key",
    "description": "40-120 字，说明这个模块监测什么",
    "topics": [
        {
            "name": "主题名称",
            "description": "该主题的监测范围",
            "queries": [{"query": "检索关键词", "priority": 5}],
            "preferred_sources": [{"domain": "example.com", "reason": "为什么值得优先"}],
            "excluded_keywords": ["招聘"],
            "analysis_instructions": "该主题分析时应关注什么",
        }
    ],
    "recommended_lookback_days": 3,
    "recommended_report_limit": 2,
    "analysis_instructions": "整个模块共用的分析要求",
    "optional_notes": "给用户的补充说明，可为空",
}

TOPIC_SCHEMA: dict[str, Any] = {
    "topic": MODULE_SCHEMA["topics"][0],
    "optional_notes": "给用户的补充说明，可为空",
}


class ConfigGenerationError(RuntimeError):
    """Generation failed in a way worth showing the user verbatim."""


@dataclass
class GenerationResult:
    """One successful generation, plus what it cost and what was trimmed."""

    config: Optional[GeneratedMonitorConfig] = None
    topic: Optional[GeneratedTopic] = None
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    provider_id: str = ""
    model: str = ""
    latency_ms: int = 0
    usage: list[dict] = field(default_factory=list)


# --- prompt construction ----------------------------------------------------

def _module_messages(description: str, existing_keys: list[str]) -> list[dict]:
    taken = "、".join(existing_keys[:30]) or "（无）"
    user = f"""用户的一句话描述：
{description}

已被占用的模块标识（key 不要与这些重复）：{taken}

输出结构必须与下面一致（字段名不可更改，不可增加字段）：
{json.dumps(MODULE_SCHEMA, ensure_ascii=False, indent=2)}
"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _topic_messages(
    description: str, module_name: str, module_description: str, existing_topics: list[str]
) -> list[dict]:
    taken = "、".join(existing_topics[:30]) or "（无）"
    user = f"""用户想在现有监测模块中新增**一个**主题。

现有模块：{module_name}
模块说明：{module_description or "（无）"}
该模块已有主题（不要重复，也不要修改它们）：{taken}

用户的一句话描述：
{description}

只输出这一个新增主题，不要输出模块级字段，不要重新生成已有主题。
输出结构必须与下面一致（字段名不可更改，不可增加字段）：
{json.dumps(TOPIC_SCHEMA, ensure_ascii=False, indent=2)}
"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# --- generation -------------------------------------------------------------

class ConfigGenerator:
    """Generates configuration drafts through the configured LLM provider."""

    def __init__(self, client: Optional[LLMService] = None) -> None:
        self._usage: list[dict] = []
        self.client = client or build_llm_service(usage_sink=self._usage.append)

    def readiness_error(self) -> str:
        """'' when a provider can serve ``config_generation``, else a message."""
        try:
            error = self.client.readiness_error()
        except Exception as exc:  # pragma: no cover - defensive
            return str(exc)
        return error

    def _call(self, messages: list[dict]) -> tuple[dict, Any]:
        try:
            response = self.client.complete_json(
                messages, purpose=PURPOSE, max_tokens=3600
            )
        except ProviderNotConfigured as exc:
            raise ConfigGenerationError(str(exc) or NO_PROVIDER_MESSAGE) from exc
        except LLMError as exc:
            raise ConfigGenerationError(f"AI 生成失败：{exc}") from exc
        if not isinstance(response.data, dict) or not response.data:
            raise ConfigGenerationError("AI 返回了空的配置，请重试或换一种描述。")
        return response.data, response

    def generate_module(
        self, description: str, existing_keys: Optional[list[str]] = None
    ) -> GenerationResult:
        """One sentence -> a full module draft. Never touches the database."""
        text = (description or "").strip()
        if len(text) < 4:
            raise ConfigGenerationError("请先用一句话描述你想监测的内容。")

        payload, response = self._call(_module_messages(text, existing_keys or []))
        payload = _coerce_module_payload(payload)
        try:
            config = GeneratedMonitorConfig.model_validate(payload)
        except ValidationError as exc:
            logger.info("Generated config rejected: %s", exc)
            raise ConfigGenerationError(
                f"AI 生成的配置不合法：{_first_error(exc)}"
            ) from exc

        if config.key in set(existing_keys or []):
            config = config.model_copy(update={"key": _unique_key(config.key, existing_keys or [])})

        return GenerationResult(
            config=config,
            notes=config.optional_notes,
            warnings=_budget_warnings(payload, config),
            provider_id=response.provider_id,
            model=response.model,
            latency_ms=response.latency_ms,
            usage=list(self._usage),
        )

    def generate_topic(
        self,
        description: str,
        module_name: str,
        module_description: str = "",
        existing_topics: Optional[list[str]] = None,
    ) -> GenerationResult:
        """One sentence -> a single extra topic for an existing module."""
        text = (description or "").strip()
        if len(text) < 4:
            raise ConfigGenerationError("请先用一句话描述你想新增的主题。")

        payload, response = self._call(
            _topic_messages(text, module_name, module_description, existing_topics or [])
        )
        payload = _coerce_topic_payload(payload)
        try:
            generated = GeneratedTopicOnly.model_validate(payload)
        except ValidationError as exc:
            logger.info("Generated topic rejected: %s", exc)
            raise ConfigGenerationError(
                f"AI 生成的主题不合法：{_first_error(exc)}"
            ) from exc

        taken = {name.lower() for name in (existing_topics or [])}
        if generated.topic.name.lower() in taken:
            raise ConfigGenerationError(
                f"该模块已存在同名主题「{generated.topic.name}」，请换一种描述。"
            )

        return GenerationResult(
            topic=generated.topic,
            notes=generated.optional_notes,
            warnings=[],
            provider_id=response.provider_id,
            model=response.model,
            latency_ms=response.latency_ms,
            usage=list(self._usage),
        )


def _first_error(exc: ValidationError) -> str:
    from ..schemas.config_generation import first_error

    return first_error(exc)


def _unique_key(key: str, taken: list[str]) -> str:
    existing = set(taken)
    if key not in existing:
        return key
    for suffix in range(2, 100):
        candidate = f"{key}_{suffix}"[:63]
        if candidate not in existing:
            return candidate
    return f"{key}_x"[:63]


def _budget_warnings(raw: dict, config: GeneratedMonitorConfig) -> list[str]:
    """Tell the user what the budget silently removed."""
    warnings: list[str] = []
    raw_topics = raw.get("topics") or []
    if isinstance(raw_topics, list) and len(raw_topics) > len(config.topics):
        warnings.append(
            f"AI 生成了 {len(raw_topics)} 个主题，已按上限保留前 {len(config.topics)} 个。"
        )
    raw_queries = 0
    if isinstance(raw_topics, list):
        for topic in raw_topics:
            if isinstance(topic, dict) and isinstance(topic.get("queries"), list):
                raw_queries += len(topic["queries"])
    if raw_queries > config.total_queries:
        warnings.append(
            f"AI 生成了 {raw_queries} 条检索式，已裁剪到 {config.total_queries} 条"
            f"（单次上限 {MAX_TOTAL_QUERIES} 条）。"
        )
    return warnings


# --- payload tolerance ------------------------------------------------------

def _coerce_module_payload(payload: dict) -> dict:
    """Accept the small shape variations models reliably produce.

    The repair loop in the provider layer already handles *malformed JSON*.
    This handles *valid JSON with a slightly different shape* - a string where a
    list of objects was asked for, ``module`` wrapping the real body, a missing
    key - without letting genuinely wrong content through.
    """
    if not isinstance(payload, dict):
        return {}
    data = dict(payload)

    inner = data.get("module")
    if isinstance(inner, dict):
        merged = dict(inner)
        for field_name in ("topics", "recommended_lookback_days", "recommended_report_limit",
                           "optional_notes", "analysis_instructions"):
            if field_name in data and field_name not in merged:
                merged[field_name] = data[field_name]
        data = merged

    if "name" not in data and "module_name" in data:
        data["name"] = data.pop("module_name")
    if "key" not in data and "slug" in data:
        data["key"] = data.pop("slug")
    data.pop("slug", None)
    data.pop("module_name", None)

    if not str(data.get("key") or "").strip():
        data["key"] = slugify_key(str(data.get("name") or ""))

    topics = data.get("topics")
    if isinstance(topics, list):
        data["topics"] = [_coerce_topic(t) for t in topics if t]

    for numeric, default in (("recommended_lookback_days", 3), ("recommended_report_limit", 2)):
        try:
            data[numeric] = int(data.get(numeric, default) or default)
        except (TypeError, ValueError):
            data[numeric] = default

    allowed = set(GeneratedMonitorConfig.model_fields)
    return {k: v for k, v in data.items() if k in allowed}


def _coerce_topic_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = dict(payload)
    topic = data.get("topic")
    if not isinstance(topic, dict):
        # The model answered with a bare topic object instead of wrapping it.
        topic = {k: v for k, v in data.items() if k != "optional_notes"}
    return {
        "topic": _coerce_topic(topic),
        "optional_notes": str(data.get("optional_notes") or "")[:800],
    }


def _coerce_topic(topic: Any) -> dict:
    if not isinstance(topic, dict):
        return {}
    data = dict(topic)

    if "name" not in data and "topic" in data and isinstance(data["topic"], str):
        data["name"] = data.pop("topic")

    queries = data.get("queries") or data.pop("search_queries", None) or []
    normalized_queries = []
    if isinstance(queries, list):
        for item in queries:
            if isinstance(item, str):
                normalized_queries.append({"query": item, "priority": 5})
            elif isinstance(item, dict):
                text = item.get("query") or item.get("q") or item.get("text") or ""
                try:
                    priority = int(item.get("priority", 5) or 5)
                except (TypeError, ValueError):
                    priority = 5
                normalized_queries.append({"query": text, "priority": max(0, min(10, priority))})
    data["queries"] = [q for q in normalized_queries if str(q.get("query") or "").strip()]

    sources = data.get("preferred_sources") or data.pop("sources", None) or []
    normalized_sources = []
    if isinstance(sources, list):
        for item in sources:
            if isinstance(item, str):
                normalized_sources.append({"domain": item, "reason": ""})
            elif isinstance(item, dict):
                domain = item.get("domain") or item.get("url") or item.get("source") or ""
                normalized_sources.append(
                    {"domain": domain, "reason": str(item.get("reason") or "")[:200]}
                )
    # A domain the model got wrong should not sink an otherwise good draft:
    # suggestions are dropped individually, queries are not.
    data["preferred_sources"] = [
        s for s in normalized_sources if _domain_is_usable(s.get("domain", ""))
    ]

    exclusions = data.get("excluded_keywords") or []
    if isinstance(exclusions, str):
        exclusions = [exclusions]
    data["excluded_keywords"] = [str(x) for x in exclusions if str(x or "").strip()]

    if "analysis_instructions" not in data:
        for alias in ("analysis_prompt", "instructions"):
            if alias in data:
                data["analysis_instructions"] = data.pop(alias)
                break

    allowed = set(GeneratedTopic.model_fields)
    return {k: v for k, v in data.items() if k in allowed}


def _domain_is_usable(value: str) -> bool:
    from ..schemas.config_generation import GeneratedSource

    try:
        GeneratedSource.model_validate({"domain": value, "reason": ""})
    except ValidationError:
        return False
    return True


# --- applying a draft -------------------------------------------------------

@dataclass
class ApplyResult:
    """What a transactional apply created."""

    module_id: Optional[int] = None
    topic_ids: list[int] = field(default_factory=list)
    query_count: int = 0
    message: str = ""


class ApplyError(RuntimeError):
    """The draft could not be applied; the caller must roll back."""


def apply_module_draft(session: Session, config: GeneratedMonitorConfig) -> ApplyResult:
    """Create the module, its topics, queries, sources and exclusions.

    Raises :class:`ApplyError` before writing anything the caller would have to
    clean up by hand. The caller is responsible for the transaction boundary -
    every checked failure is raised, never half-committed.
    """
    if modules_repo.key_exists(session, config.key):
        raise ApplyError(f"模块标识「{config.key}」已存在，请修改后再应用。")

    seen_topics: set[str] = set()
    seen_queries: set[str] = set()
    for topic in config.topics:
        lowered = topic.name.lower()
        if lowered in seen_topics:
            raise ApplyError(f"主题名称重复：「{topic.name}」。")
        seen_topics.add(lowered)
        for query in topic.queries:
            key = normalize_query(query.query)
            if not key:
                raise ApplyError(f"主题「{topic.name}」包含空检索式。")
            seen_queries.add(key)

    module = modules_repo.create_module(
        session,
        key=config.key,
        name=config.name,
        description=config.description,
        enabled=True,
        sort_order=modules_repo.next_sort_order(session),
        lookback_days=config.recommended_lookback_days,
        max_candidates=18,
        max_report_items=config.recommended_report_limit,
        analysis_prompt=config.analysis_instructions,
    )

    result = ApplyResult(module_id=module.id)
    for order, topic in enumerate(config.topics):
        topic_id = _create_topic(session, module.id, topic, sort_order=(order + 1) * 10)
        result.topic_ids.append(topic_id)
        result.query_count += len(topic.queries)

    session.flush()
    result.message = (
        f"已创建模块「{config.name}」：{len(result.topic_ids)} 个主题，"
        f"{result.query_count} 条检索式。"
    )
    return result


def apply_topic_draft(session: Session, module_id: int, topic: GeneratedTopic) -> ApplyResult:
    """Add one generated topic to an existing module.

    Only the new topic is written. Existing topics, queries, sources and the
    module's own settings are left exactly as they were.
    """
    module = modules_repo.get_module(session, module_id)
    if module is None:
        raise ApplyError("模块不存在。")

    existing = {t.name.lower() for t in module.topics if not t.archived}
    if topic.name.lower() in existing:
        raise ApplyError(f"主题「{topic.name}」已存在。")
    if not topic.queries:
        raise ApplyError(f"主题「{topic.name}」没有检索式。")

    topic_id = _create_topic(
        session, module_id, topic, sort_order=topics_repo.next_sort_order(session, module_id)
    )
    session.flush()
    return ApplyResult(
        module_id=module_id,
        topic_ids=[topic_id],
        query_count=len(topic.queries),
        message=f"已添加主题「{topic.name}」：{len(topic.queries)} 条检索式。",
    )


def _create_topic(
    session: Session, module_id: int, topic: GeneratedTopic, sort_order: int
) -> int:
    row = topics_repo.create_topic(
        session,
        module_id=module_id,
        name=topic.name,
        description=topic.description,
        enabled=True,
        sort_order=sort_order,
        analysis_prompt=topic.analysis_instructions,
    )
    for query in topic.queries:
        topics_repo.create_query(session, row.id, query.query, priority=query.priority)
    for source in topic.preferred_sources:
        topics_repo.add_preferred_source(session, source.domain, topic_id=row.id, priority=5)
    for keyword in topic.excluded_keywords:
        topics_repo.add_excluded_keyword(session, keyword, topic_id=row.id)
    session.flush()
    return row.id


# --- usage accounting -------------------------------------------------------

def record_usage(session: Session, usage_rows: list[dict]) -> int:
    """Persist ``config_generation`` LLM usage. Never stores credentials.

    ``run_id`` stays NULL: configuration generation is not part of a monitoring
    run, but it is still a real model call the user paid for.
    """
    written = 0
    for fields in usage_rows or []:
        payload = {k: v for k, v in fields.items() if k in _USAGE_COLUMNS}
        payload.setdefault("purpose", PURPOSE)
        try:
            runs_repo.add_usage(session, run_id=None, **payload)
            written += 1
        except Exception:  # accounting must never break the feature
            logger.debug("Could not record config_generation usage", exc_info=True)
    return written


_USAGE_COLUMNS = {
    "purpose", "module_id", "topic_id", "provider_id", "model",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "latency_ms", "attempts", "success", "error_message", "extra_json",
}


def generation_limits() -> dict[str, int]:
    return dict(LIMITS)


def build_generator(usage_sink: Optional[Callable[[dict], None]] = None) -> ConfigGenerator:
    """Factory mirroring :func:`aios.services.llm.service.build_llm_service`."""
    if usage_sink is None:
        return ConfigGenerator()
    return ConfigGenerator(client=build_llm_service(usage_sink=usage_sink))
