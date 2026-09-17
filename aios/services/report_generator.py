"""Report persistence, HTML export and JSON audit snapshot.

The HTML keeps the visual identity of the original report - blue gradient
header, white cards, tag chips, headline box, trend list, data overview - and
adds what the new data model makes possible: per-item confidence, clickable
evidence, and NEW/UPDATED event state.

The files under ``data/reports`` are export artifacts. The database rows written
by :func:`persist_report` are the source of truth.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..config import get_paths
from ..models import Report, ReportItem, ReportSection
from ..timeutil import fmt_local, utcnow

logger = logging.getLogger(__name__)


def esc(value: Any) -> str:
    """HTML-escape anything, including None."""
    return html.escape(str(value if value is not None else ""), quote=True)


@dataclass
class SectionPayload:
    """One module's finished contribution to a report."""

    module_id: Optional[int]
    module_key: str
    module_name: str
    sort_order: int
    status: str = "watch"
    #: How collection went for this module, from
    #: :class:`aios.services.collection_planner.TopicCollectionStatus`. Decides
    #: whether an empty section may claim there was no news.
    coverage_state: str = ""
    summary: str = ""
    metrics: list = field(default_factory=list)
    #: dicts with keys: tag, title, fact_summary, assessment, importance,
    #: confidence, event_id, observation_id, event_state, sources
    items: list[dict] = field(default_factory=list)


def persist_report(
    session: Session,
    report_date: dt.date,
    run_id: Optional[int],
    sections: list[SectionPayload],
    overview: dict,
    model: str,
    title: str = "",
    coverage: Optional[dict] = None,
) -> Report:
    """Write the report and its sections/items into the database."""
    report = Report(
        report_date=report_date,
        run_id=run_id,
        title=title or f"全球智能终端操作系统监测日报 · {report_date.isoformat()}",
        headline_json=overview.get("headline") or {},
        trends_json=overview.get("trends") or [],
        metrics_json=overview.get("metrics") or [],
        coverage_json=coverage or None,
        model=model,
        created_at=utcnow(),
    )
    session.add(report)
    session.flush()

    for payload in sections:
        section = ReportSection(
            report_id=report.id,
            module_id=payload.module_id,
            module_key=payload.module_key,
            module_name=payload.module_name,
            sort_order=payload.sort_order,
            status=payload.status,
            coverage_state=payload.coverage_state or "",
            summary=payload.summary,
            metrics_json=payload.metrics or [],
        )
        session.add(section)
        session.flush()

        for order, item in enumerate(payload.items):
            session.add(
                ReportItem(
                    report_section_id=section.id,
                    event_id=item.get("event_id"),
                    observation_id=item.get("observation_id"),
                    tag=item.get("tag", "")[:64],
                    title=item.get("title", ""),
                    fact_summary=item.get("fact_summary", ""),
                    assessment=item.get("assessment", ""),
                    importance=int(item.get("importance", 3) or 3),
                    confidence=item.get("confidence", "medium"),
                    event_state=item.get("event_state", ""),
                    sort_order=order,
                )
            )
    session.flush()
    return report


# --- HTML export ------------------------------------------------------------

