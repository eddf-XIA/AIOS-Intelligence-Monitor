"""The research instructions handed to every agent.

One prompt, shared by every backend, so the *quality bar* does not depend on
which vendor the user configured. The invariants are the ones the Classic
analyser already enforced and which this product is built on:

* only what the retrieved sources actually say may be stated as fact,
* fact and judgement are separate fields and never merged,
* an empty result is a valid result - padding the brief is forbidden,
* every claim cites source ids that really exist,
* when something could not be checked, say so in ``coverage`` rather than
  quietly reporting less.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from ...schemas.research import RESULT_SCHEMA_EXAMPLE
from .base import DEPTH_DEEP, ResearchRequest

SYSTEM_PROMPT = """你是资深科技产业情报研究员，负责为一份专业情报日报做当期研究。

你的工作方式：
1. 先理解研究目标，再决定检索什么。不要只用用户原话做一次搜索。
2. 必须实际检索并阅读公开信息。禁止凭记忆作答。
3. 同一件事尽量找到两个以上独立来源交叉验证；只有单一来源时降低置信度并说明。
4. 优先一手来源（厂商公告、官方博客、标准组织、监管文件、财报），
   其次权威媒体；营销软文、转载、无署名聚合内容降低权重。

绝对不能违反的规则：

A. 只能写检索到的来源明确陈述的内容。来源没写的数字、日期、因果关系一律不得补全。
   宁可少写，不要推测。
B. 事实与判断必须分开。
   body / summary 只写事实；
   significance / analysis 写判断，并使用"判断/预计/值得关注/可能"这类措辞。
C. 如果本期确实没有达到入报标准的重要动态，events 返回空数组，
   coverage.status 仍然是 complete。这是一个合法且有价值的结果，
   绝对不要为了填满报告而编造或降格收录不重要的事。
D. 如果有来源无法访问、检索受限、时间窗内信息明显不足，
   coverage.status 必须是 partial 或 failed，并在 limitations 里说明。
   "没有发现重要动态"和"没能完成研究"是两件完全不同的事，不得混为一谈。
E. 每个 event 和每个 summary_item 都必须引用 sources 里真实存在的 source_id。
   没有来源支撑的条目不要输出。
F. sources 里的 url 必须是你真实访问或检索到的可用链接。禁止编造 URL。
G. market_snapshot 只在来源中出现明确数字时才填写；没有就返回空数组。
   禁止估算、换算或凑数。

事件识别（很重要）：
events 是给系统做跨天追踪用的，不是给人看的。
每个 event 要尽可能填写 organization（主体机构）、product_or_project（产品/项目名）、
event_type（事件类型）、event_date（事件发生日期）。
同一件事在不同日期可能被不同媒体用不同说法描述，
这些字段是系统判断"这是同一件事的新进展"还是"这是一件新事"的依据，
所以要填写稳定、规范的名称，而不是当天的新闻标题措辞。

输出：只输出一个合法 JSON 对象。不要 Markdown 代码围栏，不要任何解释文字。"""


def _window_line(request: ResearchRequest) -> str:
    def fmt(value: Optional[dt.datetime]) -> str:
        return value.strftime("%Y-%m-%d %H:%M") if value else ""

    start, end = fmt(request.window_from), fmt(request.window_to)
    if start and end:
        return f"{request.window_text}（{start} 至 {end}，本地时间）"
    return request.window_text


def _bullet(label: str, values: list[str]) -> str:
    return f"{label}：{'、'.join(values)}" if values else ""


def build_brief_block(request: ResearchRequest) -> str:
    """The research brief as the agent sees it - prose, not query syntax."""
    lines = [
        f"研究主题：{request.topic_name}",
        f"研究目标：{request.brief or request.topic_name}",
    ]
    if request.scope:
        lines.append(f"研究范围：{request.scope}")
    for line in (
        _bullet("重点关注", request.focus_areas),
        _bullet("重点对象", request.keywords),
        _bullet("明确排除", request.exclusions),
    ):
        if line:
            lines.append(line)
    if request.regions:
        lines.append(f"地域范围：{request.regions}")
    lines.append(f"时间范围：{_window_line(request)}")
    if request.report_date:
        lines.append(f"报告日期：{request.report_date.isoformat()}")
    if request.preferred_sections:
        lines.append(
            "本主题历史报告使用的领域划分（如果本期内容契合，请沿用，"
            "便于跨期对比）：" + "、".join(request.preferred_sections)
        )
    return "\n".join(lines)


def build_user_prompt(request: ResearchRequest) -> str:
    """The full research instruction for a web-capable agent."""
    depth_note = (
        "研究深度：深入。请多轮检索，覆盖主要厂商与主要子领域，"
        "并尽量交叉验证关键数字。"
        if request.depth == DEPTH_DEEP
        else "研究深度：标准。聚焦时间范围内最重要的动态，不必穷尽所有细节。"
    )

    return f"""{build_brief_block(request)}

{depth_note}

请完成这次研究，然后严格按下面的结构输出结果。
字段名不可更改，不可增加字段。数组可以为空数组。

{json.dumps(RESULT_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)}
"""


EVIDENCE_SYSTEM_PROMPT = SYSTEM_PROMPT + """

本次研究的检索与抓取工作已由本机采集系统完成，证据列表在下方给出。
因此：
- 你不需要（也不能）再自行联网，只能使用给出的证据；
- source_id 必须引用下方证据的 id；
- 如果证据明显不足以覆盖研究范围，coverage.status 填 partial，
  并在 limitations 里说明哪部分没有覆盖到。"""


def build_evidence_prompt(request: ResearchRequest, evidence: list[dict]) -> str:
    """The instruction for the local-collection agent.

    The retrieval was done by AIOS's own collectors, so the model's job is
    strictly analysis over a fixed evidence set - the same discipline the
    Classic pipeline applies, expressed in the research result shape.
    """
    return f"""{build_brief_block(request)}

请基于下面这批已采集到的证据完成本期研究。

输出结构（字段名不可更改，不可增加字段）：
{json.dumps(RESULT_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)}

证据（共 {len(evidence)} 条）：
{json.dumps(evidence, ensure_ascii=False)}
"""


#: Appended when an agent's first answer was not usable JSON.
REPAIR_INSTRUCTION = (
    "你上一次的回复不是符合要求的 JSON。请只输出一个合法的 JSON 对象，"
    "字段结构与之前给出的结构完全一致，不要输出 Markdown 代码围栏、"
    "解释或任何额外文字。"
)
