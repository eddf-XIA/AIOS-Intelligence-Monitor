"""研究主题: natural-language creation, preview, saving and conversational edit.

The contract that matters most: **a natural-language edit keeps the same topic
id.** A user who says "以后多关注商业化" is adjusting a topic, and every report,
event and observation already produced must stay attached to it. Creating a new
topic and abandoning the old one would silently destroy the history this product
exists to maintain.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aios.repositories import research_topics as topics_repo
from aios.schemas.research_topic import ResearchBrief, validate_brief
from aios.services.research_topic_service import (
    BriefGenerationError,
    ResearchTopicService,
    fallback_brief,
)

BRIEF_PAYLOAD = {
    "name": "全球人形机器人产业与技术进展",
    "brief": "跟踪人形机器人平台、机器人操作系统与主要厂商的重要进展。",
    "scope": "人形机器人平台、VLA / 世界模型、运动控制、主要厂商产品与商业部署。",
    "focus_areas": ["技术进展", "产品发布", "开源项目", "商业落地"],
    "keywords": ["机器人OS", "VLA", "世界模型", "Figure", "宇树"],
    "exclusions": ["招聘", "培训", "重复转载"],
    "regions": "中国 + 全球",
    "window_hours": 72,
    "notes": "已聚焦到产业与技术进展。",
}


class StubClient:
    """An LLM router stand-in. No provider, no network, no keyring."""

    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else dict(BRIEF_PAYLOAD)
        self.error = error
        self.calls: list[dict] = []

    def readiness_error(self) -> str:
        return ""

    def complete_json(self, messages, purpose="generic", **kwargs):
        self.calls.append({"purpose": purpose, "messages": messages})
        if self.error is not None:
            raise self.error

        from aios.services.llm.base import LLMResponse

        return LLMResponse(
            data=self.payload,
            content="",
            model="stub-model",
            provider_id="stub",
            latency_ms=5,
        )


@pytest.fixture
def service():
    return ResearchTopicService(client=StubClient())


class TestBriefFromNaturalLanguage:
    def test_one_sentence_becomes_a_valid_brief(self, service):
        result = service.refine("帮我看看最近人形机器人有什么重要进展")
        assert result.brief is not None
        assert result.brief.name == BRIEF_PAYLOAD["name"]
        assert result.brief.focus_areas == BRIEF_PAYLOAD["focus_areas"]
        assert result.brief.window_hours == 72

    def test_the_brief_contains_no_search_syntax(self, service):
        """A brief is subject matter, not queries - that is the agent's job."""
        result = service.refine("关注国产 AI PC 操作系统")
        brief = result.brief
        assert brief is not None
        assert not hasattr(brief, "queries")
        combined = f"{brief.brief} {brief.scope}"
        for token in (" OR ", " AND ", "site:", "intitle:"):
            assert token not in combined

    def test_too_short_a_description_is_refused(self, service):
        with pytest.raises(BriefGenerationError):
            service.refine("x")

    def test_an_empty_model_answer_is_refused(self):
        service = ResearchTopicService(client=StubClient(payload={}))
        with pytest.raises(BriefGenerationError):
            service.refine("关注人形机器人进展")

    def test_a_malformed_brief_is_rejected_with_a_readable_reason(self):
        service = ResearchTopicService(client=StubClient(payload={"name": ""}))
        with pytest.raises(BriefGenerationError) as excinfo:
            service.refine("关注人形机器人进展")
        assert "主题" in str(excinfo.value)

    def test_a_missing_goal_falls_back_to_the_users_own_words(self):
        payload = dict(BRIEF_PAYLOAD)
        payload["brief"] = ""
        service = ResearchTopicService(client=StubClient(payload=payload))
        result = service.refine("关注太空智算的最新进展")
        assert result.brief is not None
        assert "太空智算" in result.brief.brief

    def test_alternative_field_names_are_repaired(self):
        service = ResearchTopicService(
            client=StubClient(
                payload={
                    "title": "太空智算",
                    "objective": "跟踪星载算力进展",
                    "focus": ["技术进展"],
                    "exclude": ["招聘"],
                    "time_window": "7天",
                    "geography": ["中国", "全球"],
                }
            )
        )
        brief = service.refine("关注太空智算").brief
        assert brief is not None
        assert brief.name == "太空智算"
        assert brief.brief == "跟踪星载算力进展"
        assert brief.window_hours == 168
        assert brief.regions == "中国 + 全球"

    def test_a_fallback_brief_works_without_a_model(self):
        brief = fallback_brief("关注人形机器人和具身智能的重要进展")
        assert brief.name
        assert "人形机器人" in brief.brief