CSS = """
:root{
--bg:#f6f6f4;--surface:#fff;--surface-muted:#f1f2f3;
--text:#17181a;--text-secondary:#6d7075;--text-muted:#969a9f;
--border:#e6e7e8;--accent:#5b62bf;--accent-soft:#eef0fa;
--success-bg:#edf7ef;--success-text:#337346;
--warning-bg:#fff5e5;--warning-text:#8a6322;
--info-bg:#eef1fa;--info-text:#53638f;
--radius:14px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Inter","Segoe UI",-apple-system,BlinkMacSystemFont,system-ui,
"Microsoft YaHei","PingFang SC","Noto Sans SC",sans-serif;
background:var(--bg);color:var(--text);line-height:1.75;font-size:15px;
-webkit-font-smoothing:antialiased}
.wrap{max-width:820px;margin:0 auto;padding:48px 28px 72px}
header{border-bottom:1px solid var(--border);padding-bottom:24px;margin-bottom:28px}
header .eyebrow{font-size:13px;color:var(--accent);font-weight:600;letter-spacing:.02em}
header h1{font-size:30px;font-weight:600;letter-spacing:-.02em;margin:6px 0 10px}
.meta{color:var(--text-secondary);font-size:13px;line-height:1.7}
h2{font-size:20px;font-weight:600;letter-spacing:-.01em;margin:40px 0 8px}
.sec-summary{color:var(--text-secondary);font-size:13.5px;margin-bottom:14px}
.hl{background:var(--warning-bg);border-radius:var(--radius);padding:16px 20px;
margin-bottom:28px;color:var(--warning-text);font-size:14.5px}
.coverage{background:var(--info-bg);border-radius:var(--radius);padding:14px 20px;
margin-bottom:22px;color:var(--info-text);font-size:13.5px;line-height:1.7}
.coverage b{display:block;margin-bottom:4px;font-weight:600}
.coverage ul{margin:6px 0 0 18px}
.coverage li{margin:2px 0}
.hl b{display:block;margin-bottom:5px;font-weight:600}
.item{padding:22px 0;border-top:1px solid var(--border)}
.item:first-of-type{border-top:none}
.item h3{font-size:17px;font-weight:600;letter-spacing:-.01em;margin:8px 0 8px;color:var(--text)}
.item p{color:var(--text-secondary);font-size:14px;line-height:1.8}
.tag{display:inline-block;background:var(--surface-muted);color:var(--text-secondary);
font-size:12px;border-radius:999px;padding:2px 10px;margin-right:6px}
.state{display:inline-block;font-size:11.5px;border-radius:999px;padding:2px 9px;margin-right:6px}
.state-new{background:var(--success-bg);color:var(--success-text)}
.state-updated{background:var(--info-bg);color:var(--info-text)}
.conf{float:right;font-size:12px;color:var(--text-muted)}
.assess{margin-top:12px;padding:12px 14px;background:var(--surface-muted);
border-radius:10px;color:var(--text-secondary);font-size:13.5px;line-height:1.75}
.assess b{color:var(--text);font-weight:600}
.metrics-inline{margin-top:12px;font-size:13px;color:var(--text-secondary)}
.metrics-inline b{color:var(--text);font-weight:600}
.sources{margin-top:12px;font-size:12.5px;color:var(--text-muted)}
.sources a{color:var(--text-secondary);text-decoration:none;
background:var(--surface-muted);border-radius:999px;padding:2px 10px;
display:inline-block;margin:2px 4px 2px 0}
.sources a:hover{background:var(--accent-soft);color:var(--accent)}
.sources span{color:var(--text-muted)}
.watch{color:var(--text-muted);font-style:italic;padding:14px 0}
.card{background:var(--surface);border:1px solid var(--border);
border-radius:var(--radius);padding:18px 22px;margin-bottom:14px}
.card h3{font-size:15px;font-weight:600;margin-bottom:10px}
.kv{display:flex;flex-wrap:wrap;gap:0;border:1px solid var(--border);
border-radius:var(--radius);overflow:hidden;background:var(--surface);margin-top:12px}
.kv>div{flex:1;min-width:150px;padding:16px 18px;border-left:1px solid var(--border)}
.kv>div:first-child{border-left:none}
.kv .n{font-size:24px;font-weight:600;letter-spacing:-.02em;color:var(--text)}
.kv .t{font-size:12.5px;color:var(--text-secondary);margin-top:2px}
ul{padding-left:20px;color:var(--text-secondary)}li{margin:10px 0;line-height:1.75}
li b{color:var(--text);font-weight:600}
footer{margin-top:48px;padding-top:20px;border-top:1px solid var(--border);
font-size:12px;color:var(--text-muted);line-height:1.8}
@media(max-width:640px){.wrap{padding:28px 16px 48px}header h1{font-size:24px}
.conf{float:none;display:block;margin-top:4px}.kv>div{border-left:none;border-top:1px solid var(--border)}
.kv>div:first-child{border-top:none}}
@media print{body{background:#fff}.wrap{max-width:none;padding:0}
.item,.card{break-inside:avoid;page-break-inside:avoid}
a{text-decoration:none;color:var(--text-secondary)}}
"""

STATE_LABELS = {"new": "NEW 新事件", "updated": "UPDATED 持续追踪"}


def _source_links(item: ReportItem) -> str:
    """Render the evidence chain for one card as clickable links."""
    observation = item.observation
    if observation is None:
        return ""
    chunks: list[str] = []
    seen: set[str] = set()
    for link in observation.sources:
        article = link.article
        if article is None or not article.url or article.url in seen:
            continue
        seen.add(article.url)
        label = article.source or article.domain or "来源"
        date = article.published_raw or (
            article.published_at.strftime("%Y-%m-%d") if article.published_at else ""
        )
        chunk = f'<a href="{esc(article.url)}" target="_blank" rel="noopener">{esc(label)}</a>'
        if date:
            chunk += f' <span>{esc(date)}</span>'
        chunks.append(chunk)
    return " · ".join(chunks)


