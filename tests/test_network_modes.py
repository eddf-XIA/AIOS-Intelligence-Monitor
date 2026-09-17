"""Network modes, transport settings and connectivity diagnostics.

No test here reaches the network: every probe and every collector request is
served by a fake ``requests`` session.
"""

from __future__ import annotations

import datetime as dt

import pytest
import requests

from aios.services import network_service
from aios.services.collector import (
    CollectorResult,
    CollectorStatus,
    FeedSpec,
    SourceCollector,
)
from aios.services.net_policy import (
    CONNECTION_CUSTOM,
    CONNECTION_DIRECT,
    CONNECTION_SYSTEM,
    TransportSettings,
    split_proxy_credentials,
    strip_credentials,
)
from aios.services.source_health import HealthState, ProbeResult


WINDOW = (dt.datetime(2026, 9, 12), dt.datetime(2026, 9, 15, 23, 59))


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None, text=""):
        self.status_code = status_code
        self.content = content
        self.text = text or content.decode("utf-8", "ignore")
        self.headers = headers or {}

    def json(self):
        import json

        return json.loads(self.text)


class FakeSession:
    """Answers by URL substring; records every call for assertions."""

    def __init__(self, rules, default=None):
        self.rules = rules
        self.default = default
        self.calls: list[tuple[str, str]] = []
        self.proxies: dict = {}
        self.trust_env = True

    def _answer(self, method, url):
        self.calls.append((method, url))
        for needle, outcome in self.rules.items():
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        if isinstance(self.default, Exception):
            raise self.default
        return self.default or FakeResponse(200, b"<rss></rss>")

    def get(self, url, **kwargs):
        return self._answer("GET", url)

    def request(self, method, url, **kwargs):
        return self._answer(method, url)

    def head(self, url, **kwargs):
        return self._answer("HEAD", url)


GDELT_OK = FakeResponse(200, b'{"articles": []}', text='{"articles": []}')


# --- 9. auto mode selects a healthy source ---------------------------------

class TestAutoMode:
    def test_auto_mode_skips_a_source_the_probe_found_unreachable(self, session, db):
        """The probe already proved Google News is down; don't re-prove it 50 times."""
        config = network_service.collection_settings(session)
        config.network_mode = network_service.MODE_AUTO
        registry = network_service.build_registry(config, feeds=[])

        probes = [
            ProbeResult(target="google_news", display_name="Google News",
                        status=CollectorStatus.TIMEOUT, detail="连接超时"),
            ProbeResult(target="gdelt", display_name="GDELT",
                        status=CollectorStatus.OK, latency_ms=310),
        ]
        notes = network_service.apply_mode_policy(registry, config, probes)

        google = registry.by_id("google_news")
        gdelt = registry.by_id("gdelt")
        assert google.breaker.is_open, "an unreachable source must be taken out of rotation"
        assert not gdelt.breaker.is_open
        assert any("Google News" in note for note in notes)

        # And the open circuit is what a request actually meets.
        result = google.fetch("test", *WINDOW, 5)
        assert result.status == CollectorStatus.CIRCUIT_OPEN
        assert result.attempts == 0

    def test_auto_mode_slows_down_a_rate_limited_source_without_disabling_it(
        self, session, db
    ):
        config = network_service.collection_settings(session)
        registry = network_service.build_registry(config, feeds=[])
        probes = [
            ProbeResult(target="gdelt", display_name="GDELT",
                        status=CollectorStatus.RATE_LIMITED, detail="HTTP 429")
        ]
        network_service.apply_mode_policy(registry, config, probes)

        gdelt = registry.by_id("gdelt")
        assert not gdelt.breaker.is_open, "429 is not the same as unreachable"
        assert gdelt.limiter._paused_until > 0

    def test_probe_failures_never_break_the_run(self, session, db, monkeypatch):
        """A probe that raises must degrade to 'not checked', not to a crash."""
        config = network_service.collection_settings(session)
        registry = network_service.build_registry(config, feeds=[])
        notes = network_service.apply_mode_policy(registry, config, probes=None)
        assert isinstance(notes, list)
        assert not registry.by_id("gdelt").breaker.is_open


# --- 10 & 11. china / international modes ----------------------------------