class TestPreviewWritesNothing:
    def test_refining_does_not_create_a_topic(self, client, session, monkeypatch):
        from aios.routers import simple as simple_router

        monkeypatch.setattr(
            simple_router, "ResearchTopicService",
            lambda *a, **k: ResearchTopicService(client=StubClient()),
        )

        before = topics_repo.count_topics(session)
        body = client.post(
            "/simple/topics/refine", data={"description": "帮我看看最近人形机器人有什么重要进展"}
        ).text

        session.expire_all()
        assert topics_repo.count_topics(session) == before
        # ...but the preview is visible.
        assert BRIEF_PAYLOAD["name"] in body
        assert "保存主题" in body

    def test_the_preview_is_compact_not_a_giant_form(self, client, session, monkeypatch):
        from aios.routers import simple as simple_router

        monkeypatch.setattr(
            simple_router, "ResearchTopicService",
            lambda *a, **k: ResearchTopicService(client=StubClient()),
        )
        body = client.post(
            "/simple/topics/refine", data={"description": "关注人形机器人进展"}
        ).text
        assert "展开详情" in body
        # None of the Classic configuration vocabulary appears.
        for word in ("检索式", "GDELT", "RSS", "Preferred Source", "Collector"):
            assert word not in body


class TestSavingAndLoading:
    def _save(self, client, brief: ResearchBrief):
        return client.post(
            "/simple/topics",
            data={
                "name": brief.name,
                "brief": brief.brief,
                "scope": brief.scope,
                "focus_areas": "\n".join(brief.focus_areas),
                "exclusions": "\n".join(brief.exclusions),
                "keywords": "\n".join(brief.keywords),
                "regions": brief.regions,
                "window_hours": brief.window_hours,
                "depth": brief.depth,
            },
            follow_redirects=False,
        )

    def test_a_saved_topic_appears_in_recent_topics(self, client, session):
        brief = validate_brief(BRIEF_PAYLOAD)
        self._save(client, brief)

        session.expire_all()
        recent = topics_repo.recent_topics(session)
        assert [t.name for t in recent] == [brief.name]

        assert brief.name in client.get("/").text

    def test_loading_a_historical_topic_restores_the_brief(self, client, session):
        brief = validate_brief(BRIEF_PAYLOAD)
        self._save(client, brief)
        session.expire_all()
        topic = topics_repo.get_by_name(session, brief.name)

        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)
        body = client.get("/").text

        assert brief.name in body
        assert "中国 + 全球" in body
        assert "最近72小时" in body
        for area in brief.focus_areas:
            assert area in body
        # It is immediately runnable - no configuration workflow in between.
        assert "开始研究" in body

    def test_a_duplicate_name_does_not_fail_the_save(self, client, session):
        brief = validate_brief(BRIEF_PAYLOAD)
        self._save(client, brief)
        self._save(client, brief)
        session.expire_all()
        assert topics_repo.count_topics(session) == 2

    def test_loading_an_unknown_topic_is_a_404(self, client):
        assert client.post("/simple/topics/99999/load").status_code == 404


