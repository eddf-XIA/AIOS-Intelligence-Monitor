"""本地检索研究: AIOS collects, the configured model analyses.

This is the path most of this product's users will actually take, because the
model they already have configured - DeepSeek, Qwen, a local Ollama - cannot
browse. It is a first-class, explicitly chosen agent, and it must obey the same
rules as a vendor-native one: real fetched documents, honest coverage, and no
claim that sources answered when they did not.

No network is reached: the collectors are stubbed at
:meth:`SourceCollector.fetch`, the one seam every source shares.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aios.schemas.research import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    RESULT_SCHEMA_EXAMPLE,
)
from aios.services.collector import Candidate, CollectorStatus
from aios.services.research import ResearchError, ResearchRequest
from aios.services.research.catalog import LOCAL_AGENT
from aios.services.research.local_agent import LocalCollectionAgent
from aios.timeutil import utcnow
from conftest import stub_collectors


def candidate(title="HarmonyOS 6 正式发布", url="https://developer.huawei.com/news/h6"):
    return Candidate(
        title=title,
        url=url,
        domain="developer.huawei.com",
        source="华为开发者",
        snippet="华为发布 HarmonyOS 6，新增 Agent Framework。",
        published_at=utcnow() - dt.timedelta(hours=6),
        published_raw="2026-09-19",
        collector="rss",
    )


class ScriptedLLM:
    """Answers the query-planning call and the analysis call in order."""

    def __init__(self, queries=None, analysis=None, analysis_error=None):
        self.queries = queries if queries is not None else ["HarmonyOS 6", "鸿蒙 生态"]
        self.analysis = analysis
        self.analysis_error = analysis_error
        self.purposes: list[str] = []

    def readiness_error(self) -> str:
        return ""

    def complete_json(self, messages, purpose="generic", **kwargs):
        from aios.services.llm.base import LLMResponse

        self.purposes.append(purpose)
        if purpose == "research_queries":
            data = {"queries": list(self.queries)}
        else:
            if self.analysis_error is not None:
                raise self.analysis_error
            data = self.analysis if self.analysis is not None else _analysis_payload()
        return LLMResponse(
            data=data, content="", model="stub", provider_id="stub", latency_ms=3
        )

    def describe_routing(self) -> dict:
        return {}


def _analysis_payload(evidence_id=0):
    """What a well-behaved analysis pass returns over fixed evidence."""
    return {
        "coverage": {"status": "complete", "sources_examined": 1},
        "report": {
            "title": "智能终端操作系统监测日报",
            "focus_title": "HarmonyOS 6 正式发布",
            "focus_summary": "华为发布 HarmonyOS 6。",
            "sections": [
                {
                    "name": "移动智能终端侧",
                    "summary": "本期以鸿蒙为主。",
                    "summary_items": [
                        {
                            "headline": "HarmonyOS 6 正式发布",
                            "body": "华为发布 HarmonyOS 6。",
                            "significance": "判断：生态节奏加快。",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                }
            ],
            "market_snapshot": [],
            "trend_analysis": [],
        },
        "events": [
            {
                "title": "HarmonyOS 6 正式发布",
                "summary": "华为发布 HarmonyOS 6。",
                "organization": "华为",
                "product_or_project": "HarmonyOS 6",
                "event_type": "product_launch",
                "event_date": "2026-09-19",
                "section": "移动智能终端侧",
                "source_ids": [evidence_id],
            }
        ],
        # Deliberately paraphrased: AIOS must replace this with the document
        # it really fetched rather than trusting the model's echo.
        "sources": [
            {
                "source_id": evidence_id,
                "title": "模型改写过的标题",
                "publisher": "模型改写过的来源",
                "url": "https://hallucinated.example.com/not-what-we-fetched",
                "published_at": "2026-09-19",
            }
        ],
        "watch_next": [],
    }


@pytest.fixture
def request_obj():
    return ResearchRequest(
        topic_name="全球智能终端",
        brief="跟踪智能终端操作系统的重要进展。",
        focus_areas=["技术进展"],
        keywords=["HarmonyOS"],
        exclusions=["招聘"],
        regions="中国 + 全球",
        window_hours=72,
        report_date=dt.date(2026, 9, 20),
    )


def make_agent(llm=None, log=None):
    return LocalCollectionAgent(
        LOCAL_AGENT, client=llm or ScriptedLLM(), log=log or (lambda m, level="info": None)
    )


class TestTheAgentIsHonestlyResearchCapable:
    def test_it_is_offered_as_research_capable(self):
        assert LOCAL_AGENT.is_research_capable is True
        assert LOCAL_AGENT.capabilities.supports_web_research is True
        assert LOCAL_AGENT.capabilities.supports_citations is True

    def test_it_does_not_claim_multi_step_research(self):
        """It is one collection pass, not an iterative agent. Say so."""
        assert LOCAL_AGENT.capabilities.supports_multi_step is False
        assert "多轮研究" not in LOCAL_AGENT.capability_labels()

    def test_it_works_with_any_provider(self):
        assert LOCAL_AGENT.provider_id == ""
        assert LOCAL_AGENT.requires_provider is False


class TestSuccessfulResearch:
    def test_it_returns_a_valid_result(self, db, monkeypatch, request_obj):
        stub_collectors(monkeypatch, candidates=[candidate()])
        monkeypatch.setattr(
            "aios.services.article_extractor.ArticleExtractor.fetch",
            lambda self, url: "华为在开发者大会上发布 HarmonyOS 6。",
        )

        result = make_agent().run(request_obj)

        assert result.coverage.status == COVERAGE_COMPLETE
        assert len(result.events) == 1
        assert result.events[0].product_or_project == "HarmonyOS 6"
        assert result.report.focus_title == "HarmonyOS 6 正式发布"

    def test_the_brief_is_turned_into_queries_by_the_model(
        self, db, monkeypatch, request_obj
    ):
        seen: list[str] = []

        def capture(self, query, start, end, limit=20):
            from aios.services.collector import CollectorResult

            seen.append(query)
            return CollectorResult(
                collector=self.name, status=CollectorStatus.OK, candidates=[candidate()]
            )

        from aios.services.collector import SourceCollector

        monkeypatch.setattr(SourceCollector, "fetch", capture)
        monkeypatch.setattr(
            "aios.services.article_extractor.ArticleExtractor.fetch",
            lambda self, url: "正文",
        )

        llm = ScriptedLLM(queries=["HarmonyOS 6 发布", "harmonyos 6 release"])
        make_agent(llm).run(request_obj)

        assert "research_queries" in llm.purposes
        assert "HarmonyOS 6 发布" in seen
        assert "harmonyos 6 release" in seen

    def test_the_evidence_is_the_document_we_fetched_not_the_models_echo(
        self, db, monkeypatch, request_obj
    ):
        """A paraphrased URL must not be able to break the evidence chain."""
        stub_collectors(monkeypatch, candidates=[candidate()])
        monkeypatch.setattr(
            "aios.services.article_extractor.ArticleExtractor.fetch",
            lambda self, url: "正文",
        )

        result = make_agent().run(request_obj)

        assert len(result.sources) == 1
        source = result.sources[0]
        assert source.url == "https://developer.huawei.com/news/h6"
        assert source.publisher == "华为开发者"
        assert "hallucinated" not in source.url

    def test_query_planning_falls_back_without_a_model(
        self, db, monkeypatch, request_obj
    ):
        """Losing the LLM costs query quality, not the ability to research."""
        from aios.services.llm import LLMError

        class NoQueryPlanning(ScriptedLLM):
            def complete_json(self, messages, purpose="generic", **kwargs):
                if purpose == "research_queries":
                    raise LLMError("planning unavailable")
                return super().complete_json(messages, purpose=purpose, **kwargs)

        seen: list[str] = []

        def capture(self, query, start, end, limit=20):
            from aios.services.collector import CollectorResult

            seen.append(query)
            return CollectorResult(
                collector=self.name, status=CollectorStatus.OK, candidates=[candidate()]
            )

        from aios.services.collector import SourceCollector

        monkeypatch.setattr(SourceCollector, "fetch", capture)
        monkeypatch.setattr(
            "aios.services.article_extractor.ArticleExtractor.fetch",
            lambda self, url: "正文",
        )

        result = make_agent(NoQueryPlanning()).run(request_obj)
        assert result.events
        # Derived deterministically from the brief's own named things.
        assert "全球智能终端" in seen
        assert "HarmonyOS" in seen

    def test_progress_is_reported_in_human_stages(self, db, monkeypatch, request_obj):
        stub_collectors(monkeypatch, candidates=[candidate()])
        monkeypatch.setattr(
            "aios.services.article_extractor.ArticleExtractor.fetch",
            lambda self, url: "正文",
        )

        stages: list[str] = []
        agent = LocalCollectionAgent(
            LOCAL_AGENT,
            client=ScriptedLLM(),
            progress=lambda stage, note="": stages.append(stage),
            log=lambda m, level="info": None,
        )
        agent.run(request_obj)

        from aios.services.research import STAGE_ORDER

        assert stages
        assert all(stage in STAGE_ORDER for stage in stages)
        assert stages[0] == "understanding"

    def test_excluded_keywords_are_applied(self, db, monkeypatch, request_obj):
        stub_collectors(
            monkeypatch,
            candidates=[
                candidate(title="华为招聘鸿蒙工程师", url="https://jobs.example.com/1"),
                candidate(),
            ],
        )
        monkeypatch.setattr(
            "aios.services.article_extractor.ArticleExtractor.fetch",
            lambda self, url: "正文",
        )

        captured: dict = {}

        original = LocalCollectionAgent._build_evidence

        def spy(candidates):
            captured["titles"] = [c.title for c in candidates]
            return original(candidates)

        monkeypatch.setattr(LocalCollectionAgent, "_build_evidence", staticmethod(spy))

        make_agent().run(request_obj)
        assert "华为招聘鸿蒙工程师" not in captured["titles"]


class TestCoverageHonesty:
    def test_unreachable_sources_raise_rather_than_report_no_news(
        self, db, monkeypatch, request_obj
    ):
        """The central invariant: we do not know whether there was news."""
        stub_collectors(
            monkeypatch, candidates=[], status=CollectorStatus.NETWORK_ERROR,
            error="connect timeout",
        )

        with pytest.raises(ResearchError) as excinfo:
            make_agent().run(request_obj)
        assert "无法访问" in str(excinfo.value)

    def test_sources_that_answered_with_nothing_is_a_complete_result(
        self, db, monkeypatch, request_obj
    ):
        """We looked, and there was nothing. A real and valid finding."""
        stub_collectors(monkeypatch, candidates=[], status=CollectorStatus.OK)

        result = make_agent().run(request_obj)
        assert result.coverage.status == COVERAGE_COMPLETE
        assert result.coverage.is_failed is False
        assert result.is_empty is True

    def test_a_degraded_collection_downgrades_the_models_coverage_claim(
        self, db, monkeypatch, request_obj
    ):
        """The model cannot know a collector was down. AIOS can, so it corrects it."""
        from aios.services.collection_planner import TopicCollectionStatus

        stub_collectors(monkeypatch, candidates=[candidate()])
        monkeypatch.setattr(
            "aios.services.article_extractor.ArticleExtractor.fetch",
            lambda self, url: "正文",
        )

        agent = make_agent()
        original_collect = agent._collect

        def degraded(request, queries):
            candidates, _ = original_collect(request, queries)
            agent.failed_sources = ["Google News"]
            return candidates, TopicCollectionStatus.PARTIAL

        monkeypatch.setattr(agent, "_collect", degraded)

        result = agent.run(request_obj)
        # The model said "complete"; the truth is "partial".
        assert result.coverage.status == COVERAGE_PARTIAL
        assert any("Google News" in note for note in result.coverage.limitations)

    def test_an_unparseable_analysis_fails_loudly(self, db, monkeypatch, request_obj):
        stub_collectors(monkeypatch, candidates=[candidate()])
        monkeypatch.setattr(
            "aios.services.article_extractor.ArticleExtractor.fetch",
            lambda self, url: "正文",
        )

        llm = ScriptedLLM(analysis={"nonsense": True})
        with pytest.raises(ResearchError):
            make_agent(llm).run(request_obj)

    def test_an_analysis_model_failure_is_surfaced(self, db, monkeypatch, request_obj):
        from aios.services.llm import LLMError

        stub_collectors(monkeypatch, candidates=[candidate()])
        monkeypatch.setattr(
            "aios.services.article_extractor.ArticleExtractor.fetch",
            lambda self, url: "正文",
        )

        llm = ScriptedLLM(analysis_error=LLMError("balance exhausted"))
        with pytest.raises(ResearchError) as excinfo:
            make_agent(llm).run(request_obj)
        assert "分析模型调用失败" in str(excinfo.value)

    def test_an_empty_brief_cannot_produce_queries(self, db, monkeypatch):
        from aios.services.llm import LLMError

        class NoPlanning(ScriptedLLM):
            def complete_json(self, messages, purpose="generic", **kwargs):
                if purpose == "research_queries":
                    raise LLMError("unavailable")
                return super().complete_json(messages, purpose=purpose, **kwargs)

        empty = ResearchRequest(topic_name="", brief="")
        with pytest.raises(ResearchError) as excinfo:
            make_agent(NoPlanning()).run(empty)
        assert "检索方向" in str(excinfo.value)


class TestPromptDiscipline:
    def test_the_analysis_prompt_forbids_further_browsing(self):
        from aios.services.research.prompts import EVIDENCE_SYSTEM_PROMPT

        assert "不需要" in EVIDENCE_SYSTEM_PROMPT
        assert "只能使用给出的证据" in EVIDENCE_SYSTEM_PROMPT

    def test_the_shared_prompt_separates_fact_from_judgement(self):
        from aios.services.research.prompts import SYSTEM_PROMPT

        assert "事实与判断必须分开" in SYSTEM_PROMPT

    def test_the_shared_prompt_forbids_conflating_empty_with_failed(self):
        from aios.services.research.prompts import SYSTEM_PROMPT

        assert "完全不同的两件事" in SYSTEM_PROMPT or "两件完全不同的事" in SYSTEM_PROMPT

    def test_the_output_contract_documents_the_three_parts(self):
        assert set(RESULT_SCHEMA_EXAMPLE) >= {
            "coverage", "report", "events", "sources"
        }