class TestModeOrdering:
    def test_china_mode_does_not_depend_on_google_news(self, session, db):
        network_service.set_network_mode(session, network_service.MODE_CHINA)
        session.flush()

        config = network_service.collection_settings(session)
        assert config.network_mode == "china"

        registry = network_service.build_registry(
            config, feeds=[FeedSpec(url="https://example.com/feed.xml", title="官方博客")]
        )
        order = [c.name for c in registry.ordered()]
        assert order[0] != "google_news", "Google News must not be the primary source"
        assert order.index("google_news") == len(order) - 1, "it is the last-resort fallback"
        assert "rss" in order and "gdelt" in order

        notes = network_service.apply_mode_policy(registry, config)
        assert any("中国大陆" in note for note in notes)
        # It is still *available* - just not assumed, and not mandatory.
        assert not registry.by_id("google_news").breaker.is_open

    def test_international_mode_can_use_google_news(self, session, db):
        network_service.set_network_mode(session, network_service.MODE_INTERNATIONAL)
        session.flush()

        config = network_service.collection_settings(session)
        registry = network_service.build_registry(config, feeds=[])
        order = [c.name for c in registry.ordered()]
        assert "google_news" in order
        assert order.index("google_news") < order.index("rss")
        assert not registry.by_id("google_news").breaker.is_open

    def test_international_mode_still_stages_rather_than_fanning_out(
        self, session, db, monkeypatch
    ):
        """'Full data sources' does not mean every query to every collector."""
        from aios.services.collection_planner import CollectionPlanner, QueryCache
        from aios.services.collector import Candidate

        network_service.set_network_mode(session, network_service.MODE_INTERNATIONAL)
        session.flush()
        config = network_service.collection_settings(session)
        registry = network_service.build_registry(config, feeds=[])

        asked: list[str] = []

        def fake_fetch(self, query, start, end, limit=20):
            asked.append(self.name)
            return CollectorResult(
                collector=self.name,
                status=CollectorStatus.OK,
                candidates=[Candidate(title="t", url=f"https://a.com/{self.name}")],
            )

        monkeypatch.setattr(SourceCollector, "fetch", fake_fetch)
        planner = CollectionPlanner(
            registry=registry, network_mode="international",
            cache=QueryCache(ttl_seconds=0),
        )
        planner.collect_query("test query", *WINDOW, 10)

        assert asked == [registry.ordered()[0].name], asked

    def test_unknown_mode_is_refused(self, session, db):
        with pytest.raises(ValueError):
            network_service.set_network_mode(session, "vpn")

    def test_mode_labels_do_not_mention_vpn(self):
        blob = " ".join(network_service.MODE_LABELS.values())
        blob += " " + " ".join(network_service.MODE_DESCRIPTIONS.values())
        assert "VPN" not in blob.upper()


# --- 12. connectivity diagnostics -------------------------------------------

class TestDiagnostics:
    def test_timeout_is_reported_as_a_timeout(self, session, db, real_probes):
        http = FakeSession(
            {
                "news.google.com": requests.exceptions.ConnectTimeout("timed out"),
                "gdeltproject.org": GDELT_OK,
            }
        )
        probes = network_service.probe_sources(session, http=http)
        by_target = {p.target: p for p in probes}

        google = by_target["google_news"]
        assert google.status == CollectorStatus.TIMEOUT
        assert google.state == HealthState.UNREACHABLE
        assert google.as_dict()["status_label"] == "超时"

        gdelt = by_target["gdelt"]
        assert gdelt.status == CollectorStatus.OK
        assert gdelt.as_dict()["status_label"] == "可用"

    def test_rate_limiting_is_reported_as_限流(self, session, db, real_probes):
        http = FakeSession(
            {
                "news.google.com": FakeResponse(200),
                "gdeltproject.org": FakeResponse(429, headers={"Retry-After": "30"}),
            }
        )
        probes = {p.target: p for p in network_service.probe_sources(session, http=http)}
        assert probes["gdelt"].status == CollectorStatus.RATE_LIMITED
        assert probes["gdelt"].as_dict()["status_label"] == "限流"
        assert probes["gdelt"].state == HealthState.RATE_LIMITED

    def test_probes_are_lightweight_not_full_searches(self, session, db, real_probes):
        http = FakeSession({}, default=GDELT_OK)
        network_service.probe_sources(session, http=http)
        methods = {method for method, _ in http.calls}
        assert "HEAD" in methods, "Google News is probed with a HEAD, not a search"
        gdelt_calls = [u for _, u in http.calls if "gdelt" in u]
        assert len(gdelt_calls) == 1, "GDELT is probed exactly once"

    def test_disabled_collectors_are_reported_as_disabled_not_broken(self, session, db, real_probes):
        from aios.services import settings_service

        settings_service.set_value(session, "collector_google_news_enabled", False)
        session.flush()
        http = FakeSession({}, default=GDELT_OK)
        probes = {p.target: p for p in network_service.probe_sources(session, http=http)}
        assert probes["google_news"].status == CollectorStatus.DISABLED
        assert probes["google_news"].state == HealthState.DISABLED
        assert not any("news.google.com" in url for _, url in http.calls)

    def test_rss_with_no_feeds_reports_not_configured(self, session, db, real_probes):
        http = FakeSession({}, default=GDELT_OK)
        probes = {p.target: p for p in network_service.probe_sources(session, http=http)}
        assert probes["rss"].status == CollectorStatus.NOT_CONFIGURED

    def test_diagnostics_output_carries_no_secrets(self, session, db, real_probes, isolated_keyring):
        from aios.services import keyring_service

        secret = "sk-" + "diag" * 8
        keyring_service.set_provider_key("deepseek", secret)
        http = FakeSession({}, default=GDELT_OK)
        probes = network_service.probe_sources(session, http=http)
        blob = " ".join(f"{p.detail} {p.display_name}" for p in probes)
        assert secret not in blob

    def test_diagnose_route_renders_states(self, client, monkeypatch):
        from aios.routers import network as network_router

        monkeypatch.setattr(
            network_router.network_service, "run_diagnostics",
            lambda session, include_ai=True: [
                ProbeResult(target="ai", display_name="AI 模型",
                            status=CollectorStatus.OK, latency_ms=842),
                ProbeResult(target="google_news", display_name="Google News",
                            status=CollectorStatus.TIMEOUT, detail="连接超时"),
                ProbeResult(target="gdelt", display_name="GDELT",
                            status=CollectorStatus.RATE_LIMITED, detail="HTTP 429"),
            ],
        )
        response = client.post("/settings/network/diagnose")
        assert response.status_code == 200
        assert "842 ms" in response.text
        assert "超时" in response.text
        assert "限流" in response.text


