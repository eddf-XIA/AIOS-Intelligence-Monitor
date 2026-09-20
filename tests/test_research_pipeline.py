"""Agent research folded into the intelligence core.

    Research Agent is the eyes. AIOS remains the memory.

These tests drive :class:`~aios.services.research_pipeline.ResearchPipeline`
end to end with a stub agent - no provider, no network, no keyring - and assert
that what comes out is ordinary AIOS intelligence: IntelligenceEvent rows,
EventObservation rows, ObservationSource evidence links, NEW/UPDATED states and
a Report the existing report system renders.

The day-to-day rewording scenario from the specification is exercised directly:
the same real-world story described three different ways must produce one
timeline, not three.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aios.models import (
    RUN_ENGINE_AGENT,
    EventObservation,
    IntelligenceEvent,
    RunStatus,
)
from aios.repositories import events as events_repo
from aios.repositories import reports as reports_repo
from aios.repositories import research_topics as topics_repo
from aios.repositories import runs as runs_repo
from aios.schemas.research import (
    COVERAGE_COMPLETE,
    COVERAGE_FAILED,
    COVERAGE_PARTIAL,
    ResearchResult,
    validate_research_payload,
)
from aios.services.research import ResearchError, ResearchRequest, ResearchService
from aios.services.research.catalog import LOCAL_AGENT
from aios.services.research_pipeline import ResearchPipeline, create_research_run


# --- stubs ------------------------------------------------------------------

class StubAgent(ResearchService):
    """Returns a canned ResearchResult. Nothing here touches a network."""

    def __init__(self, result=None, error=None, progress=None):
        super().__init__(LOCAL_AGENT, progress=progress)
        self.result = result
        self.error = error
        self.requests: list[ResearchRequest] = []

    def readiness_error(self) -> str:
        return ""

    def run(self, request: ResearchRequest) -> ResearchResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class StubLLM:
    """An LLM router that always declines, so matching uses the rules path.

    Deliberate: the matcher's deterministic behaviour is what these tests are
    about, and a stub that answered would be testing the stub.
    """

    def readiness_error(self) -> str:
        return ""

    def complete_json(self, *args, **kwargs):
        from aios.services.llm import LLMError

        raise LLMError("no provider in tests")

    def describe_routing(self) -> dict:
        return {}


@pytest.fixture
def stub_llm(monkeypatch):
    from aios.services import research_pipeline

    monkeypatch.setattr(research_pipeline, "build_llm_service", lambda **kw: StubLLM())
    return StubLLM()


@pytest.fixture
def topic(session):
    topic = topics_repo.create_topic(
        session,
        name="全球智能终端",
        brief="跟踪智能终端操作系统与 AI 基础设施的重要进展。",
        scope="鸿蒙 / Android / AI PC / 具身智能 / 太空智算",
        focus_areas=["技术进展", "产品发布"],
        exclusions=["招聘"],
        keywords=["HarmonyOS", "AI PC"],
        regions="中国 + 全球",
        window_hours=72,
    )
    session.commit()
    return topic


def result_payload(
    title="HarmonyOS 6 正式发布",
    summary="华为发布 HarmonyOS 6，重点是 Agent Framework。",
    organization="华为",
    product="HarmonyOS 6",
    event_type="product_launch",
    event_date="2026-09-19",
    url="https://developer.huawei.com/news/harmonyos6",
    status="complete",
    section="移动智能终端侧",
    extra_events=(),
    extra_sources=(),
):
    events = [
        {
            "title": title,
            "summary": summary,
            "entities": [organization],
            "organization": organization,
            "product_or_project": product,
            "event_type": event_type,
            "event_date": event_date,
            "significance": "判断：值得持续跟踪。",
            "importance": 1,
            "section": section,
            "source_ids": [0],
        }
    ]
    events.extend(extra_events)
    sources = [
        {
            "source_id": 0,
            "title": title,
            "publisher": "华为开发者",
            "url": url,
            "published_at": event_date,
        }
    ]
    sources.extend(extra_sources)
    return {
        "coverage": {"status": status, "sources_examined": len(sources)},
        "report": {
            "title": "智能终端操作系统监测日报",
            "focus_title": title,
            "focus_summary": summary,
            "sections": [
                {
                    "name": section,
                    "summary": "本期以鸿蒙生态为主。",
                    "summary_items": [
                        {
                            "headline": title,
                            "body": summary,
                            "significance": "判断：生态节奏加快。",
                            "evidence_ids": [0],
                        }
                    ],
                }
            ],
            "market_snapshot": [],
            "trend_analysis": [
                {
                    "title": "趋势：端侧 Agent 化",
                    "analysis": "判断：端侧代理框架成为竞争焦点。",
                    "confidence": "medium",
                    "evidence_ids": [0],
                }
            ],
        },
        "events": events,
        "sources": sources,
        "watch_next": ["Agent Framework 的第三方接入进度"],
    }


def run_pipeline_with(session, monkeypatch, topic_id, result=None, error=None):
    """Execute one research run against a stub agent. Returns the run id."""
    from aios.services import research_pipeline

    agent = StubAgent(result=result, error=error)
    monkeypatch.setattr(
        research_pipeline, "build_agent", lambda s, **kw: agent
    )

    run = create_research_run(session, topic_id)
    run_id = run.id
    session.commit()

    status = ResearchPipeline(run_id).execute()
    session.expire_all()
    return run_id, status, agent


# --- the happy path ---------------------------------------------------------

class TestAgentEventsBecomeIntelligence:
    def test_an_agent_event_becomes_an_intelligence_event(
        self, session, monkeypatch, topic, stub_llm
    ):
        result = validate_research_payload(result_payload())
        run_id, status, _ = run_pipeline_with(
            session, monkeypatch, topic.id, result=result
        )

        assert status == RunStatus.COMPLETED
        events = session.query(IntelligenceEvent).all()
        assert len(events) == 1

        event = events[0]
        assert event.title == "HarmonyOS 6 正式发布"
        assert event.research_topic_id == topic.id
        # The durable fingerprint was stored, not just the title.
        assert event.organization == "华为"
        assert event.product_or_project == "HarmonyOS 6"
        assert event.event_type == "product_launch"
        assert event.canonical_urls_json

    def test_an_observation_and_evidence_links_are_written(
        self, session, monkeypatch, topic, stub_llm
    ):
        result = validate_research_payload(result_payload())
        run_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=result)

        observations = session.query(EventObservation).all()
        assert len(observations) == 1

        observation = events_repo.get_observation(session, observations[0].id)
        assert observation.run_id == run_id
        assert observation.event_date == dt.date(2026, 9, 19)
        # The evidence chain is intact: observation -> source -> article -> URL.
        assert len(observation.sources) == 1
        article = observation.sources[0].article
        assert article is not None
        assert article.url == "https://developer.huawei.com/news/harmonyos6"
        assert article.collector == "agent"

    def test_a_report_is_produced_through_the_existing_report_system(
        self, session, monkeypatch, topic, stub_llm
    ):
        result = validate_research_payload(result_payload())
        run_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=result)

        report = reports_repo.get_by_run(session, run_id)
        assert report is not None
        assert report.engine == RUN_ENGINE_AGENT
        assert report.research_topic_id == topic.id
        assert report.coverage_status == COVERAGE_COMPLETE

        assert [s.module_name for s in report.sections] == ["移动智能终端侧"]
        assert report.item_count == 1
        item = report.sections[0].items[0]
        assert item.event_state == "new"
        assert item.event_id is not None
        assert item.observation_id is not None

        # Headline and trends flow into the same columns Classic reports use.
        assert report.headline_json["title"] == "HarmonyOS 6 正式发布"
        assert report.trends_json

    def test_the_run_records_agent_provenance_and_counts(
        self, session, monkeypatch, topic, stub_llm
    ):
        result = validate_research_payload(result_payload())
        run_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=result)

        run = runs_repo.get_run(session, run_id)
        assert run.engine == RUN_ENGINE_AGENT
        assert run.is_agent_run is True
        assert run.research_topic_id == topic.id
        assert run.coverage_status == COVERAGE_COMPLETE
        assert run.total_events == 1
        assert run.total_new_events == 1
        assert run.total_updated_events == 0
        assert run.total_report_items == 1

    def test_the_agent_receives_the_brief_not_a_query(
        self, session, monkeypatch, topic, stub_llm
    ):
        result = validate_research_payload(result_payload())
        _, _, agent = run_pipeline_with(session, monkeypatch, topic.id, result=result)

        request = agent.requests[0]
        assert request.topic_name == "全球智能终端"
        assert "智能终端操作系统" in request.brief
        assert request.focus_areas == ["技术进展", "产品发布"]
        assert request.exclusions == ["招聘"]
        assert request.window_hours == 72
        assert request.window_from is not None and request.window_to is not None

    def test_the_topic_records_that_it_ran(
        self, session, monkeypatch, topic, stub_llm
    ):
        result = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=result)

        reloaded = topics_repo.get_topic(session, topic.id)
        assert reloaded.run_count == 1
        assert reloaded.last_run_at is not None

    def test_an_event_with_no_resolvable_evidence_is_not_published(
        self, session, monkeypatch, topic, stub_llm
    ):
        """Same rule as the Classic analyser: untraceable items are inadmissible."""
        result = validate_research_payload(result_payload())
        # Forge an extra event whose evidence was pruned by validation.
        from aios.schemas.research import ResearchEvent

        forged = ResearchEvent(title="没有来源的事件", source_ids=[])
        result = result.model_copy(update={"events": list(result.events) + [forged]})

        run_pipeline_with(session, monkeypatch, topic.id, result=result)
        titles = [e.title for e in session.query(IntelligenceEvent).all()]
        assert titles == ["HarmonyOS 6 正式发布"]


# --- NEW / UPDATED across days ---------------------------------------------

class TestHistoricalTracking:
    def test_first_appearance_is_new(self, session, monkeypatch, topic, stub_llm):
        result = validate_research_payload(result_payload())
        run_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=result)

        report = reports_repo.get_by_run(session, run_id)
        assert report.sections[0].items[0].event_state == "new"
        assert report.new_count == 1
        assert report.updated_count == 0

    def test_later_evidence_on_the_same_story_is_updated(
        self, session, monkeypatch, topic, stub_llm
    ):
        """Day 2, reworded, from a different outlet - one event, two observations."""
        day1 = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=day1)

        day2 = validate_research_payload(
            result_payload(
                title="华为推出新一代鸿蒙操作系统 HarmonyOS 6",
                summary="HarmonyOS 6 新增 Agent Framework 2.0 的生态信息。",
                url="https://tech.example.com/harmonyos-6-ecosystem",
            )
        )
        run2_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=day2)

        events = session.query(IntelligenceEvent).all()
        assert len(events) == 1, "a reworded description must not start a new timeline"
        assert events[0].observation_count == 2

        report = reports_repo.get_by_run(session, run2_id)
        assert report.sections[0].items[0].event_state == "updated"
        assert report.updated_count == 1

    def test_the_same_event_with_a_different_title_is_matched(
        self, session, monkeypatch, topic, stub_llm
    ):
        """The specification's own scenario, run for real."""
        day1 = validate_research_payload(
            result_payload(
                title="Figure 发布 Helix 2",
                summary="Figure 发布 Helix 2 人形机器人智能系统。",
                organization="Figure AI, Inc.",
                product="Helix 2",
                event_type="product_launch",
                event_date="2026-09-18",
                url="https://figure.ai/news/helix-2",
                section="具身智能侧",
            )
        )
        run_pipeline_with(session, monkeypatch, topic.id, result=day1)

        day2 = validate_research_payload(
            result_payload(
                title="Figure 推出新一代人形机器人智能系统 Helix 2",
                summary="Helix 2 的运动控制能力细节披露。",
                organization="Figure",
                product="Helix 2",
                event_type="发布",
                event_date="2026-09-19",
                url="https://techcrunch.example.com/figure-helix2",
                section="具身智能侧",
            )
        )
        run2_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=day2)

        events = session.query(IntelligenceEvent).all()
        assert len(events) == 1
        assert events[0].observation_count == 2
        report = reports_repo.get_by_run(session, run2_id)
        assert report.sections[0].items[0].event_state == "updated"

    def test_a_different_milestone_for_the_same_product_is_a_new_event(
        self, session, monkeypatch, topic, stub_llm
    ):
        """"发布 Helix 2" and "Helix 2 工厂部署" are two events in one storyline."""
        day1 = validate_research_payload(
            result_payload(
                title="Figure 发布 Helix 2",
                organization="Figure",
                product="Helix 2",
                event_type="product_launch",
                event_date="2026-09-18",
                url="https://figure.ai/news/helix-2",
                section="具身智能侧",
            )
        )
        run_pipeline_with(session, monkeypatch, topic.id, result=day1)

        day3 = validate_research_payload(
            result_payload(
                title="Helix 2 获得新的工厂部署",
                summary="宝马工厂部署 Helix 2。",
                organization="Figure",
                product="Helix 2",
                event_type="deployment",
                event_date="2026-09-20",
                url="https://reuters.example.com/figure-bmw",
                section="具身智能侧",
            )
        )
        run_pipeline_with(session, monkeypatch, topic.id, result=day3)

        events = session.query(IntelligenceEvent).all()
        assert len(events) == 2
        assert {e.event_type for e in events} == {"product_launch", "deployment"}

    def test_a_shared_source_url_merges_even_with_an_unrelated_title(
        self, session, monkeypatch, topic, stub_llm
    ):
        """Citing the same document is the strongest identity signal there is."""
        day1 = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=day1)

        day2 = validate_research_payload(
            result_payload(
                title="完全不同措辞的同一条新闻",
                summary="同一篇公告的另一种描述。",
                organization="",
                product="",
                event_type="",
                # The same URL, with tracking noise and a trailing slash.
                url="https://www.developer.huawei.com/news/harmonyos6/?utm_source=x",
            )
        )
        run_pipeline_with(session, monkeypatch, topic.id, result=day2)

        assert len(session.query(IntelligenceEvent).all()) == 1

    def test_an_unchanged_event_does_not_become_a_duplicate_new(
        self, session, monkeypatch, topic, stub_llm
    ):
        """Running the identical result twice must not double the timeline."""
        result = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=result)
        run2_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=result)

        events = session.query(IntelligenceEvent).all()
        assert len(events) == 1
        report = reports_repo.get_by_run(session, run2_id)
        assert report.sections[0].items[0].event_state == "updated"

    def test_a_genuinely_unrelated_event_stays_separate(
        self, session, monkeypatch, topic, stub_llm
    ):
        day1 = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=day1)

        other = validate_research_payload(
            result_payload(
                title="昇腾 960 NPU 超节点正式披露",
                summary="华为披露昇腾 960 超节点架构。",
                organization="华为",
                product="昇腾 960",
                event_type="product_launch",
                event_date="2026-09-20",
                url="https://tech.example.com/ascend-960",
                section="智算超节点侧",
            )
        )
        run_pipeline_with(session, monkeypatch, topic.id, result=other)

        events = session.query(IntelligenceEvent).all()
        assert len(events) == 2

    def test_observation_source_links_remain_valid(
        self, session, monkeypatch, topic, stub_llm
    ):
        result = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=result)

        for observation in session.query(EventObservation).all():
            loaded = events_repo.get_observation(session, observation.id)
            assert loaded.sources
            for link in loaded.sources:
                assert link.article is not None
                assert link.article.url.startswith("http")

    def test_an_existing_url_is_reused_rather_than_duplicated(
        self, session, monkeypatch, topic, stub_llm
    ):
        from aios.models import RawArticle

        result = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=result)
        run_pipeline_with(session, monkeypatch, topic.id, result=result)

        urls = [a.url for a in session.query(RawArticle).all()]
        assert len(urls) == len(set(urls)) == 1

    def test_the_fingerprint_accumulates_across_runs(
        self, session, monkeypatch, topic, stub_llm
    ):
        """A storyline gets easier to recognise the longer it runs."""
        day1 = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=day1)

        day2 = validate_research_payload(
            result_payload(
                title="华为推出新一代鸿蒙操作系统 HarmonyOS 6",
                url="https://tech.example.com/harmonyos-6-ecosystem",
            )
        )
        run_pipeline_with(session, monkeypatch, topic.id, result=day2)

        event = session.query(IntelligenceEvent).one()
        assert len(event.canonical_urls_json) == 2


