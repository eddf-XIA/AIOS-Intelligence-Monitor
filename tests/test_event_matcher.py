"""Event matching: shortlist rules, LLM decisions and the offline fallback."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from aios.services.event_matcher import (
    EventMatcher,
    overlap_score,
    shortlist,
    slugify_event_key,
    tokenize,
    unique_event_key,
)
from aios.services.intelligence_analyzer import CandidateIntelligence


def make_candidate(title, fact="", topic_id=1, module_id=1):
    return CandidateIntelligence(
        tag="情报",
        title=title,
        fact_summary=fact or title,
        assessment="判断：...",
        importance=1,
        confidence="high",
        article_ids=[1],
        topic_id=topic_id,
        module_id=module_id,
    )


class StubEvent:
    """Just enough of IntelligenceEvent for the matcher."""

    def __init__(self, id, title, summary="", topic_id=1, observation_count=1):
        self.id = id
        self.title = title
        self.summary = summary
        self.topic_id = topic_id
        self.observation_count = observation_count
        self.first_seen_date = dt.date(2026, 9, 10)
        self.last_seen_date = dt.date(2026, 9, 14)


class StubClient:
    """LLM router stand-in returning a scripted decision."""

    def __init__(self, payload=None, raise_error=None):
        self.payload = payload
        self.raise_error = raise_error
        self.calls = 0

    def complete_json(self, messages, **kwargs):
        self.calls += 1
        if self.raise_error:
            raise self.raise_error
        from aios.services.llm import LLMResponse

        return LLMResponse(data=self.payload, content="", model="test", latency_ms=1, attempts=1)


class TestTokenize:
    def test_chinese_is_bigrammed(self):
        assert "鸿蒙" in tokenize("鸿蒙操作系统")

    def test_english_words_kept(self):
        assert "harmonyos" in tokenize("HarmonyOS 7 release")

    def test_stopwords_removed(self):
        assert "the" not in tokenize("the release")

    def test_overlap_of_identical_text_is_one(self):
        assert overlap_score("HarmonyOS 7 生态", "HarmonyOS 7 生态") == 1.0

    def test_overlap_of_unrelated_text_is_low(self):
        assert overlap_score("HarmonyOS 生态发展", "卫星在轨算力测试") < 0.2

    def test_empty_overlap_is_zero(self):
        assert overlap_score("", "anything") == 0.0


class TestShortlist:
    def test_related_event_is_shortlisted(self):
        events = [StubEvent(1, "HarmonyOS 7 生态发展"), StubEvent(2, "卫星在轨算力")]
        picked = shortlist(make_candidate("HarmonyOS 7 生态持续扩大"), events)
        assert picked and picked[0][0].id == 1

    def test_unrelated_events_excluded(self):
        events = [StubEvent(2, "无人机低空空域管理平台上线")]
        assert shortlist(make_candidate("HarmonyOS 7 生态发展"), events) == []

    def test_limit_is_respected(self):
        events = [StubEvent(i, "HarmonyOS 7 生态发展") for i in range(20)]
        assert len(shortlist(make_candidate("HarmonyOS 7 生态"), events, limit=3)) == 3


class TestMatching:
    def test_no_candidates_means_new_event(self):
        decision = EventMatcher(client=None, use_llm=False).match(
            make_candidate("全新的事件标题"), []
        )
        assert decision.decision == "NEW_EVENT"
        assert decision.method == "no_candidates"

    def test_llm_match_is_accepted(self):
        client = StubClient(
            {"decision": "MATCH", "event_id": 1, "confidence": 0.91, "reason": "后续进展"}
        )
        events = [StubEvent(1, "HarmonyOS 7 生态发展")]
        decision = EventMatcher(client).match(make_candidate("HarmonyOS 7 生态再扩大"), events)
        assert decision.is_match is True
        assert decision.event_id == 1
        assert decision.method == "llm"

    def test_low_confidence_match_is_rejected(self):
        """Splitting an event is recoverable; merging wrongly is not."""
        client = StubClient(
            {"decision": "MATCH", "event_id": 1, "confidence": 0.3, "reason": "不确定"}
        )
        events = [StubEvent(1, "HarmonyOS 7 生态发展")]
        decision = EventMatcher(client).match(make_candidate("HarmonyOS 7 生态再扩大"), events)
        assert decision.decision == "NEW_EVENT"

    def test_unknown_event_id_is_rejected(self):
        client = StubClient(
            {"decision": "MATCH", "event_id": 999, "confidence": 0.99, "reason": "x"}
        )
        events = [StubEvent(1, "HarmonyOS 7 生态发展")]
        decision = EventMatcher(client).match(make_candidate("HarmonyOS 7 生态再扩大"), events)
        assert decision.decision == "NEW_EVENT"

    def test_llm_failure_falls_back_to_rules(self):
        from aios.services.llm import LLMError

        client = StubClient(raise_error=LLMError("timeout"))
        events = [StubEvent(1, "HarmonyOS 7 生态发展")]
        decision = EventMatcher(client).match(make_candidate("HarmonyOS 7 生态发展"), events)
        assert decision.method == "rules"
        assert decision.is_match is True

    def test_rule_fallback_needs_strong_overlap(self):
        events = [StubEvent(1, "HarmonyOS 7 生态发展")]
        decision = EventMatcher(client=None, use_llm=False).match(
            make_candidate("HarmonyOS 设备与其他若干无关主题的讨论内容"), events
        )
        assert decision.method in {"rules", "no_candidates"}
        assert decision.decision == "NEW_EVENT"

    def test_malformed_llm_output_falls_back(self):
        client = StubClient({"nonsense": True})
        events = [StubEvent(1, "HarmonyOS 7 生态发展")]
        decision = EventMatcher(client).match(make_candidate("HarmonyOS 7 生态发展"), events)
        assert decision.method == "rules"

    def test_llm_new_event_is_respected(self):
        client = StubClient(
            {"decision": "NEW_EVENT", "event_id": None, "confidence": 0.8, "reason": "不同事件"}
        )
        events = [StubEvent(1, "HarmonyOS 7 生态发展")]
        decision = EventMatcher(client).match(make_candidate("HarmonyOS 7 安全漏洞"), events)
        assert decision.decision == "NEW_EVENT"
        assert decision.method == "llm"


class TestEventKeys:
    def test_key_is_readable_and_prefixed(self):
        key = slugify_event_key("mobile", "HarmonyOS 7 ecosystem growth")
        assert key.startswith("mobile-")
        assert "harmonyos" in key

    def test_chinese_title_still_produces_a_key(self):
        key = slugify_event_key("mobile", "鸿蒙生态发展")
        assert key.startswith("mobile-")
        assert len(key) > len("mobile-")

    def test_uniqueness_appends_a_counter(self):
        existing = {"mobile-x"}
        assert unique_event_key("mobile-x", lambda k: k in existing) == "mobile-x-2"

    def test_free_key_is_returned_unchanged(self):
        assert unique_event_key("mobile-y", lambda k: False) == "mobile-y"

    def test_key_length_is_bounded(self):
        key = slugify_event_key("mobile", "word " * 200)
        assert len(key) <= 160
