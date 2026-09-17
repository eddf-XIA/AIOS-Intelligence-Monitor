"""API-key handling and the provider contract.

The security assertions here are the ones that matter most: the full key must
never reach the database, a log, an HTML report or a JSON audit file.

The HTTP behaviour tested against ``OpenAICompatibleProvider`` is the path every
OpenAI-dialect vendor shares - DeepSeek, 通义千问, 智谱, Kimi, 硅基流动, Ollama
and the rest - so these cases cover all of them at once.
"""

from __future__ import annotations

import json

import pytest

from aios.services import keyring_service
from aios.services.llm import (
    InsufficientBalance,
    InvalidJSONResponse,
    LLMError,
    MissingAPIKey,
    RateLimited,
    extract_json,
)
from aios.services.llm.registry import build_provider, runtime_config
from conftest import FakeResponse, chat_payload

SECRET = "sk-testkey1234567890abcdef6307"


class FakeKeyring:
    """In-memory stand-in for the OS credential vault."""

    def __init__(self):
        self.store = {}

    def set_password(self, service, username, value):
        self.store[(service, username)] = value

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(keyring_service, "_backend", lambda: fake)
    return fake


class TestMasking:
    def test_mask_shows_only_the_tail(self):
        masked = keyring_service.mask(SECRET)
        assert masked.endswith("6307")
        assert SECRET not in masked
        assert "sk-" not in masked

    def test_last4(self):
        assert keyring_service.last4(SECRET) == "6307"

    def test_mask_of_empty_is_empty(self):
        assert keyring_service.mask("") == ""
        assert keyring_service.mask(None) == ""

    def test_redact_removes_key_shaped_text(self):
        text = f"Request failed with Authorization: Bearer {SECRET} in header"
        cleaned = keyring_service.redact(text)
        assert SECRET not in cleaned
        assert "REDACTED" in cleaned

    def test_redact_removes_explicit_secret(self):
        cleaned = keyring_service.redact("value=abcdefghijkl", "abcdefghijkl")
        assert "abcdefghijkl" not in cleaned

    def test_redact_handles_empty(self):
        assert keyring_service.redact("") == ""


class TestKeyStorage:
    def test_roundtrip(self, fake_keyring):
        keyring_service.set_api_key(SECRET)
        assert keyring_service.get_api_key() == SECRET
        assert keyring_service.has_api_key() is True

    def test_delete(self, fake_keyring):
        keyring_service.set_api_key(SECRET)
        assert keyring_service.delete_api_key() is True
        assert keyring_service.get_api_key() is None
        assert keyring_service.delete_api_key() is False

    def test_empty_key_rejected(self, fake_keyring):
        with pytest.raises(ValueError):
            keyring_service.set_api_key("   ")

    def test_key_never_lands_in_the_database(self, session, fake_keyring):
        """The strongest invariant: grep every DB file on disk for the secret."""
        from pathlib import Path

        from aios.database import get_engine
        from aios.services.settings_service import api_key_status, get_str, save_api_key

        save_api_key(session, SECRET)
        session.commit()

        status = api_key_status(session)
        assert status["configured"] is True
        assert status["masked"].endswith("6307")
        assert SECRET not in json.dumps(status)
        # Only the masked tail is persisted.
        assert get_str(session, "deepseek_key_last4") == "6307"

        engine = get_engine()
        db_path = Path(engine.url.database)
        engine.dispose()  # checkpoints and removes the WAL

        # Scan the main database plus any WAL/journal sidecars.
        scanned = 0
        for candidate in db_path.parent.glob(db_path.name + "*"):
            blob = candidate.read_bytes()
            scanned += 1
            assert SECRET.encode() not in blob, f"secret found in {candidate.name}"
            assert b"sk-testkey" not in blob, f"key prefix found in {candidate.name}"
        assert scanned >= 1

    def test_clear_key_updates_status(self, session, fake_keyring):
        from aios.services.settings_service import api_key_status, clear_api_key, save_api_key

        save_api_key(session, SECRET)
        clear_api_key(session)
        assert api_key_status(session)["configured"] is False


class TestExtractJSON:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_wrapped_in_prose(self):
        assert extract_json('Here you go: {"a": 1} hope that helps') == {"a": 1}

    def test_empty_raises(self):
        with pytest.raises(InvalidJSONResponse):
            extract_json("")

    def test_non_object_raises(self):
        with pytest.raises(InvalidJSONResponse):
            extract_json("[1, 2, 3]")

    def test_garbage_raises(self):
        with pytest.raises(InvalidJSONResponse):
            extract_json("no json at all here")