class TestCompareStillWorks:
    def test_the_diff_engine_reads_agent_reports(
        self, session, monkeypatch, topic, stub_llm
    ):
        from aios.services import diff_engine

        day1 = validate_research_payload(result_payload())
        run1_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=day1)

        day2 = validate_research_payload(
            result_payload(
                title="华为推出新一代鸿蒙操作系统 HarmonyOS 6",
                summary="新增 Agent Framework 2.0 的生态信息。",
                url="https://tech.example.com/harmonyos-6-ecosystem",
                extra_events=[
                    {
                        "title": "昇腾 960 超节点披露",
                        "summary": "华为披露昇腾 960 超节点。",
                        "organization": "华为",
                        "product_or_project": "昇腾 960",
                        "event_type": "product_launch",
                        "event_date": "2026-09-20",
                        "section": "移动智能终端侧",
                        "source_ids": [1],
                    }
                ],
                extra_sources=[
                    {
                        "source_id": 1,
                        "title": "昇腾 960",
                        "publisher": "科技日报",
                        "url": "https://tech.example.com/ascend-960",
                        "published_at": "2026-09-20",
                    }
                ],
            )
        )
        run2_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=day2)

        report_a = reports_repo.get_by_run(session, run1_id)
        report_b = reports_repo.get_by_run(session, run2_id)
        diff = diff_engine.compare_reports(session, report_a, report_b)

        assert diff.error == ""
        summary = diff.summary
        assert summary[diff_engine.NEW] == 1
        assert summary[diff_engine.UPDATED] >= 1

    def test_the_changes_page_renders_agent_history(
        self, client, session, monkeypatch, topic, stub_llm
    ):
        day1 = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=day1)

        day2 = validate_research_payload(
            result_payload(
                title="华为推出新一代鸿蒙操作系统 HarmonyOS 6",
                summary="新增 Agent Framework 2.0 的生态信息。",
                url="https://tech.example.com/harmonyos-6-ecosystem",
            )
        )
        run2_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=day2)

        report = reports_repo.get_by_run(session, run2_id)
        body = client.get(f"/simple/changes/{report.id}").text

        assert "相比上一期" in body
        assert "更新" in body
        # No raw observations, no change-type enums, no ids.
        for word in ("EventObservation", "DATA_CHANGE", "observation_id", "change_type"):
            assert word not in body

    def test_the_sources_page_shows_publishers_not_json(
        self, client, session, monkeypatch, topic, stub_llm
    ):
        result = validate_research_payload(result_payload())
        run_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=result)
        report = reports_repo.get_by_run(session, run_id)

        body = client.get(f"/simple/sources/{report.id}").text
        assert "华为开发者" in body
        assert "developer.huawei.com/news/harmonyos6" in body
        for word in ('{"events"', "ObservationSource", "source_id"):
            assert word not in body

    def test_the_history_page_is_organised_by_topic(
        self, client, session, monkeypatch, topic, stub_llm
    ):
        result = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=result)

        body = client.get("/simple/history").text
        assert "全球智能终端" in body
        assert "1 项动态" in body