def _metrics_inline(item: ReportItem) -> str:
    """Show the structured metrics attached to this observation, if any."""
    observation = item.observation
    if observation is None or not observation.metrics:
        return ""
    parts = []
    for key, entry in observation.metrics.items():
        if not isinstance(entry, dict):
            continue
        display = entry.get("display") or entry.get("value")
        if display in (None, ""):
            continue
        unit = entry.get("unit") or ""
        parts.append(f"<b>{esc(key)}</b>: {esc(display)}{(' ' + esc(unit)) if unit else ''}")
    if not parts:
        return ""
    return '<div class="metrics-inline">指标：' + " · ".join(parts) + "</div>"


#: Section-level wording for an empty module. The distinction between these
#: two sentences is the whole point of the collection-status machinery: "we
#: looked and found nothing" and "we could not look" must never read the same.
EMPTY_SECTION_OK = "本期未发现满足入报标准的新增动态。"
EMPTY_SECTION_FAILED = "本期数据源访问异常，无法确认是否存在新增动态。"
EMPTY_SECTION_PARTIAL = "本期部分数据源访问异常，本领域结果可能不完整。"

DEGRADED_BANNER = "本期部分数据源访问异常，结果可能不完整。"


def empty_section_text(coverage_state: str) -> str:
    """What an empty section is allowed to say, given how collection went."""
    if coverage_state == "collection_failed":
        return EMPTY_SECTION_FAILED
    if coverage_state == "partial_collection":
        return EMPTY_SECTION_PARTIAL
    return EMPTY_SECTION_OK


def coverage_notice(report: Report) -> Optional[dict]:
    """Banner content for a degraded report, or None when coverage was fine.

    Absent coverage data means the report predates source tracking, not that
    coverage was complete - in that case nothing is claimed either way.
    """
    coverage = report.coverage_json or {}
    if not isinstance(coverage, dict) or not coverage.get("degraded"):
        return None
    notes = [str(n) for n in (coverage.get("notes") or []) if n][:6]
    return {"headline": DEGRADED_BANNER, "notes": notes}


def render_html(report: Report) -> str:
    """Build the full standalone HTML document for a stored report."""
    date_text = report.report_date.isoformat()
    title = report.title or f"全球智能终端操作系统监测日报 · {date_text}"
    coverage = " / ".join(s.module_name for s in report.sections) or "全部监测领域"

    out: list[str] = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">',
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body><div class=\"wrap\">",
        f'<header><div class="eyebrow">{esc(date_text)}</div>',
        "<h1>全球智能终端操作系统监测日报</h1>",
        f'<div class="meta">自动采集 · AI 分析 · 覆盖 {esc(coverage)}</div>',
        "</header>",
    ]

    notice = coverage_notice(report)
    if notice:
        lines = "".join(f"<li>{esc(note)}</li>" for note in notice["notes"])
        out.append(
            '<div class="coverage"><b>数据源提示</b>'
            f'{esc(notice["headline"])}'
            + (f"<ul>{lines}</ul>" if lines else "")
            + "</div>"
        )

    headline = report.headline_json or {}
    if headline.get("title") or headline.get("body"):
        out.append(
            f'<div class="hl"><b>今日头条：{esc(headline.get("title", ""))}</b> '
            f'{esc(headline.get("body", ""))}</div>'
        )

    for index, section in enumerate(report.sections, 1):
        out.append(f'<h2>{index}、{esc(section.module_name)}</h2>')
        if section.summary:
            out.append(f'<div class="sec-summary">{esc(section.summary)}</div>')

        if not section.items:
            out.append(
                f'<div class="watch">{esc(empty_section_text(section.coverage_state))}</div>'
            )
            continue

        for item in section.items:
            out.append('<article class="item">')
            state = STATE_LABELS.get(item.event_state, "")
            state_html = (
                f'<span class="state state-{esc(item.event_state)}">{esc(state)}</span>'
                if state
                else ""
            )
            out.append(
                f'<span class="tag">{esc(item.tag or "情报")}</span>{state_html}'
                f'<span class="conf">confidence: {esc(item.confidence)}</span>'
            )
            out.append(f'<h3>{esc(item.title)}</h3>')
            out.append(f'<p>{esc(item.fact_summary)}</p>')
            if item.assessment:
                out.append(f'<div class="assess"><b>情报判断：</b>{esc(item.assessment)}</div>')
            out.append(_metrics_inline(item))
            links = _source_links(item)
            if links:
                out.append(f'<div class="sources">来源 · {links}</div>')
            out.append("</article>")

    trends = report.trends_json or []
    if trends:
        out.append('<h2>趋势研判</h2><div class="card"><ul>')
        for trend in trends:
            if not isinstance(trend, dict):
                continue
            out.append(
                f'<li><b>{esc(trend.get("title", ""))}</b> {esc(trend.get("body", ""))} '
                f'<span class="conf">[{esc(trend.get("confidence", ""))}]</span></li>'
            )
        out.append("</ul></div>")

    metrics = report.metrics_json or []
    if metrics:
        out.append('<h2>数据速览</h2><div class="kv">')
        for metric in metrics[:6]:
            if not isinstance(metric, dict):
                continue
            out.append(
                f'<div><div class="n">{esc(metric.get("value", ""))}</div>'
                f'<div class="t">{esc(metric.get("label", ""))}</div></div>'
            )
        out.append("</div>")

    out.append(
        f'<footer>本报告由 AIOS Intelligence Monitor 生成 · 模型：{esc(report.model)} · '
        f'生成时间：{esc(fmt_local(report.created_at))} · '
        "公开来源自动采集 · 情报判断不等同于事实 · 建议对关键数字回溯原始来源核验</footer>"
        "</div></body></html>"
    )
    return "".join(out)