# --- transport --------------------------------------------------------------

class TestTransport:
    def test_direct_mode_ignores_system_proxies(self):
        from aios.services.net_policy import build_session

        http = build_session(TransportSettings(mode=CONNECTION_DIRECT), requests.Session())
        assert http.trust_env is False
        assert http.proxies == {}

    def test_custom_proxy_is_applied(self):
        from aios.services.net_policy import build_session

        settings = TransportSettings(
            mode=CONNECTION_CUSTOM,
            http_proxy="http://127.0.0.1:7890",
            https_proxy="http://127.0.0.1:7890",
        )
        http = build_session(settings, requests.Session())
        assert http.proxies["https"] == "http://127.0.0.1:7890"

    def test_proxy_password_is_never_written_to_sqlite(self, session, db, isolated_keyring):
        from aios.models import AppSetting

        network_service.save_transport(
            session, CONNECTION_CUSTOM,
            http_proxy="http://alice:hunter2@127.0.0.1:7890",
            https_proxy="http://alice:hunter2@127.0.0.1:7890",
        )
        session.flush()

        stored = {row.key: row.value for row in session.query(AppSetting).all()}
        assert "hunter2" not in " ".join(stored.values())
        assert stored["http_proxy"] == "http://127.0.0.1:7890"
        assert stored["proxy_username"] == "alice"

        # The credential is reassembled from the keyring only at call time.
        transport = network_service.transport_settings(session)
        assert transport.proxy_password == "hunter2"
        assert "hunter2" in transport.proxies()["http"]
        assert "hunter2" not in transport.describe()

    def test_credentials_are_stripped_for_display(self):
        assert strip_credentials("http://u:p@host:8080") == "http://host:8080"
        assert split_proxy_credentials("http://u:p@host:8080") == (
            "http://host:8080", "u", "p"
        )

    def test_system_mode_is_the_default(self, session, db):
        transport = network_service.transport_settings(session)
        assert transport.mode == CONNECTION_SYSTEM
        assert transport.proxies() is None


# --- feeds ------------------------------------------------------------------

class TestFeeds:
    def test_feed_crud(self, session, db):
        feed = network_service.add_feed(session, "https://example.com/feed.xml", "官方博客")
        session.flush()
        assert feed.domain == "example.com"
        assert network_service.feed_specs(session)[0].url == "https://example.com/feed.xml"

        network_service.toggle_feed(session, feed.id)
        session.flush()
        assert network_service.feed_specs(session) == []

        assert network_service.delete_feed(session, feed.id)
        session.flush()
        assert network_service.list_feeds(session) == []

    def test_duplicate_and_credentialed_feeds_are_refused(self, session, db):
        network_service.add_feed(session, "https://example.com/feed.xml")
        session.flush()
        with pytest.raises(ValueError):
            network_service.add_feed(session, "https://example.com/feed.xml")
        with pytest.raises(ValueError):
            network_service.add_feed(session, "https://u:p@example.org/feed.xml")
        with pytest.raises(ValueError):
            network_service.add_feed(session, "example.org/feed.xml")