class TestNaturalLanguageEdit:
    @pytest.fixture
    def topic(self, session):
        brief = validate_brief(BRIEF_PAYLOAD)
        topic = topics_repo.create_topic(
            session,
            name=brief.name,
            brief=brief.brief,
            scope=brief.scope,
            focus_areas=brief.focus_areas,
            exclusions=brief.exclusions,
            keywords=brief.keywords,
            regions=brief.regions,
            window_hours=brief.window_hours,
        )
        session.commit()
        return topic

    def _revised_payload(self):
        payload = dict(BRIEF_PAYLOAD)
        payload["focus_areas"] = ["产业合作", "商业化落地", "产品发布"]
        payload["notes"] = "已提高产业合作与商业化的权重，降低芯片参数细节。"
        return payload

    def test_an_edit_updates_the_same_topic_id(self, client, session, topic, monkeypatch):
        from aios.routers import simple as simple_router

        original_id = topic.id
        monkeypatch.setattr(
            simple_router, "ResearchTopicService",
            lambda *a, **k: ResearchTopicService(client=StubClient(self._revised_payload())),
        )

        client.post(
            f"/simple/topics/{original_id}/revise",
            data={"instruction": "以后多关注产业合作和商业化，少一点芯片参数。"},
            follow_redirects=False,
        )

        session.expire_all()
        assert topics_repo.count_topics(session) == 1
        updated = topics_repo.get_topic(session, original_id)
        assert updated is not None
        assert updated.id == original_id
        assert "产业合作" in updated.focus_areas

    def test_an_edit_bumps_the_version_and_snapshots_the_old_brief(
        self, client, session, topic, monkeypatch
    ):
        from aios.routers import simple as simple_router

        monkeypatch.setattr(
            simple_router, "ResearchTopicService",
            lambda *a, **k: ResearchTopicService(client=StubClient(self._revised_payload())),
        )
        client.post(
            f"/simple/topics/{topic.id}/revise",
            data={"instruction": "多关注商业化"},
            follow_redirects=False,
        )

        session.expire_all()
        updated = topics_repo.get_topic(session, topic.id)
        assert updated.version == 2
        assert len(updated.revisions) == 1

        revision = updated.revisions[0]
        assert revision.version == 1
        # The snapshot holds what the brief said *before* the edit.
        assert "技术进展" in [str(x) for x in (revision.focus_areas_json or [])]
        assert revision.change_note == "多关注商业化"

    def test_an_edit_does_not_silently_drop_unmentioned_fields(
        self, session, topic
    ):
        """A model that omits a field must not delete it."""
        service = ResearchTopicService(
            client=StubClient({"name": BRIEF_PAYLOAD["name"], "focus_areas": ["商业化"]})
        )
        current = ResearchBrief.from_topic(topic)
        revised = service.revise(current, "多关注商业化").brief

        assert revised is not None
        assert revised.focus_areas == ["商业化"]
        # Everything the instruction did not mention survives.
        assert revised.scope == current.scope
        assert revised.keywords == current.keywords
        assert revised.regions == current.regions

    def test_topic_history_remains_linked_to_previous_reports(
        self, client, session, topic, monkeypatch
    ):
        """The point of a stable id: old reports stay attached after an edit."""
        from aios.models import Report
        from aios.routers import simple as simple_router

        report = Report(
            report_date=dt.date(2026, 9, 19),
            title="旧报告",
            model="m",
            research_topic_id=topic.id,
            engine="agent",
        )
        session.add(report)
        session.commit()
        report_id = report.id

        monkeypatch.setattr(
            simple_router, "ResearchTopicService",
            lambda *a, **k: ResearchTopicService(client=StubClient(self._revised_payload())),
        )
        client.post(
            f"/simple/topics/{topic.id}/revise",
            data={"instruction": "多关注商业化"},
            follow_redirects=False,
        )

        session.expire_all()
        linked = topics_repo.reports_for_topic(session, topic.id)
        assert [r.id for r in linked] == [report_id]

    def test_events_remain_linked_to_the_topic_after_an_edit(
        self, client, session, topic, monkeypatch, make_event
    ):
        from aios.routers import simple as simple_router

        event, _ = make_event(session, title="持续事件")
        event.research_topic_id = topic.id
        session.commit()

        monkeypatch.setattr(
            simple_router, "ResearchTopicService",
            lambda *a, **k: ResearchTopicService(client=StubClient(self._revised_payload())),
        )
        client.post(
            f"/simple/topics/{topic.id}/revise",
            data={"instruction": "多关注商业化"},
            follow_redirects=False,
        )

        session.expire_all()
        assert topics_repo.count_events(session, topic.id) == 1

    def test_a_failed_edit_leaves_the_topic_untouched(
        self, client, session, topic, monkeypatch
    ):
        from aios.routers import simple as simple_router
        from aios.services.llm import LLMError

        before = list(topic.focus_areas)
        monkeypatch.setattr(
            simple_router, "ResearchTopicService",
            lambda *a, **k: ResearchTopicService(
                client=StubClient(error=LLMError("provider down"))
            ),
        )
        response = client.post(
            f"/simple/topics/{topic.id}/revise",
            data={"instruction": "多关注商业化"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        session.expire_all()
        reloaded = topics_repo.get_topic(session, topic.id)
        assert reloaded.focus_areas == before
        assert reloaded.version == 1

    def test_a_rename_collision_does_not_fail_the_edit(self, session, topic):
        """A cosmetic field must not be able to abort a whole revision."""
        other = topics_repo.create_topic(session, name="另一个主题")
        session.commit()

        topics_repo.update_topic(
            session, topic, {"name": other.name, "scope": "新的范围"}
        )
        session.expire_all()

        reloaded = topics_repo.get_topic(session, topic.id)
        assert reloaded.name == BRIEF_PAYLOAD["name"]  # unchanged
        assert reloaded.scope == "新的范围"  # the real edit applied


class TestExplicitEditor:
    def test_the_editor_round_trips_a_brief(self, client, session):
        brief = validate_brief(BRIEF_PAYLOAD)
        body = client.post(
            "/simple/topics/edit",
            data={
                "name": brief.name,
                "brief": brief.brief,
                "scope": brief.scope,
                "focus_areas": "\n".join(brief.focus_areas),
                "exclusions": "\n".join(brief.exclusions),
                "keywords": "\n".join(brief.keywords),
                "regions": brief.regions,
                "window_hours": brief.window_hours,
                "depth": brief.depth,
            },
        ).text
        assert brief.name in body
        assert "技术进展" in body

    def test_edited_values_go_through_the_same_validation(self, client, session):
        """An edit is not a way around the schema's caps and cleaning."""
        client.post(
            "/simple/topics/save",
            data={
                "name": "  多余空格的主题  ",
                "brief": "目标",
                "scope": "",
                # More focus areas than the cap allows, with duplicates.
                "focus_areas": "\n".join([f"维度{i}" for i in range(20)] + ["维度0"]),
                "exclusions": "",
                "keywords": "",
                "regions": "全球",
                "window_hours": "999999",
                "depth": "nonsense",
                "action": "save",
            },
            follow_redirects=False,
        )
        session.expire_all()
        topic = topics_repo.list_topics(session)[0]

        assert topic.name == "多余空格的主题"
        from aios.schemas.research_topic import MAX_FOCUS_AREAS, MAX_WINDOW_HOURS

        assert len(topic.focus_areas) <= MAX_FOCUS_AREAS
        assert topic.window_hours <= MAX_WINDOW_HOURS
        assert topic.depth == "standard"


class TestSimpleScheduling:
    def test_schedule_can_be_turned_on_without_cron_syntax(self, client, session):
        topic = topics_repo.create_topic(session, name="定时主题")
        session.commit()

        client.post(
            f"/simple/topics/{topic.id}/schedule",
            data={"enabled": "on", "time": "08:30"},
            follow_redirects=False,
        )
        session.expire_all()
        reloaded = topics_repo.get_topic(session, topic.id)
        assert reloaded.schedule_enabled is True
        assert reloaded.schedule_time == "08:30"
        assert reloaded in topics_repo.scheduled_topics(session)

    def test_an_invalid_time_is_refused_cleanly(self, client, session):
        topic = topics_repo.create_topic(session, name="定时主题")
        session.commit()

        response = client.post(
            f"/simple/topics/{topic.id}/schedule",
            data={"enabled": "on", "time": "99:99"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        session.expire_all()
        assert topics_repo.get_topic(session, topic.id).schedule_enabled is False

    def test_no_cron_syntax_is_exposed_on_the_home_page(self, client, session):
        topic = topics_repo.create_topic(session, name="定时主题")
        session.commit()
        client.post(f"/simple/topics/{topic.id}/load", follow_redirects=False)

        body = client.get("/").text
        assert "每天自动研究" in body
        for token in ("cron", "CronTrigger", "* * *"):
            assert token not in body