class StubHTTP:
    """Scripted requests.Session replacement."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_client(config, api_key=None, usage_sink=None, session=None, provider_id="deepseek"):
    """Build a provider for a preset using the shared test config."""
    return build_provider(
        runtime_config(
            provider_id=provider_id,
            base_url=config.base_url,
            model=config.model,
            api_key=api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
            retries=config.retries,
        ),
        usage_sink=usage_sink,
        session=session,
    )


class TestOpenAICompatibleProvider:
    def test_successful_call_records_usage(self, deepseek_config):
        recorded = []
        http = StubHTTP(
            [
                FakeResponse(
                    200,
                    chat_payload(
                        '{"status": "ok"}',
                        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    ),
                )
            ]
        )
        client = make_client(
            deepseek_config, api_key=SECRET, usage_sink=recorded.append, session=http
        )
        result = client.generate_json([{"role": "user", "content": "hi"}], purpose="t")

        assert result.data == {"status": "ok"}
        assert result.total_tokens == 15
        assert recorded[0]["success"] is True
        assert recorded[0]["total_tokens"] == 15

    def test_json_mode_is_requested(self, deepseek_config):
        http = StubHTTP([FakeResponse(200, chat_payload('{"a":1}'))])
        make_client(deepseek_config, api_key=SECRET, session=http).generate_json(
            [{"role": "user", "content": "x"}]
        )
        assert http.calls[0]["json"]["response_format"] == {"type": "json_object"}

    def test_missing_key_raises_before_any_request(self, deepseek_config):
        http = StubHTTP([])
        client = make_client(deepseek_config, api_key="", session=http)
        with pytest.raises(MissingAPIKey):
            client.generate_json([{"role": "user", "content": "x"}])
        assert http.calls == []

    def test_retries_then_succeeds(self, deepseek_config, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        http = StubHTTP(
            [
                FakeResponse(200, chat_payload("not json at all")),
                FakeResponse(200, chat_payload('{"ok": true}')),
            ]
        )
        client = make_client(deepseek_config, api_key=SECRET, session=http)
        result = client.generate_json([{"role": "user", "content": "x"}])
        assert result.data == {"ok": True}
        assert result.attempts == 2

    def test_rate_limit_is_not_retried(self, deepseek_config):
        http = StubHTTP([FakeResponse(429)])
        client = make_client(deepseek_config, api_key=SECRET, session=http)
        with pytest.raises(RateLimited) as exc:
            client.generate_json([{"role": "user", "content": "x"}])
        assert "429" in str(exc.value)
        assert len(http.calls) == 1

    def test_insufficient_balance_message(self, deepseek_config):
        http = StubHTTP([FakeResponse(402)])
        client = make_client(deepseek_config, api_key=SECRET, session=http)
        with pytest.raises(InsufficientBalance):
            client.generate_json([{"role": "user", "content": "x"}])

    def test_unauthorized_is_readable(self, deepseek_config):
        http = StubHTTP([FakeResponse(401)])
        client = make_client(deepseek_config, api_key=SECRET, session=http)
        with pytest.raises(LLMError) as exc:
            client.generate_json([{"role": "user", "content": "x"}])
        assert "401" in str(exc.value)

    def test_errors_never_leak_the_key(self, deepseek_config, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        http = StubHTTP([RuntimeError(f"connection failed using {SECRET}")] * 2)
        client = make_client(deepseek_config, api_key=SECRET, session=http)
        with pytest.raises(LLMError) as exc:
            client.generate_json([{"role": "user", "content": "x"}])
        assert SECRET not in str(exc.value)
        assert "REDACTED" in str(exc.value)

    def test_test_connection_success(self, deepseek_config):
        http = StubHTTP([FakeResponse(200, chat_payload('{"status":"ok"}', model="m1"))])
        client = make_client(deepseek_config, api_key=SECRET, session=http)
        result = client.test_connection()
        assert result["ok"] is True
        assert result["model"] == "m1"
        assert result["latency_ms"] >= 0

    def test_test_connection_never_raises(self, deepseek_config):
        http = StubHTTP([FakeResponse(500)])
        client = make_client(deepseek_config, api_key=SECRET, session=http)
        result = client.test_connection()
        assert result["ok"] is False
        assert SECRET not in json.dumps(result)

    def test_test_connection_without_key(self, deepseek_config):
        """The error points the user at Settings, in the app's own language."""
        client = make_client(deepseek_config, api_key="", session=StubHTTP([]))
        result = client.test_connection()
        assert result["ok"] is False
        assert "API Key" in result["error"]
        assert "设置" in result["error"]

    def test_failed_call_is_also_recorded(self, deepseek_config, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        recorded = []
        http = StubHTTP([FakeResponse(500), FakeResponse(500)])
        client = make_client(
            deepseek_config, api_key=SECRET, usage_sink=recorded.append, session=http
        )
        with pytest.raises(LLMError):
            client.generate_json([{"role": "user", "content": "x"}])
        assert recorded[0]["success"] is False
