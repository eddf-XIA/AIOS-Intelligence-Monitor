"""Turning evidence into intelligence.

The prompts here are an extension of the ones that already produced good output
in the original script. The invariants they enforce are unchanged and must stay
that way:

* only the supplied evidence may be used - no completion from model memory,
* fact summary and assessment are separate fields, never merged,
* an empty result is a valid result; padding the report is forbidden,
* every item cites the evidence ids it came from.

New in this version: ``structured_data`` for metrics that literally appear in
the evidence, so the Diff engine has numbers to compare across days.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .llm import LLMError
from .llm.service import LLMService

logger = logging.getLogger(__name__)


TOPIC_SYSTEM_PROMPT = """你是操作系统、AI 基础设施与智能终端领域的情报分析员。

规则：
1. 只能使用用户给出的证据；证据没有写的数字、日期、因果关系一律不得补全。
2. 优先一手/官方来源，其次权威媒体；营销软文和转载降低置信度。
3. "事实摘要"(fact_summary) 和 "情报判断"(assessment) 必须分开。
   fact_summary 只写证据明确陈述的内容。
   assessment 必须使用"判断/预计/值得关注/可能"等措辞，明确这是研判而非事实。
4. 如果本期证据不足以证明有新增，items 返回空数组；绝不为了填栏目而编造事件。
5. source_ids 必须引用证据中真实存在的 id。
6. structured_data 只允许提取证据原文中明确出现的数字指标。
   没有数字就返回空对象 {}。绝对禁止估算、换算出证据中不存在的数值。
7. 只输出合法 JSON，不要 Markdown，不要代码围栏。"""


MODULE_SYSTEM_PROMPT = """你是资深科技产业情报编辑，负责一个监测领域的当期小结。
只能基于给定的本领域分析结果撰写，不得引入新事实或新数字。
如果本领域没有值得记录的新增动态，summary 明确写"本期未检出高置信新增动态"。
只输出合法 JSON。"""


SYNTHESIS_SYSTEM_PROMPT = """你是资深科技产业情报编辑。只基于给定的分领域分析做二次综合。
不要引入新事实或新数字。趋势研判必须明确是研判，而不是把推断写成事实。
如果没有足够事实，不要凑数。只输出合法 JSON。"""


TOPIC_SCHEMA: dict[str, Any] = {
    "status": "new|watch",
    "items": [
        {
            "tag": "短标签",
            "title": "不夸张的标题",
            "fact_summary": "120-260字事实摘要，只写证据明确陈述的内容",
            "assessment": "40-100字情报判断，必须明确这是判断而非事实",
            "importance": 1,
            "confidence": "high|medium|low",
            "is_correction": False,
            "source_ids": [0],
            "structured_data": {
                "指标英文键名": {
                    "value": 87200000,
                    "display": "8720万",
                    "unit": "devices",
                    "source_id": 0,
                }
            },
        }
    ],
    "metrics": [{"value": "可直接展示的数字", "label": "数字含义", "source_ids": [0]}],
}


MODULE_SCHEMA: dict[str, Any] = {
    "status": "new|watch",
    "summary": "80-160字本领域当期小结",
    "metrics": [{"value": "数字", "label": "指标含义"}],
}


SYNTHESIS_SCHEMA: dict[str, Any] = {
    "headline": {"title": "今日头条", "body": "100-180字", "section": "来源领域名"},
    "trends": [{"title": "趋势一：...", "body": "100-180字", "confidence": "high|medium"}],
    "metrics": [{"value": "数字", "label": "指标", "section": "领域名"}],
}


@dataclass
class EvidenceItem:
    """One article, presented to the model under a stable local id."""

    id: int
    article_id: int
    title: str
    source: str
    published_at: str
    url: str
    text: str

    def as_prompt_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "published_at": self.published_at,
            "url": self.url,
            "text": self.text,
        }


@dataclass
class CandidateIntelligence:
    """A model-proposed report item, before event matching."""

    tag: str
    title: str
    fact_summary: str
    assessment: str
    importance: int
    confidence: str
    article_ids: list[int]
    structured_data: dict[str, Any] = field(default_factory=dict)
    is_correction: bool = False
    topic_id: Optional[int] = None
    topic_name: str = ""
    module_id: Optional[int] = None


def build_evidence(articles, max_chars: int = 7000) -> list[EvidenceItem]:
    """Convert stored articles into the numbered evidence list the prompt uses."""
    evidence: list[EvidenceItem] = []
    for index, article in enumerate(articles):
        evidence.append(
            EvidenceItem(
                id=index,
                article_id=article.id,
                title=article.title or "",
                source=article.source or article.domain or "",
                published_at=article.published_raw or (
                    article.published_at.isoformat() if article.published_at else ""
                ),
                url=article.url or "",
                text=(article.evidence_text or "")[:max_chars],
            )
        )
    return evidence


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_structured(raw: Any, evidence: list[EvidenceItem]) -> dict[str, Any]:
    """Keep only well-formed metric entries that cite real evidence.

    A metric without a usable ``value`` is dropped rather than guessed at.
    """
    if not isinstance(raw, dict):
        return {}
    valid_ids = {item.id for item in evidence}
    cleaned: dict[str, Any] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(entry, dict):
            continue
        if "value" not in entry and "display" not in entry:
            continue
        record: dict[str, Any] = {}
        if "value" in entry:
            record["value"] = entry["value"]
        record["display"] = str(entry.get("display", entry.get("value", "")))
        if entry.get("unit"):
            record["unit"] = str(entry["unit"])
        source_id = entry.get("source_id")
        if isinstance(source_id, int) and source_id in valid_ids:
            record["source_id"] = source_id
        cleaned[key.strip()] = record
    return cleaned


def parse_topic_items(
    payload: dict, evidence: list[EvidenceItem], max_items: int
) -> list[CandidateIntelligence]:
    """Validate the model payload and map evidence ids back to article ids."""
    by_evidence_id = {item.id: item.article_id for item in evidence}
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []

    results: list[CandidateIntelligence] = []
    for raw in raw_items[: max(max_items, 0)]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        fact = str(raw.get("fact_summary") or raw.get("summary") or "").strip()
        if not title or not fact:
            continue

        source_ids = raw.get("source_ids")
        article_ids: list[int] = []
        if isinstance(source_ids, list):
            for sid in source_ids:
                if isinstance(sid, int) and sid in by_evidence_id:
                    article_ids.append(by_evidence_id[sid])
        if not article_ids:
            # An item with no traceable evidence is not admissible.
            logger.info("Dropping item without valid source_ids: %s", title[:60])
            continue

        confidence = str(raw.get("confidence") or "medium").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"

        results.append(
            CandidateIntelligence(
                tag=str(raw.get("tag") or "情报").strip()[:32],
                title=title,
                fact_summary=fact,
                assessment=str(raw.get("assessment") or "").strip(),
                importance=max(1, min(_coerce_int(raw.get("importance"), 3), 5)),
                confidence=confidence,
                article_ids=article_ids,
                structured_data=_clean_structured(raw.get("structured_data"), evidence),
                is_correction=bool(raw.get("is_correction")),
            )
        )
    return results


class IntelligenceAnalyzer:
    """Runs the topic / module / synthesis analysis passes."""

    def __init__(self, client: LLMService) -> None:
        #: Any object exposing ``complete_json`` - the vendor is resolved by the
        #: router, so this class never learns which model answered.
        self.client = client

    def analyze_topic(
        self,
        topic_name: str,
        module_name: str,
        report_date: str,
        evidence: list[EvidenceItem],
        max_items: int = 2,
        extra_instructions: str = "",
    ) -> tuple[list[CandidateIntelligence], dict]:
        """Extract at most ``max_items`` candidate intelligence items for a topic."""
        if not evidence:
            return [], {"status": "watch", "items": [], "metrics": []}

        instructions = extra_instructions.strip()
        instruction_block = f"\n本主题额外分析要求：\n{instructions}\n" if instructions else ""

        user = f"""报告日期：{report_date}
