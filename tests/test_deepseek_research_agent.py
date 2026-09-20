"""DeepSeek as a remote Research Agent - via the endpoint that can actually search.

DeepSeek has two integrations and they do not have the same capability:

``https://api.deepseek.com`` (OpenAI-compatible)
    Chat only. A built-in tool posted here is rejected outright - HTTP 422,
    ``unknown variant `web_search`, expected `function``` - for both
    ``web_search`` and ``web_search_20250305``. This surface accepts only
    client-side ``function`` tools and can never search server-side.
``https://api.deepseek.com/anthropic`` (Anthropic-compatible)
    Runs ``web_search_20250305`` server-side and returns ``server_tool_use`` /
    ``web_search_tool_result`` blocks carrying live URLs.

Verified live against the real API with a real credential on 2026-09-20:

* control, no tools -> the model refuses, stating it has no search tool and
  its knowledge predates 2026;
* ``web_search_20250305`` -> 6 server-side searches, 50 URLs, and the
  production research prompt still returned a schema-valid ResearchResult;
* through :class:`DeepSeekWebSearchAgent` with a real user's configuration
  (``base_url=https://api.deepseek.com``, ``model=deepseek-chat``) ->
  coverage ``complete``, 4 events, 17 sources, no collector touched.

The tests below run offline over a fake transport and pin the *structure* of
that verified behaviour, so a regression is caught without spending an API
call. The live test at the bottom is opt-in.
"""

from __future__ import annotations

import json

import pytest

from aios.services.llm.base import ProviderRuntimeConfig
from aios.services.research import ResearchError, ResearchRequest
from aios.services.research.catalog import AGENT_DEEPSEEK_WEB, get_agent
from aios.services.research.local_agent import LocalCollectionAgent
from aios.services.research.web_agents import DeepSeekWebSearchAgent

SPEC = get_agent(AGENT_DEEPSEEK_WEB)
FAKE_KEY = "sk-deepseek-unit-test-0000000000000000"


def config(base_url="https://api.deepseek.com", model="deepseek-chat", key=FAKE_KEY):
    return ProviderRuntimeConfig(
        provider_id="deepseek",
        base_url=base_url,
        model=model,
        api_key=key,
        temperature=0.2,
        max_tokens=4000,
        timeout=180,
        retries=3,
        display_name="DeepSeek",
    )


def result_payload():
    """A minimal but schema-valid ResearchResult."""
    return {
        "coverage": {"status": "complete", "sources_examined": 2},
        "report": {
            "title": "人形机器人监测日报",
            "focus_title": "Optimus 启动新一轮量产审厂",
            "focus_summary": "特斯拉在华启动新一轮量产审厂。",
            "sections": [
                {
                    "name": "本体量产与供应链",
                    "summary": "量产爬坡进入实质阶段。",
                    "summary_items": [
                        {
                            "headline": "Optimus 启动新一轮量产审厂",
                            "body": "特斯拉在华启动新一轮量产审厂。",
                            "significance": "判断：量产节奏加快。",
                            "evidence_ids": [0],
                        }
                    ],
                }
            ],
            "market_snapshot": [],
            "trend_analysis": [],
        },
        "events": [
            {
                "title": "Optimus 启动新一轮量产审厂",
                "summary": "特斯拉在华启动新一轮量产审厂。",
                "entities": ["特斯拉"],
                "organization": "特斯拉",
                "product_or_project": "Optimus",
                "event_type": "manufacturing",
                "event_date": "2026-09-19",
                "significance": "判断：量产节奏加快。",
                "importance": 1,
                "section": "本体量产与供应链",
                "source_ids": [0],
            }
        ],
        "sources": [
            {
                "source_id": 0,
                "title": "Optimus 量产审厂",
                "publisher": "36Kr",
                "url": "https://eu.36kr.com/p/optimus-2026",
                "published_at": "2026-09-19",
            },
            {
                "source_id": 1,
                "title": "供应链确认",
                "publisher": "TechWeb",
                "url": "https://m.techweb.com.cn/optimus",
                "published_at": "2026-09-19",
            },
        ],
        "watch_next": ["量产良率"],
    }


def anthropic_response(
    text: str,
    *,
    search_urls=("https://eu.36kr.com/p/optimus-2026", "https://m.techweb.com.cn/optimus"),
    searches: int = 2,
):
    """The shape DeepSeek's Anthropic-compatible endpoint really returns.

    Modelled on the observed live payload: thinking blocks, interleaved
    ``server_tool_use`` and ``web_search_tool_result``, and - importantly -
    URLs inside the tool-result content rather than on text-block citations.
    """
    content = [{"type": "thinking", "thinking": "先检索最近三天的动态。"}]
    for _ in range(searches):
        content.append(
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}
        )
    if search_urls:
        content.append(
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": url,
                        "title": "t",
                        "encrypted_content": "…",
                    }
                    for url in search_urls
                ],
            }
        )
    content.append({"type": "text", "text": text})
    return {
        "id": "msg_1",
        "model": "deepseek-v4-flash",
        "stop_reason": "end_turn",
        "content": content,
        "usage": {
            "input_tokens": 31977,
            "output_tokens": 4200,
            "server_tool_use": {"web_search_requests": searches},
        },
    }


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        self.calls.append(
            {"url": url, "headers": headers or {}, "body": json, "timeout": timeout}
        )
        payload = self._responses.pop(0) if self._responses else self._responses
        if isinstance(payload, tuple):
            return FakeResponse(payload[0], payload[1])
        return FakeResponse(payload)

    def get(self, url, **kwargs):  # pragma: no cover
        raise AssertionError(f"unexpected GET to {url}")