def build_audit_payload(report: Report) -> dict:
    """Machine-readable snapshot, independent of the current DB schema."""
    modules: list[dict] = []
    events: dict[int, dict] = {}
    observations: list[dict] = []
    items: list[dict] = []
    sources: dict[int, dict] = {}

    for section in report.sections:
        modules.append(
            {
                "module_key": section.module_key,
                "module_name": section.module_name,
                "status": section.status,
                "summary": section.summary,
                "sort_order": section.sort_order,
                "metrics": section.metrics_json or [],
            }
        )
        for item in section.items:
            entry = {
                "module_key": section.module_key,
                "event_id": item.event_id,
                "observation_id": item.observation_id,
                "tag": item.tag,
                "title": item.title,
                "fact_summary": item.fact_summary,
                "assessment": item.assessment,
                "importance": item.importance,
                "confidence": item.confidence,
                "event_state": item.event_state,
                "source_ids": [],
            }
            event = item.event
            if event is not None and event.id not in events:
                events[event.id] = {
                    "id": event.id,
                    "event_key": event.event_key,
                    "title": event.title,
                    "status": event.status,
                    "summary": event.summary,
                    "first_seen_date": event.first_seen_date.isoformat()
                    if event.first_seen_date
                    else None,
                    "last_seen_date": event.last_seen_date.isoformat()
                    if event.last_seen_date
                    else None,
                    "observation_count": event.observation_count,
                }

            observation = item.observation
            if observation is not None:
                article_ids = []
                for link in observation.sources:
                    article = link.article
                    if article is None:
                        continue
                    article_ids.append(article.id)
                    if article.id not in sources:
                        sources[article.id] = {
                            "id": article.id,
                            "title": article.title,
                            "url": article.url,
                            "source": article.source,
                            "domain": article.domain,
                            "published_at": article.published_raw
                            or (article.published_at.isoformat() if article.published_at else ""),
                            "collected_at": article.collected_at.isoformat()
                            if article.collected_at
                            else "",
                            "trust_score": article.trust_score,
                        }
                entry["source_ids"] = article_ids
                observations.append(
                    {
                        "id": observation.id,
                        "event_id": observation.event_id,
                        "observation_date": observation.observation_date.isoformat(),
                        "title": observation.title,
                        "fact_summary": observation.fact_summary,
                        "assessment": observation.assessment,
                        "importance": observation.importance,
                        "confidence": observation.confidence,
                        "is_correction": observation.is_correction,
                        "structured_data": observation.structured_data_json or {},
                        "source_ids": article_ids,
                    }
                )
            items.append(entry)

    return {
        "schema_version": 2,
        "report_date": report.report_date.isoformat(),
        "run_id": report.run_id,
        "report_id": report.id,
        "title": report.title,
        "model": report.model,
        "generated_at": (report.created_at or utcnow()).isoformat(),
        "modules": modules,
        "events": list(events.values()),
        "observations": observations,
        "report_items": items,
        "sources": list(sources.values()),
        "headline": report.headline_json or {},
        "trends": report.trends_json or [],
        "metrics": report.metrics_json or [],
    }


def output_dir(session: Optional[Session] = None) -> Path:
    """Where exports land: the configured directory, else ``data/reports``."""
    if session is not None:
        from .settings_service import get_str

        configured = get_str(session, "report_output_dir", "").strip()
        if configured:
            path = Path(configured).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return path
    reports_dir = get_paths().reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir


def export_report(session: Session, report: Report) -> tuple[Path, Path]:
    """Write ``YYYY-MM-DD.html`` and ``YYYY-MM-DD.json`` and record the paths."""
    directory = output_dir(session)
    stem = report.report_date.isoformat()
    html_path = directory / f"{stem}.html"
    json_path = directory / f"{stem}.json"

    html_path.write_text(render_html(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(build_audit_payload(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report.html_path = str(html_path)
    report.json_path = str(json_path)
    session.flush()
    logger.info("Exported report %s -> %s", stem, html_path)
    return html_path, json_path