监测领域：{module_name}
监测主题：{topic_name}
{instruction_block}
请从以下证据中提炼最多 {max_items} 条最值得进入日报的动态，并抽取证据中明确出现的指标。
证据不足时 items 返回空数组。

输出结构必须类似：
{json.dumps(TOPIC_SCHEMA, ensure_ascii=False)}

证据：
{json.dumps([e.as_prompt_dict() for e in evidence], ensure_ascii=False)}
"""
        result = self.client.complete_json(
            [
                {"role": "system", "content": TOPIC_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            purpose="topic_analysis",
            max_tokens=3200,
        )
        items = parse_topic_items(result.data, evidence, max_items)
        return items, result.data

    def analyze_module(
        self, module_name: str, report_date: str, items: list[CandidateIntelligence]
    ) -> dict:
        """Write the short per-module summary shown above its cards."""
        if not items:
            return {
                "status": "watch",
                "summary": "本期未检出足够高置信度的新增动态，延续观察。",
                "metrics": [],
            }

        compact = [
            {
                "topic": item.topic_name,
                "title": item.title,
                "fact_summary": item.fact_summary,
                "assessment": item.assessment,
                "confidence": item.confidence,
                "structured_data": item.structured_data,
            }
            for item in items
        ]
        user = f"""报告日期：{report_date}
监测领域：{module_name}

请基于以下本领域当期结果，写一段小结，并列出最多 3 个已经出现在结果中的指标。
不得引入新的事实或数字。

输出结构：
{json.dumps(MODULE_SCHEMA, ensure_ascii=False)}

本领域结果：
{json.dumps(compact, ensure_ascii=False)}
"""
        try:
            result = self.client.complete_json(
                [
                    {"role": "system", "content": MODULE_SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                purpose="module_analysis",
                max_tokens=1200,
            )
        except LLMError as exc:
            # A missing summary must not cost us the module's items.
            logger.warning("Module summary failed for %s: %s", module_name, exc)
            return {"status": "new", "summary": "", "metrics": []}

        data = result.data
        return {
            "status": str(data.get("status") or "new"),
            "summary": str(data.get("summary") or "").strip(),
            "metrics": data.get("metrics") if isinstance(data.get("metrics"), list) else [],
        }

    def synthesize_report(self, sections: list[dict], report_date: str) -> dict:
        """Produce headline, trends and the data overview across all modules."""
        if not any(section.get("items") for section in sections):
            return {"headline": {}, "trends": [], "metrics": []}

        user = f"""报告日期：{report_date}
请生成总览。最多 5 条趋势、最多 6 个数据速览。
如果没有足够事实，不要凑数。

输出结构：
{json.dumps(SYNTHESIS_SCHEMA, ensure_ascii=False)}

分领域结果：
{json.dumps(sections, ensure_ascii=False)}
"""
        try:
            result = self.client.complete_json(
                [
                    {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                purpose="synthesis",
                max_tokens=3000,
            )
        except LLMError as exc:
            logger.warning("Report synthesis failed: %s", exc)
            return {"headline": {}, "trends": [], "metrics": []}

        data = result.data
        headline = data.get("headline") if isinstance(data.get("headline"), dict) else {}
        trends = data.get("trends") if isinstance(data.get("trends"), list) else []
        metrics = data.get("metrics") if isinstance(data.get("metrics"), list) else []
        return {"headline": headline, "trends": trends[:5], "metrics": metrics[:6]}