def build(http, **cfg):
    return DeepSeekWebSearchAgent(SPEC, config=config(**cfg), session=http)


def request():
    return ResearchRequest(
        topic_name="人形机器人",
        brief="跟踪人形机器人量产进展。",
        window_hours=72,
    )


# --- the endpoint ----------------------------------------------------------

class TestItUsesTheEndpointThatCanSearch:
    """The whole point of a dedicated adapter."""

    def test_it_posts_to_the_anthropic_compatible_endpoint(self):
        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        build(http).run(request())

        assert http.calls[0]["url"] == "https://api.deepseek.com/anthropic/v1/messages"

    def test_it_never_posts_to_the_openai_chat_surface(self):
        """That surface rejects built-in tools with HTTP 422 and cannot search."""
        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        build(http).run(request())

        for call in http.calls:
            assert "/chat/completions" not in call["url"]
            assert "/responses" not in call["url"]
            assert "/anthropic/" in call["url"]

    @pytest.mark.parametrize(
        "configured,expected",
        [
            ("https://api.deepseek.com", "https://api.deepseek.com/anthropic/v1/messages"),
            ("https://api.deepseek.com/", "https://api.deepseek.com/anthropic/v1/messages"),
            # A hand-written chat base carrying /v1 is the OpenAI surface; the
            # version segment must not end up inside the Anthropic path.
            ("https://api.deepseek.com/v1", "https://api.deepseek.com/anthropic/v1/messages"),
            # Already pointed at the research endpoint: do not double it.
            (
                "https://api.deepseek.com/anthropic",
                "https://api.deepseek.com/anthropic/v1/messages",
            ),
            # A proxy or gateway still works.
            ("https://gw.example.com/ds", "https://gw.example.com/ds/anthropic/v1/messages"),
        ],
    )
    def test_the_research_endpoint_is_derived_from_the_chat_base(
        self, configured, expected
    ):
        """The user configures one URL; they should never learn about /anthropic."""
        assert build(FakeSession(), base_url=configured).messages_url == expected

    def test_an_empty_base_url_falls_back_to_the_official_host(self):
        assert (
            build(FakeSession(), base_url="").messages_url
            == "https://api.deepseek.com/anthropic/v1/messages"
        )


class TestItAsksForServerSideSearch:
    def test_the_web_search_tool_is_in_every_request(self):
        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        build(http).run(request())

        tools = http.calls[0]["body"]["tools"]
        assert tools == [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": DeepSeekWebSearchAgent.MAX_SEARCH_USES,
            }
        ]

    def test_it_authenticates_the_anthropic_way(self):
        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        build(http).run(request())

        headers = http.calls[0]["headers"]
        assert headers["x-api-key"] == FAKE_KEY
        assert headers["anthropic-version"] == "2023-06-01"
        # The OpenAI-style header would be ignored here.
        assert "Authorization" not in headers

    def test_the_users_configured_model_name_is_sent_unchanged(self):
        """Every name tested live worked; the endpoint maps unknown ones itself."""
        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        build(http, model="deepseek-chat").run(request())

        assert http.calls[0]["body"]["model"] == "deepseek-chat"

    def test_the_token_ceiling_leaves_room_for_thinking_and_a_full_report(self):
        """The verified run finished near 16k output tokens."""
        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        build(http).run(request())

        assert http.calls[0]["body"]["max_tokens"] >= 16000

    def test_openai_shaped_extra_body_is_not_forwarded(self):
        """The DeepSeek preset ships ``{"thinking": {"type": "disabled"}}``.

        That is an OpenAI-surface option. Posting it here risks a rejected
        request, which costs a whole slow research pass.
        """
        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        cfg = config()
        cfg.extra_body = {"thinking": {"type": "disabled"}}
        DeepSeekWebSearchAgent(SPEC, config=cfg, session=http).run(request())

        assert "thinking" not in http.calls[0]["body"]