# --- failure and partial coverage ------------------------------------------

class TestFailureExperience:
    def test_failed_research_does_not_become_no_news(
        self, session, monkeypatch, topic, stub_llm
    ):
        """The central coverage invariant, at the pipeline level."""
        run_id, status, _ = run_pipeline_with(
            session, monkeypatch, topic.id, error=ResearchError("研究服务暂时不可用")
        )

        assert status == RunStatus.FAILED
        run = runs_repo.get_run(session, run_id)
        assert run.coverage_status == COVERAGE_FAILED
        assert "研究服务暂时不可用" in run.error_message

        # No report at all - an empty one would read as "nothing happened".
        assert reports_repo.get_by_run(session, run_id) is None
        assert session.query(IntelligenceEvent).count() == 0

    def test_an_agent_reporting_failed_coverage_produces_no_report(
        self, session, monkeypatch, topic, stub_llm
    ):
        from aios.schemas.research import failed_result

        run_id, status, _ = run_pipeline_with(
            session, monkeypatch, topic.id, result=failed_result("检索服务不可用")
        )
        assert status == RunStatus.FAILED
        assert reports_repo.get_by_run(session, run_id) is None

    def test_an_unavailable_agent_fails_cleanly(self, session, monkeypatch, topic, stub_llm):
        from aios.services.research import ResearchAgentUnavailable

        run_id, status, _ = run_pipeline_with(
            session,
            monkeypatch,
            topic.id,
            error=ResearchAgentUnavailable("所选研究 Agent 不具备联网研究能力。"),
        )
        assert status == RunStatus.FAILED
        run = runs_repo.get_run(session, run_id)
        assert "联网研究" in run.error_message

    def test_the_failure_state_is_shown_on_the_home_page(
        self, client, session, monkeypatch, topic, stub_llm
    ):
        run_pipeline_with(
            session, monkeypatch, topic.id, error=ResearchError("研究服务暂时不可用")
        )
        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)
        body = client.get("/").text

        assert "研究未完成" in body
        assert "研究服务暂时不可用" in body
        assert "重试" in body
        # And it does not claim there was no news.
        assert "未发现达到入报标准" not in body

    def test_partial_coverage_produces_a_report_with_a_calm_warning(
        self, session, monkeypatch, topic, stub_llm, client
    ):
        result = validate_research_payload(result_payload(status="partial"))
        result = result.model_copy(
            update={
                "coverage": result.coverage.model_copy(
                    update={"limitations": ["部分来源访问异常：Google News"]}
                )
            }
        )
        run_id, status, _ = run_pipeline_with(
            session, monkeypatch, topic.id, result=result
        )

        assert status == RunStatus.COMPLETED_WITH_ERRORS
        run = runs_repo.get_run(session, run_id)
        assert run.coverage_status == COVERAGE_PARTIAL

        report = reports_repo.get_by_run(session, run_id)
        assert report is not None, "partial coverage still yields a usable report"
        assert report.coverage_status == COVERAGE_PARTIAL
        assert report.coverage_json["degraded"] is True

        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)
        body = client.get("/").text
        assert "部分完成" in body
        assert "结果可能不完整" in body

    def test_a_complete_pass_with_no_events_says_so_truthfully(
        self, session, monkeypatch, topic, stub_llm, client
    ):
        raw = result_payload()
        raw["events"] = []
        raw["report"]["sections"][0]["summary_items"] = []
        result = validate_research_payload(raw)

        run_id, status, _ = run_pipeline_with(
            session, monkeypatch, topic.id, result=result
        )
        assert status == RunStatus.COMPLETED

        run = runs_repo.get_run(session, run_id)
        assert run.coverage_status == COVERAGE_COMPLETE

        report = reports_repo.get_by_run(session, run_id)
        assert report is not None
        assert report.item_count == 0

        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)
        body = client.get("/").text
        assert "研究完成" in body
        assert "未发现达到入报标准的重要动态" in body
        # Not the failure screen.
        assert "研究未完成" not in body

    def test_a_topic_deleted_after_queueing_fails_rather_than_crashing(
        self, session, monkeypatch, topic
    ):
        """``ON DELETE SET NULL`` leaves the run orphaned, not dangling.

        The intelligence a topic produced outlives the topic, so the foreign
        key nulls rather than cascades - which means a run can legitimately
        find itself with no topic to research, and has to say so.
        """
        from aios.services import research_pipeline

        monkeypatch.setattr(
            research_pipeline, "build_agent", lambda s, **kw: StubAgent()
        )
        run = create_research_run(session, topic.id)
        run_id = run.id
        session.commit()

        topics_repo.delete_topic(session, topics_repo.get_topic(session, topic.id))
        session.commit()
        session.expire_all()
        assert runs_repo.get_run(session, run_id).research_topic_id is None

        assert ResearchPipeline(run_id).execute() == RunStatus.FAILED
        session.expire_all()
        assert reports_repo.get_by_run(session, run_id) is None


