#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIOS Daily Intelligence Report
- Public-source collection: GDELT DOC API (no key) with Google News RSS fallback
- Evidence extraction: article HTML -> visible paragraphs
- Analysis: DeepSeek Chat Completions API
- Output: styled HTML daily report

Usage:
  export DEEPSEEK_API_KEY="..."
  python aios_daily.py --date 2026-09-15 --days 3

Demo renderer only:
  python aios_daily.py --demo demo_sources.json --output demo.html
"""

from __future__ import annotations
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

TRACKS = [
    {
        "key": "mobile",
        "name": "移动智能终端侧",
        "queries": [
            'HarmonyOS OR 鸿蒙 OR "Android 17" OR iOS "operating system" AI agent',
            '"mobile operating system" AI agent smartphone'
        ],
    },
    {
        "key": "pc",
        "name": "PC 侧",
        "queries": [
            'Windows agentic OS OR "agent-ready" PC',
            'HarmonyOS PC OR 鸿蒙电脑 OR "AI PC" operating system'
        ],
    },
    {
        "key": "server",
        "name": "服务器侧",
        "queries": [
            'openEuler OR Anolis OS OR 龙蜥 operating system server',
            '"server operating system" AI Linux China'
        ],
    },
    {
        "key": "supernode",
        "name": "智算超节点侧",
        "queries": [
            'SuperPoD OR 超节点 AI cluster interconnect',
            'Atlas 950 SuperPoD OR AI supernode'
        ],
    },
    {
        "key": "iot",
        "name": "物联网侧",
        "queries": [
            'OpenHarmony IoT OR 开源鸿蒙 物联网',
            'RISC-V OpenHarmony industrial IoT'
        ],
    },
    {
        "key": "uav",
        "name": "无人飞行器侧",
        "queries": [
            'drone operating system AI flight controller RT-Thread',
            '无人机 操作系统 AI 飞控'
        ],
    },
    {
        "key": "robotics",
        "name": "具身智能侧",
        "queries": [
            'robot operating system embodied AI ROS 2',
            'M-Robots OS OpenHarmony robot'
        ],
    },
    {
        "key": "space",
        "name": "太空智算侧",
        "queries": [
            'space computing operating system satellite AI',
            '太空算力 太空操作系统 卫星'
        ],
    },
]

TRUST_HINTS = (
    ".gov", ".edu", "huawei.com", "microsoft.com", "google.com", "android.com",
    "openeuler.org", "openatom.org", "openharmony", "ros.org", "rt-thread.org",
    "nvidia.com", "alibabacloud.com", "cas.cn", "stdaily.com", "people.com.cn"
)

UA = "Mozilla/5.0 (compatible; AIOS-Daily/1.0; +internal-research)"


def strip_html(s: str) -> str:
    if not s:
        return ""
    if BeautifulSoup:
        return " ".join(BeautifulSoup(s, "html.parser").stripped_strings)
    return re.sub(r"<[^>]+>", " ", s)


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def dedupe(items: list[dict], threshold: float = 0.86) -> list[dict]:
    out = []
    for x in items:
        title = normalize_space(x.get("title", ""))
        if not title:
            continue
        if any(title_similarity(title, y.get("title", "")) >= threshold for y in out):
            continue
        out.append(x)
    return out


def source_score(item: dict) -> int:
    domain = (item.get("domain") or "").lower()
    score = 0
    if any(h in domain for h in TRUST_HINTS):
        score += 4
    if item.get("body"):
        score += 2
    if item.get("published_at"):
        score += 1
    return score


def collect_gdelt(query: str, start: dt.datetime, end: dt.datetime, limit: int = 20) -> list[dict]:
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": min(limit, 250),
        "format": "json",
        "sort": "DateDesc",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    out = []
    for a in data.get("articles", []):
        out.append({
            "title": normalize_space(a.get("title", "")),
            "url": a.get("url", ""),
            "domain": a.get("domain", ""),
            "published_at": a.get("seendate", ""),
            "source": a.get("domain", ""),
            "snippet": "",
            "body": "",
        })
    return out


def collect_google_news(query: str, limit: int = 20) -> list[dict]:
    rss = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    )
    try:
        r = requests.get(rss, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception:
        return []

    out = []
    for item in root.findall(".//item")[:limit]:
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub = item.findtext("pubDate") or ""
        desc = strip_html(item.findtext("description") or "")
        source_node = item.find("source")
        source = source_node.text if source_node is not None else "Google News"
        out.append({
            "title": normalize_space(title),
            "url": link,
            "domain": "",
            "published_at": pub,
            "source": source or "Google News",
            "snippet": normalize_space(desc),
            "body": "",
        })
    return out


def fetch_article_text(url: str, max_chars: int = 7000) -> str:
    if not url or "news.google.com" in url:
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=18, allow_redirects=True)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype:
            return ""
        if BeautifulSoup:
            soup = BeautifulSoup(r.text, "html.parser")
            for bad in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
                bad.decompose()
            paras = [normalize_space(p.get_text(" ", strip=True)) for p in soup.find_all("p")]
            text = "\n".join(p for p in paras if len(p) >= 40)
        else:
            text = normalize_space(strip_html(r.text))
        return text[:max_chars]
    except Exception:
        return ""


def collect_track(track: dict, start: dt.datetime, end: dt.datetime, per_query: int = 18,
                  fetch_top: int = 8) -> list[dict]:
    items = []
    for q in track["queries"]:
        got = collect_gdelt(q, start, end, per_query)
        if not got:
            got = collect_google_news(q, per_query)
        items.extend(got)

    items = dedupe(items)
    # Give official/primary domains a chance to float upward before body fetching.
    items.sort(key=lambda x: source_score(x), reverse=True)
    for x in items[:fetch_top]:
        x["body"] = fetch_article_text(x.get("url", ""))
    items.sort(key=lambda x: source_score(x), reverse=True)
    return items[:12]


def call_deepseek(messages: list[dict], max_tokens: int = 4500) -> dict:
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    last_err = None
    for i in range(3):
        try:
            r = requests.post(
                DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions",
                headers=headers,
                json=payload,
                timeout=180,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception as e:
            last_err = e
            time.sleep(2 ** i)
    raise RuntimeError(f"DeepSeek request failed: {last_err}")


def analyze_track(track: dict, items: list[dict], report_date: str) -> dict:
    evidence = []
    for i, x in enumerate(items):
        evidence.append({
            "id": i,
            "title": x.get("title"),
            "source": x.get("source") or x.get("domain"),
            "published_at": x.get("published_at"),
            "url": x.get("url"),
            "text": (x.get("body") or x.get("snippet") or "")[:7000],
        })

    schema = {
        "section": track["name"],
        "status": "new|watch",
        "items": [{
            "tag": "短标签",
            "title": "不夸张的标题",
            "summary": "120-260字事实摘要",
            "assessment": "40-100字情报判断，明确这是判断而非事实",
            "importance": 1,
            "confidence": "high|medium|low",
            "source_ids": [0]
        }],
        "metrics": [{
            "value": "可直接展示的数字",
            "label": "数字含义",
            "source_ids": [0]
        }]
    }

    system = """你是操作系统、AI基础设施与智能终端领域的情报分析员。
