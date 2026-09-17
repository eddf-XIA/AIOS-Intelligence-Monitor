"""Transport settings, verified against a real proxy process.

Everything else about proxying is easy to get wrong in a way unit tests miss:
the settings can be stored perfectly and still never reach ``requests``. So
these tests stand up an actual HTTP proxy on localhost and assert that traffic
went *through it* - and that a proxy failure is classified as a proxy failure
rather than as "no results".
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from aios.services.collector import CollectorStatus, classify_exception
from aios.services.net_policy import (
    CONNECTION_CUSTOM,
    CONNECTION_DIRECT,
    CONNECTION_SYSTEM,
    TransportSettings,
    build_session,
)


class _ProxyHandler(BaseHTTPRequestHandler):
    """Answers absolute-URI GETs, which is exactly what a forward proxy sees."""

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self.server.seen.append((self.path, dict(self.headers)))
        body = b'{"articles": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output clean
        return


@pytest.fixture
def proxy():
    """A real forward proxy on localhost, recording every absolute URI it sees."""
    server = HTTPServer(("127.0.0.1", 0), _ProxyHandler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def proxy_url(server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


class TestProxyRouting:
    def test_traffic_actually_goes_through_a_custom_proxy(self, proxy):
        """The claim that matters: the setting changes where bytes go."""
        http = build_session(
            TransportSettings(mode=CONNECTION_CUSTOM, http_proxy=proxy_url(proxy)),
            requests.Session(),
        )

        response = http.get("http://example.invalid/api/doc", timeout=5)

        assert response.status_code == 200
        assert proxy.seen, "the proxy never saw the request"
        path, _headers = proxy.seen[0]
        assert path == "http://example.invalid/api/doc", (
            "the proxy received an absolute URI, so it really was proxied"
        )

    def test_a_collector_sends_its_requests_through_the_proxy(self, proxy):
        """End to end: a real collector, a real proxy, real politeness settings."""
        import datetime as dt

        from aios.services.collector import GDELTCollector
        from aios.services.net_policy import RateLimiter

        http = build_session(
            TransportSettings(mode=CONNECTION_CUSTOM, http_proxy=proxy_url(proxy)),
            requests.Session(),
        )
        collector = GDELTCollector(session=http, limiter=RateLimiter(0, 1))
        # Point the collector at plain HTTP so the proxy can serve it without
        # having to implement CONNECT tunnelling.
        collector.endpoint = "http://api.gdeltproject.invalid/api/v2/doc/doc"

        result = collector.fetch(
            "operating system", dt.datetime(2026, 9, 12), dt.datetime(2026, 9, 15), 5
        )

        assert result.ok, result.error
        assert result.zero_results, "the proxy served an empty but valid GDELT body"
        assert proxy.seen, "the collector's request did not reach the proxy"
        assert "gdeltproject.invalid" in proxy.seen[0][0]

    def test_direct_mode_ignores_the_environment(self, proxy, monkeypatch):
        """直连 must mean direct, even when the OS says to use a proxy."""
        monkeypatch.setenv("HTTP_PROXY", proxy_url(proxy))
        monkeypatch.setenv("HTTPS_PROXY", proxy_url(proxy))

        http = build_session(
            TransportSettings(mode=CONNECTION_DIRECT), requests.Session()
        )

        assert http.trust_env is False
        assert http.proxies == {}
        with pytest.raises(requests.exceptions.RequestException):
            http.get("http://example.invalid/should-not-resolve", timeout=3)
        assert proxy.seen == [], "direct mode still went through the system proxy"

    def test_system_mode_follows_the_environment(self, proxy, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", proxy_url(proxy))

        http = build_session(
            TransportSettings(mode=CONNECTION_SYSTEM), requests.Session()
        )
        http.get("http://example.invalid/env", timeout=5)

        assert proxy.seen, "system mode should have honoured HTTP_PROXY"

    def test_proxy_credentials_reach_the_wire_but_never_the_database(self, proxy):
        """The password is added at call time, from the keyring, and nowhere else."""
        import base64

        settings = TransportSettings(
            mode=CONNECTION_CUSTOM,
            http_proxy=proxy_url(proxy),
            proxy_username="alice",
            proxy_password="hunter2",
        )
        http = build_session(settings, requests.Session())
        http.get("http://example.invalid/secure", timeout=5)

        assert proxy.seen
        _path, headers = proxy.seen[0]
        auth = headers.get("Proxy-Authorization", "")
        assert auth.startswith("Basic ")
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
        assert decoded == "alice:hunter2"

        # The credential is never part of anything we would display or store.
        assert "hunter2" not in settings.describe()
        assert "hunter2" not in repr(settings)


class TestProxyFailureClassification:
    def test_an_unreachable_proxy_is_a_proxy_error_not_an_empty_result(self):
        """A dead proxy must never look like a source with no news."""
        import datetime as dt

        from aios.services.collector import GDELTCollector
        from aios.services.net_policy import RateLimiter

        http = build_session(
            # Port 1 is reserved and nothing listens there.
            TransportSettings(mode=CONNECTION_CUSTOM, http_proxy="http://127.0.0.1:1"),
            requests.Session(),
        )
        collector = GDELTCollector(session=http, limiter=RateLimiter(0, 1), max_attempts=1)
        collector.endpoint = "http://api.gdeltproject.invalid/api/v2/doc/doc"

        result = collector.fetch(
            "q", dt.datetime(2026, 9, 12), dt.datetime(2026, 9, 15), 5
        )

        assert result.failed
        assert not result.zero_results
        assert result.status in (
            CollectorStatus.PROXY_ERROR,
            CollectorStatus.NETWORK_ERROR,
        )

    def test_requests_proxy_errors_map_to_the_proxy_status(self):
        status, message = classify_exception(requests.exceptions.ProxyError("boom"))
        assert status == CollectorStatus.PROXY_ERROR
        assert "代理" in message

    def test_a_proxy_failure_contributes_to_source_health_as_a_failure(self):
        from aios.services.collector import CollectorResult
        from aios.services.source_health import HealthState, SourceHealthTracker

        tracker = SourceHealthTracker()
        tracker.record(
            CollectorResult(
                collector="gdelt", status=CollectorStatus.PROXY_ERROR, error="代理连接失败"
            )
        )
        counters = tracker.snapshot()[0]
        assert counters.network_errors == 1
        assert counters.requests_succeeded == 0
        assert tracker.state_of("gdelt") == HealthState.UNREACHABLE
