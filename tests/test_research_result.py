"""The ResearchResult contract and its coverage semantics.

The single most important assertion in this file is that

    "no important developments found"   and   "research failed"

can never become the same thing. v2.1 established that principle for the
Classic collection engine; a Research Agent introduces a new way to violate it,
so it gets its own tests.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aios.schemas.research import (
    COVERAGE_COMPLETE,
    COVERAGE_FAILED,
    COVERAGE_PARTIAL,
    MAX_EVENTS,
    ResearchResult,
    ResearchResultError,
    coerce_research_payload,
    failed_result,
    parse_date,
    validate_research_payload,
)


def payload(**overrides):
    """A well-formed agent answer, minus whatever the test overrides."""
    base = {
        "coverage": {
            "status": "complete",
            "window_from": "2026-09-17",
            "window_to": "2026-09-20",
            "limitations": [],
            "sources_examined": 2,
        },
        "report": {
            "title": "智能终端操作系统监测日报",
            "focus_title": "HarmonyOS 6 正式发布",
            "focus_summary": "华为发布 HarmonyOS 6，重点在 Agent Framework。",
            "sections": [
                {
                    "name": "移动智能终端侧",
                    "summary": "本期以鸿蒙生态为主。",
                    "summary_items": [
                        {
                            "headline": "HarmonyOS 6 正式发布",
                            "body": "华为在开发者大会上发布 HarmonyOS 6。",
                            "significance": "判断：生态节奏加快。",
                            "evidence_ids": [0],
                        }
                    ],
                }
            ],
            "market_snapshot": [
                {"label": "装机量", "value": "8720万", "note": "台", "evidence_ids": [0]}
            ],
            "trend_analysis": [
                {
                    "title": "趋势：端侧 Agent 化",
                    "analysis": "判断：端侧代理框架成为竞争焦点。",
                    "confidence": "medium",
                    "evidence_ids": [0],
                }
            ],
        },
        "events": [
            {
                "title": "HarmonyOS 6 正式发布",
                "summary": "华为发布 HarmonyOS 6。",
                "entities": ["华为"],
                "organization": "华为",
                "product_or_project": "HarmonyOS 6",
                "event_type": "product_launch",
                "event_date": "2026-09-19",
                "significance": "判断：值得持续跟踪。",
                "importance": 1,
                "section": "移动智能终端侧",
                "source_ids": [0],
            }
        ],
        "sources": [
            {
                "source_id": 0,
                "title": "华为发布 HarmonyOS 6",
                "publisher": "华为开发者",
                "url": "https://developer.huawei.com/news/harmonyos6",
                "published_at": "2026-09-19",
            },
            {
                "source_id": 1,
                "title": "鸿蒙生态进展",
                "publisher": "科技日报",
                "url": "https://tech.example.com/harmony",
                "published_at": "2026-09-19",
            },
        ],
        "watch_next": ["Agent Framework 的第三方接入进度"],
    }
    base.update(overrides)
    return base


class TestValidResult:
    def test_a_well_formed_agent_answer_validates(self):
        result = validate_research_payload(payload())
        assert isinstance(result, ResearchResult)
        assert result.coverage.status == COVERAGE_COMPLETE
        assert len(result.events) == 1
        assert result.report.focus_title == "HarmonyOS 6 正式发布"

    def test_identity_fields_survive_validation(self):
        event = validate_research_payload(payload()).events[0]
        assert event.organization == "华为"
        assert event.product_or_project == "HarmonyOS 6"
        assert event.event_type == "product_launch"
        assert event.event_date == dt.date(2026, 9, 19)
        assert event.entities == ["华为"]

    def test_report_and_event_data_stay_separate(self):
        """The distinction the whole design rests on."""
        result = validate_research_payload(payload())
        assert result.report.sections[0].summary_items[0].headline
        assert result.events[0].product_or_project
        # Report items carry no identity, events carry no prose formatting.
        assert not hasattr(result.report.sections[0].summary_items[0], "organization")


class TestRepair:
    def test_a_top_level_report_body_is_repaired(self):
        raw = payload()
        report = raw.pop("report")
        raw.update(report)
        result = validate_research_payload(raw)
        assert result.report.sections
        assert result.report.focus_title

    def test_items_instead_of_summary_items_is_repaired(self):
        raw = payload()
        section = raw["report"]["sections"][0]
        section["items"] = section.pop("summary_items")
        result = validate_research_payload(raw)
        assert result.report.sections[0].summary_items

    def test_a_double_wrapped_result_is_unwrapped(self):
        result = validate_research_payload({"result": payload()})
        assert result.events

    def test_short_field_aliases_are_repaired(self):
        raw = payload()
        event = raw["events"][0]
        event["org"] = event.pop("organization")
        event["product"] = event.pop("product_or_project")
        event["type"] = event.pop("event_type")
        event["date"] = event.pop("event_date")
        result = validate_research_payload(raw)
        assert result.events[0].organization == "华为"
        assert result.events[0].product_or_project == "HarmonyOS 6"
        assert result.events[0].event_type == "product_launch"

    def test_string_source_ids_are_coerced(self):
        raw = payload()
        raw["events"][0]["source_ids"] = ["0"]
        assert validate_research_payload(raw).events[0].source_ids == [0]

    def test_a_bare_url_string_becomes_a_source(self):
        raw = payload()
        raw["sources"] = ["https://example.com/a"]
        raw["events"][0]["source_ids"] = [0]
        result = validate_research_payload(raw)
        assert result.sources[0].url == "https://example.com/a"

    def test_nonsense_is_rejected_rather_than_guessed_at(self):
        with pytest.raises(ResearchResultError):
            validate_research_payload({"report": {"sections": [{"name": ""}]}, "events": "x"})

    def test_a_non_dict_payload_yields_an_empty_repair(self):
        assert coerce_research_payload("not a dict") == {}

    def test_one_malformed_item_does_not_sink_the_whole_pass(self):
        """A research call is expensive; one bad entry must not discard it."""
        raw = payload()
        raw["events"].append({"title": "x", "source_ids": [0]})  # too short
        raw["report"]["sections"][0]["summary_items"].append({"headline": ""})
        result = validate_research_payload(raw)
        assert len(result.events) == 1
        assert len(result.report.sections[0].summary_items) == 1

    def test_caps_are_enforced(self):
        raw = payload()
        raw["events"] = [
            dict(raw["events"][0], title=f"事件 {i}") for i in range(MAX_EVENTS + 20)
        ]
        assert len(validate_research_payload(raw).events) <= MAX_EVENTS


class TestSourceValidation:
    def test_an_event_citing_an_unknown_source_is_dropped(self):
        """AIOS claims every item is traceable, so an untraceable one is not published."""
        raw = payload()
        raw["events"][0]["source_ids"] = [99]
        result = validate_research_payload(raw)
        assert result.events == []

    def test_a_non_http_url_is_dropped(self):
        raw = payload()
        raw["sources"].append({"source_id": 5, "url": "not-a-url"})
        result = validate_research_payload(raw)
        assert all(s.url.startswith("http") for s in result.sources)
        assert 5 not in result.source_ids

    def test_duplicate_urls_are_merged_and_citations_remapped(self):
        raw = payload()
        raw["sources"].append(
            {"source_id": 7, "url": "https://developer.huawei.com/news/harmonyos6"}
        )
        raw["events"][0]["source_ids"] = [7]
        result = validate_research_payload(raw)
        assert len(result.sources) == 2
        # The citation followed the merge rather than being dropped.
        assert result.events[0].source_ids == [0]

    def test_sources_examined_defaults_to_the_source_count(self):
        raw = payload()
        raw["coverage"]["sources_examined"] = 0
        assert validate_research_payload(raw).coverage.sources_examined == 2


class TestCoverageSemantics:
    """complete / partial / failed are three different facts."""

    def test_the_three_states_are_distinct(self):
        assert len({COVERAGE_COMPLETE, COVERAGE_PARTIAL, COVERAGE_FAILED}) == 3

    def test_complete_is_not_failed(self):
        complete = validate_research_payload(payload())
        assert complete.coverage.status == COVERAGE_COMPLETE
        assert complete.coverage.is_failed is False
        assert complete.coverage.is_partial is False

    def test_partial_is_neither_complete_nor_failed(self):
        raw = payload()
        raw["coverage"]["status"] = "partial"
        raw["coverage"]["limitations"] = ["部分来源无法访问"]
        result = validate_research_payload(raw)
        assert result.coverage.is_partial is True
        assert result.coverage.is_failed is False
        assert result.coverage.limitations == ["部分来源无法访问"]

    def test_failed_is_distinguishable_from_an_empty_complete_result(self):
        """The assertion this whole file exists for."""
        failed = failed_result("研究服务暂时不可用")
        raw = payload()
        raw["events"] = []
        raw["report"]["sections"][0]["summary_items"] = []
        empty_but_complete = validate_research_payload(raw)

        # Both have no events...
        assert failed.is_empty is True
        assert empty_but_complete.is_empty is True
        # ...and they are emphatically not the same outcome.
        assert failed.coverage.status == COVERAGE_FAILED
        assert empty_but_complete.coverage.status == COVERAGE_COMPLETE
        assert failed.coverage.is_failed is not empty_but_complete.coverage.is_failed
        assert failed.coverage.label != empty_but_complete.coverage.label

    def test_an_unrecognised_status_degrades_to_partial_not_complete(self):
        """A word we do not understand must not be read as an all-clear."""
        raw = payload()
        raw["coverage"]["status"] = "mostly_fine_probably"
        assert validate_research_payload(raw).coverage.status == COVERAGE_PARTIAL

    @pytest.mark.parametrize(
        "word,expected",
        [
            ("ok", COVERAGE_COMPLETE),
            ("SUCCESS", COVERAGE_COMPLETE),
            ("error", COVERAGE_FAILED),
            ("unavailable", COVERAGE_FAILED),
        ],
    )
    def test_common_synonyms_are_understood(self, word, expected):
        raw = payload()
        raw["coverage"]["status"] = word
        assert validate_research_payload(raw).coverage.status == expected

    def test_a_missing_coverage_block_defaults_to_complete(self):
        raw = payload()
        raw.pop("coverage")
        assert validate_research_payload(raw).coverage.status == COVERAGE_COMPLETE

    def test_a_failed_result_carries_its_reason(self):
        failed = failed_result("研究服务暂时不可用", ["Google News 无法访问"])
        assert "研究服务暂时不可用" in failed.coverage.limitations
        assert "Google News 无法访问" in failed.coverage.limitations

    def test_a_complete_result_with_no_events_is_a_valid_state(self):
        raw = payload()
        raw["events"] = []
        result = validate_research_payload(raw)
        assert result.is_empty is True
        assert result.coverage.status == COVERAGE_COMPLETE
        # The report scaffolding is still present and renderable.
        assert result.report.focus_title


class TestDateParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-09-19", dt.date(2026, 9, 19)),
            ("2026/09/19", dt.date(2026, 9, 19)),
            ("2026年9月19日", dt.date(2026, 9, 19)),
            ("2026-09-19T08:30:00Z", dt.date(2026, 9, 19)),
            (dt.date(2026, 9, 19), dt.date(2026, 9, 19)),
            (dt.datetime(2026, 9, 19, 8, 30), dt.date(2026, 9, 19)),
        ],
    )
    def test_common_date_forms_are_read(self, raw, expected):
        assert parse_date(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "最近", "not a date", "2026-13-45"])
    def test_an_unreadable_date_stays_none_rather_than_being_guessed(self, raw):
        """A wrong date is an active signal pointing the wrong way."""
        assert parse_date(raw) is None
