"""Shared test fixtures.

Every test runs against a throwaway SQLite file in a temp directory. No test
touches the real DeepSeek API, the real network, or the real OS keyring.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios import config as config_module  # noqa: E402
from aios.services.settings_service import DeepSeekConfig  # noqa: E402

logging.disable(logging.CRITICAL)


class InMemoryKeyring:
    """Stand-in for the OS credential vault, scoped to a single test."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, value):
        self.store[(service, username)] = value

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch):
    """Never touch the real credential store from a test.

    Without this, a test that saves or deletes a provider key would mutate the
    developer's actual Windows Credential Manager - and leak state between
    tests, since the vault outlives the temp database.
    """
    fake = InMemoryKeyring()
    from aios.services import keyring_service

    monkeypatch.setattr(keyring_service, "_backend", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def clean_query_cache():
    """The short-term query cache is process-wide by design.

    That is right in production - two manual runs minutes apart should not hit
    a public API twice - but it would let one test's canned results leak into
    the next, so each test starts with an empty cache.
    """
    from aios.services.collection_planner import reset_shared_cache

    reset_shared_cache()
    yield
    reset_shared_cache()


@pytest.fixture(autouse=True)
def no_connectivity_probes(monkeypatch):
    """No test may reach the real network.

    The pipeline runs cheap reachability probes before collecting in auto mode.
    Returning an empty probe list is the honest stand-in for "we did not check":
    :func:`aios.services.network_service.apply_mode_policy` then leaves every
    source available, which is what an un-probed run does in production too.
    A test that wants probe behaviour patches this back.
    """
    from aios.services import network_service

    original = network_service.probe_sources
    monkeypatch.setattr(network_service, "probe_sources", lambda *a, **k: [])
    return original


@pytest.fixture
def real_probes(monkeypatch, no_connectivity_probes):
    """Restore the real probe implementation for tests that fake the HTTP layer.

    The probes themselves are under test here; the network is still not reached
    because the caller supplies a fake ``requests`` session.
    """
    from aios.services import network_service

    monkeypatch.setattr(network_service, "probe_sources", no_connectivity_probes)
    return no_connectivity_probes


@pytest.fixture
def paths(tmp_path):
    """Point the whole application at a temporary data directory.

    Everything resolves the layout through ``config.get_paths()`` at call time,
    so redirecting this one global is enough - no per-module patching.
    """
    original = config_module.get_paths()
    built = config_module.set_paths(config_module.build_paths(tmp_path / "data"))
    try:
        yield built
    finally:
        config_module.set_paths(original)


@pytest.fixture
def db(paths):
    """An initialised, seeded database bound to the temp directory."""
    from aios.database import configure_engine, init_db

    configure_engine(paths.db_path)
    init_db(paths)
    yield paths

    from aios.database import get_engine

    get_engine().dispose()


@pytest.fixture
def session(db):
    """A session for direct repository/service testing."""
    from aios.database import new_session

    s = new_session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def client(db, monkeypatch):
    """A TestClient with the scheduler and run manager kept inert.

    ``RunManager.shutdown`` is neutralised for the same reason the scheduler
    is, but it matters more: the manager is a process-wide singleton owning one
    ThreadPoolExecutor, and the application lifespan shuts it down on exit. One
    test closing its client would therefore leave every later test unable to
    queue a run at all ("cannot schedule new futures after shutdown").

    Patched on the *class*, not on the ``manager`` instance: monkeypatch
    restores an instance attribute by assigning the bound method it captured,
    which permanently shadows the class attribute and silently defeats any
    later class-level patch.
    """
    from fastapi.testclient import TestClient

    from aios.app import create_app
    from aios.services import scheduler as scheduler_module
    from aios.services.run_manager import RunManager

    monkeypatch.setattr(scheduler_module.scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler_module.scheduler, "shutdown", lambda wait=False: None)
    monkeypatch.setattr(RunManager, "shutdown", lambda self, wait=False: None)

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def deepseek_config():
    return DeepSeekConfig(
        base_url="https://api.example.invalid",
        model="test-model",
        temperature=0.0,
        max_tokens=512,
        timeout=5,
        retries=2,
        thinking="disabled",
    )


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def chat_payload(content: str, model: str = "test-model", usage: dict | None = None) -> dict:
    """Build a DeepSeek-shaped chat completion response."""
    body = {"choices": [{"message": {"content": content}}], "model": model}
    if usage:
        body["usage"] = usage
    return body


@pytest.fixture
def make_article():
    """Factory for RawArticle rows without going through collection."""

    def _make(session, **overrides):
        from aios.models import RawArticle
        from aios.services.article_extractor import url_hash
        from aios.timeutil import utcnow

        url = overrides.pop("url", f"https://example.com/{overrides.get('title', 'a')}")
        fields = {
            "url": url,
            "canonical_url": url,
            "url_hash": overrides.pop("url_hash", url_hash(url)),
            "title": "Example article",
            "source": "Example",
            "domain": "example.com",
            "collected_at": utcnow(),
            "last_seen_at": utcnow(),
            "snippet": "",
            "body_text": "Body text.",
            "trust_score": 5.0,
        }
        fields.update(overrides)
        article = RawArticle(**fields)
        session.add(article)
        session.flush()
        return article

    return _make


@pytest.fixture
def make_event():
    """Factory for an event plus one observation."""

    def _make(session, title="Test event", date=None, metrics=None, **overrides):
        from aios.repositories import events as events_repo
        from aios.timeutil import utcnow

        date = date or dt.date(2026, 9, 15)
        event = events_repo.create_event(
            session,
            event_key=overrides.pop("event_key", f"test-{title[:20]}-{date}"),
            title=title,
            summary=overrides.pop("summary", "Summary."),
            module_id=overrides.pop("module_id", None),
            topic_id=overrides.pop("topic_id", None),
            first_seen_at=utcnow(),
            last_seen_at=utcnow(),
            first_seen_date=date,
            last_seen_date=date,
            observation_count=1,
        )
        observation = events_repo.add_observation(
            session,
            event_id=event.id,
            run_id=overrides.pop("run_id", None),
            observation_date=date,
            title=title,
            tag=overrides.pop("tag", "情报"),
            fact_summary=overrides.pop("fact_summary", "Fact."),
            assessment=overrides.pop("assessment", "判断：..."),
            importance=overrides.pop("importance", 1),
            confidence=overrides.pop("confidence", "high"),
            structured_data_json=metrics,
        )
        return event, observation

    return _make


def stub_collectors(monkeypatch, candidates=None, status=None, error=""):
    """Make every collector answer from memory instead of the network.

    The pipeline drives collectors through
    :class:`~aios.services.collection_planner.CollectionPlanner`, so this
    patches :meth:`SourceCollector.fetch` - the one seam every source shares.
    ``status`` lets a test distinguish a source that answered with nothing from
    one that could not answer at all, which the pipeline treats differently.
    """
    from aios.services.collector import CollectorResult, CollectorStatus, SourceCollector

    resolved = status or CollectorStatus.OK

    def fake_fetch(self, query, start, end, limit=20):
        produced = candidates(query) if callable(candidates) else list(candidates or [])
        return CollectorResult(
            collector=self.name,
            status=resolved,
            candidates=list(produced),
            error=error,
        )

    monkeypatch.setattr(SourceCollector, "fetch", fake_fetch)
    return fake_fetch