规则：
1. 只能使用用户给出的证据；证据没有写的数字、日期、因果关系不得补全。
2. 优先一手/官方来源，其次权威媒体；营销软文和转载降低置信度。
3. “事实摘要”和“情报判断”必须分开。判断必须使用“判断/预计/值得关注”等措辞。
4. 如果本期证据不足以证明有新增，status=watch，items可以为空；绝不为了填栏目而编造。
5. source_ids 必须引用证据中的 id。
6. 只输出合法 JSON，不要 Markdown。"""

    user = f"""报告日期：{report_date}
赛道：{track['name']}
请从以下证据中提炼最多 2 条最值得进入日报的动态，并抽取最多 2 个可核验指标。
输出结构必须类似：
{json.dumps(schema, ensure_ascii=False)}

证据：
{json.dumps(evidence, ensure_ascii=False)}
"""
    out = call_deepseek(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=3200
    )
    out["_evidence"] = evidence
    return out


def synthesize(sections: list[dict], report_date: str) -> dict:
    compact = []
    for s in sections:
        compact.append({
            "section": s.get("section"),
            "status": s.get("status"),
            "items": [
                {
                    "title": x.get("title"),
                    "summary": x.get("summary"),
                    "assessment": x.get("assessment"),
                    "importance": x.get("importance"),
                    "confidence": x.get("confidence"),
                }
                for x in s.get("items", [])
            ],
            "metrics": s.get("metrics", []),
        })

    system = """你是资深科技产业情报编辑。只基于给定的分赛道分析做二次综合。