class TestEvidenceComesFromDeepSeeksOwnSearch:
    def test_source_urls_are_harvested_from_the_tool_result_blocks(self):
        """Not from text-block citations - live, those were empty."""
        urls = ("https://a.example.com/1", "https://b.example.com/2")
        http = FakeSession(
            anthropic_response(json.dumps(result_payload()), search_urls=urls)
        )
        agent = build(http)
        agent.run(request())

        assert agent.searched_urls == list(urls)

    def test_the_result_sources_are_the_agents_own_citations(self):
        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        result = build(http).run(request())

        assert [s.url for s in result.sources] == [
            "https://eu.36kr.com/p/optimus-2026",
            "https://m.techweb.com.cn/optimus",
        ]

    def test_it_never_constructs_a_local_collector(self, monkeypatch):
        """A remote agent that quietly collects locally is the bug we fixed."""
        from aios.services import collection_planner, collector

        def boom(*a, **k):
            raise AssertionError("the DeepSeek agent used a local collector")

        monkeypatch.setattr(collection_planner.CollectionPlanner, "__init__", boom)
        for cls in (collector.GDELTCollector, collector.GoogleNewsCollector,
                    collector.RSSCollector):
            monkeypatch.setattr(cls, "fetch", boom)
        monkeypatch.setattr(LocalCollectionAgent, "run", boom)

        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        result = build(http).run(request())
        assert result.coverage.status == "complete"


class TestItRefusesToPassOffChatAsResearch:
    """The failure mode that justifies the whole capability layer."""

    def test_a_response_with_no_search_is_an_error_not_a_report(self):
        """No search performed means the answer came from training data.

        Live, the control run with no tools produced a fluent, confident
        refusal - but a differently-worded prompt could just as easily produce
        a fluent, confident *report* with invented dates and no URLs. If the
        model declines to search, or a future endpoint change stops honouring
        the tool, that must surface as a failed run rather than a report.
        """
        no_search = anthropic_response(
            json.dumps(result_payload()), search_urls=(), searches=0
        )
        http = FakeSession(no_search)

        with pytest.raises(ResearchError) as excinfo:
            build(http).run(request())

        assert "未执行联网检索" in str(excinfo.value)
        assert excinfo.value.retryable is False

    def test_a_search_that_ran_is_accepted(self):
        """The control for the test above."""
        http = FakeSession(anthropic_response(json.dumps(result_payload())))
        assert build(http).run(request()).coverage.status == "complete"

    def test_a_missing_credential_is_reported_before_any_request(self):
        http = FakeSession()
        agent = build(http, key="")

        assert "API Key" in agent.readiness_error()
        with pytest.raises(ResearchError):
            agent.run(request())
        assert http.calls == []


class TestUsageAccounting:
    def test_search_count_comes_from_the_vendors_own_usage_block(self):
        records: list[dict] = []
        http = FakeSession(
            anthropic_response(json.dumps(result_payload()), searches=6)
        )
        DeepSeekWebSearchAgent(
            SPEC, config=config(), session=http, usage_sink=records.append
        ).run(request())

        assert len(records) == 1
        assert records[0]["success"] is True
        assert records[0]["extra_json"]["search_calls"] == 6
        assert records[0]["extra_json"]["agent_id"] == AGENT_DEEPSEEK_WEB

    def test_the_grounding_signal_is_recorded(self):
        """How many URLs the search returned, against how many were cited.

        Live, 14 of 17 citations appeared verbatim in the raw search results.
        The gap is worth watching, so it is recorded rather than discarded.
        """
        records: list[dict] = []
        http = FakeSession(
            anthropic_response(
                json.dumps(result_payload()),
                search_urls=(f"https://e{i}.example.com" for i in range(5)),
            )
        )
        DeepSeekWebSearchAgent(
            SPEC, config=config(), session=http, usage_sink=records.append
        ).run(request())

        assert records[0]["extra_json"]["searched_urls"] == 5
        assert records[0]["extra_json"]["sources"] == 2


# --- opt-in live verification ----------------------------------------------

@pytest.mark.live
def test_live_deepseek_really_searches_the_web():
    """The real thing. Opt in with ``pytest -m live``.

    Kept out of the default run because it costs an API call and depends on
    the open web, but kept in the suite because the offline tests above only
    pin the shape of behaviour that was verified here once by hand.

    The credential comes from ``DEEPSEEK_API_KEY``, not from the OS keyring:
    ``conftest`` deliberately isolates every test from the developer's real
    credential store, and a live test is not a reason to punch through that.

        DEEPSEEK_API_KEY=sk-... pytest -m live
    """
    import os

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        pytest.skip("set DEEPSEEK_API_KEY to run the live DeepSeek verification")

    agent = DeepSeekWebSearchAgent(SPEC, config=config(key=key))
    result = agent.run(
        ResearchRequest(
            topic_name="人工智能",
            brief="最近三天人工智能领域的重要进展。",
            window_hours=72,
        )
    )

    # The search really ran and really returned pages.
    assert agent.searched_urls, "DeepSeek returned no web_search results"
    assert all(url.startswith("http") for url in agent.searched_urls)

    # And the result is usable by the rest of AIOS.
    assert result.coverage.status in {"complete", "partial"}
    assert result.sources, "a research pass with no sources is not research"
    for source in result.sources:
        assert source.url.startswith("http")