class TestSectionHandling:
    def test_an_event_in_an_undeclared_section_still_reaches_the_report(
        self, session, monkeypatch, topic, stub_llm
    ):
        """Losing intelligence over a labelling inconsistency is unacceptable."""
        raw = result_payload()
        raw["events"][0]["section"] = "一个没有声明的领域"
        result = validate_research_payload(raw)

        run_id, _, _ = run_pipeline_with(session, monkeypatch, topic.id, result=result)
        report = reports_repo.get_by_run(session, run_id)

        names = [s.module_name for s in report.sections]
        assert "一个没有声明的领域" in names
        assert report.item_count == 1

    def test_previous_section_names_are_offered_to_the_agent(
        self, session, monkeypatch, topic, stub_llm
    ):
        """So a topic keeps a recognisable shape across days."""
        result = validate_research_payload(result_payload())
        run_pipeline_with(session, monkeypatch, topic.id, result=result)
        _, _, agent = run_pipeline_with(session, monkeypatch, topic.id, result=result)

        assert "移动智能终端侧" in agent.requests[0].preferred_sections


class TestUsageAccounting:
    def test_research_usage_is_recorded_against_the_run(
        self, session, monkeypatch, topic
    ):
        """Reuses LLMUsage; Simple mode never shows it, Professional mode can."""
        from aios.models import LLMUsage
        from aios.services import research_pipeline

        result = validate_research_payload(result_payload())

        class RecordingAgent(StubAgent):
            def run(self, request):
                # Agents report usage through the sink the pipeline supplies.
                self.sink(
                    {
                        "purpose": "research",
                        "provider_id": "stub",
                        "model": "stub-model",
                        "latency_ms": 1234,
                        "success": True,
                        "extra_json": {"agent_id": LOCAL_AGENT.agent_id},
                    }
                )
                return super().run(request)

        agent = RecordingAgent(result=result)

        def build(session_arg, **kwargs):
            agent.sink = kwargs["usage_sink"]
            return agent

        monkeypatch.setattr(research_pipeline, "build_agent", build)
        monkeypatch.setattr(
            research_pipeline, "build_llm_service", lambda **kw: StubLLM()
        )

        run = create_research_run(session, topic.id)
        run_id = run.id
        session.commit()
        ResearchPipeline(run_id).execute()
        session.expire_all()

        rows = session.query(LLMUsage).filter(LLMUsage.run_id == run_id).all()
        assert rows
        assert any(row.purpose == "research" for row in rows)
        assert any(row.latency_ms == 1234 for row in rows)