不要引入新事实或新数字。趋势研判必须明确是研判，而不是把推断写成事实。
只输出合法 JSON。"""

    schema = {
        "headline": {
            "title": "今日头条",
            "body": "100-180字",
            "section": "来源赛道名"
        },
        "trends": [
            {"title": "趋势一：...", "body": "100-180字", "confidence": "high|medium"}
        ],
        "metrics": [
            {"value": "数字", "label": "指标", "section": "赛道名"}
        ]
    }

    user = f"""报告日期：{report_date}
请生成总览。最多 5 条趋势、最多 6 个数据速览。
如果没有足够事实，不要凑数。
输出结构：
{json.dumps(schema, ensure_ascii=False)}

分赛道结果：
{json.dumps(compact, ensure_ascii=False)}
"""
    return call_deepseek(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=3000
    )


def esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


def source_links(section: dict, ids: list[int]) -> str:
    ev = section.get("_evidence", [])
    chunks = []
    seen = set()
    for i in ids or []:
        if not isinstance(i, int) or i < 0 or i >= len(ev):
            continue
        x = ev[i]
        url = x.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        label = x.get("source") or x.get("title") or "来源"
        date = x.get("published_at") or ""
        chunks.append(
            f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(label)}</a>'
            + (f' <span>{esc(date)}</span>' if date else "")
        )
    return " · ".join(chunks)


CSS = """
:root{--ink:#1a2233;--sub:#5a6478;--line:#e6e9f0;--bg:#f6f7fa;--card:#fff;
--accent:#2456d6;--accent2:#c23a3a;--tag-bg:#eef2fd;--tag-ink:#2456d6}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif;background:var(--bg);
color:var(--ink);line-height:1.75;font-size:15px}
.wrap{max-width:960px;margin:0 auto;padding:32px 24px 64px}
header{background:linear-gradient(135deg,#1c2f6e,#2456d6);color:#fff;border-radius:14px;padding:28px 32px;margin-bottom:24px}
header h1{font-size:24px;letter-spacing:1px;margin-bottom:6px}.meta{opacity:.85;font-size:13px}
h2{font-size:19px;margin:34px 0 14px;padding-left:12px;border-left:4px solid var(--accent)}
h3{font-size:15.5px;margin:8px 0 6px;color:#16307e}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 24px;margin-bottom:14px}
.card p{color:var(--sub);margin-top:6px}.tag{display:inline-block;background:var(--tag-bg);color:var(--tag-ink);
font-size:12px;border-radius:6px;padding:2px 10px;margin-right:8px}
.hl{background:#fff8e6;border-left:4px solid #e8a13a;border-radius:8px;padding:14px 18px;margin:14px 0;color:#6b5312}
.assess{margin-top:10px;padding:10px 12px;background:#f7f9fd;border-radius:8px;color:#46506a;font-size:13px}
.sources{margin-top:10px;padding-top:9px;border-top:1px dashed var(--line);font-size:12px;color:#8b93a6}
.sources a{color:#5269a5;text-decoration:none}.sources span{color:#a3a9b6}
.conf{float:right;font-size:11px;color:#8c95aa}
.watch{color:#8b93a6;font-style:italic}
.kv{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}.kv>div{flex:1;min-width:180px;background:#f0f3fa;border-radius:10px;padding:12px 16px}
.kv .n{font-size:22px;font-weight:700;color:var(--accent)}.kv .t{font-size:12px;color:var(--sub)}
ul{padding-left:20px;color:var(--sub)}li{margin:7px 0}
footer{margin-top:40px;font-size:12px;color:#9aa2b5;text-align:center;border-top:1px solid var(--line);padding-top:16px}
@media(max-width:640px){.wrap{padding:16px 12px 40px}header{padding:22px 20px}.card{padding:16px}}
"""


def render_html(report_date: str, sections: list[dict], overview: dict, output: Path) -> None:
    title = f"AIOS 智能终端操作系统监测日报 · {report_date}"
    h = [f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{esc(title)}</title><style>{CSS}</style></head><body><div class="wrap">
<header><h1>全球智能终端操作系统监测日报</h1>
<div class="meta">监测日期：{esc(report_date)} · 自动采集 / DeepSeek 分析 ·
覆盖：移动 / PC / 服务器 / 智算超节点 / 物联网 / 无人飞行器 / 具身智能 / 太空智算</div></header>"""]

    headline = overview.get("headline") or {}
    if headline:
        h.append(
            f'<div class="hl"><b>今日头条：{esc(headline.get("title",""))}</b> '
            f'{esc(headline.get("body",""))}</div>'
        )

    for idx, s in enumerate(sections, 1):
        h.append(f'<h2>{idx}、{esc(s.get("section",""))}</h2>')
        items = s.get("items") or []
        if not items:
            h.append('<div class="card watch">本期未检出足够高置信的新增动态，延续观察。</div>')
            continue
        for item in items:
            conf = item.get("confidence", "")
            h.append('<div class="card">')
            h.append(f'<span class="tag">{esc(item.get("tag","情报"))}</span>'
                     f'<span class="conf">confidence: {esc(conf)}</span>')
            h.append(f'<h3>{esc(item.get("title",""))}</h3>')
            h.append(f'<p>{esc(item.get("summary",""))}</p>')
            if item.get("assessment"):
                h.append(f'<div class="assess"><b>情报判断：</b>{esc(item.get("assessment"))}</div>')
            links = source_links(s, item.get("source_ids", []))
            if links:
                h.append(f'<div class="sources"><b>证据：</b>{links}</div>')
            h.append('</div>')

    trends = overview.get("trends") or []
    if trends:
        h.append('<h2>九、操作系统智能化转型与 AI OS 趋势研判</h2><div class="card"><ul>')
        for t in trends:
            h.append(f'<li><b>{esc(t.get("title",""))}</b> {esc(t.get("body",""))} '
                     f'<span class="conf">[{esc(t.get("confidence",""))}]</span></li>')
        h.append('</ul></div>')

    metrics = overview.get("metrics") or []
    if metrics:
        h.append('<div class="card"><h3>数据速览</h3><div class="kv">')
        for m in metrics[:6]:
            h.append(
                f'<div><div class="n">{esc(m.get("value",""))}</div>'
                f'<div class="t">{esc(m.get("label",""))}</div></div>'
            )
        h.append('</div></div>')

    h.append(
        f'<footer>本报告由 AIOS 自动监测脚本生成 · DeepSeek 模型：{esc(DEEPSEEK_MODEL)} · '
        '公开来源自动采集 · 情报判断不等同于事实 · 建议对关键数字回溯原始来源核验</footer>'
        '</div></body></html>'
    )
    output.write_text("".join(h), encoding="utf-8")


def load_demo(path: Path) -> tuple[list[dict], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["sections"], data["overview"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="report date YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=3, help="lookback window")
    ap.add_argument("--output", default="", help="output html")
    ap.add_argument("--demo", default="", help="render existing demo JSON without API/network")
    args = ap.parse_args()

    out = Path(args.output or f"AIOS监测日报-{args.date}.html")

    if args.demo:
        sections, overview = load_demo(Path(args.demo))
        render_html(args.date, sections, overview, out)
        print(out.resolve())
        return

    report_day = dt.datetime.strptime(args.date, "%Y-%m-%d")
    end = report_day + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    start = end - dt.timedelta(days=max(args.days, 1))

    sections = []
    for track in TRACKS:
        print(f"[collect] {track['name']}", file=sys.stderr)
        items = collect_track(track, start, end)
        print(f"[analyze] {track['name']} ({len(items)} candidates)", file=sys.stderr)
        sections.append(analyze_track(track, items, args.date))

    overview = synthesize(sections, args.date)

    # Save machine-readable evidence + model output for audit.
    audit_path = out.with_suffix(".json")
    audit_path.write_text(
        json.dumps({"date": args.date, "sections": sections, "overview": overview},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    render_html(args.date, sections, overview, out)
    print(out.resolve())
    print(audit_path.resolve())


if __name__ == "__main__":
    main()
