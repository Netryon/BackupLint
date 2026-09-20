"""HTTPS JSON SIEM transport tests against a local mock server."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from backuplint.secrets import SecretRef, SecretSource
from backuplint.siem.config import SiemAuthType, SiemConfig
from backuplint.siem.event import SiemCategory, SiemEvent, SiemEventFamily, SiemSeverity
from backuplint.siem.transport.base import classify_http_status
from backuplint.siem.transport.https_json import HttpsJsonTransport, parse_retry_after


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"{}",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._body
        chunk = self._body[:size]
        self._body = self._body[size:]
        return chunk

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeHTTPError(Exception):
    def __init__(self, code: int, headers: dict[str, str] | None = None) -> None:
        self.code = code
        self.headers = headers or {}
        self._body = b"error"

    def read(self, size: int = -1) -> bytes:
        return self._body


def _event() -> SiemEvent:
    return SiemEvent(
        schema_version=1,
        event_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        event_family=SiemEventFamily.AGENT_OFFLINE,
        severity=SiemSeverity.MEDIUM,
        category=SiemCategory.FLEET_TRANSPORT,
        occurred_at=datetime.now(UTC).isoformat(),
        received_at=datetime.now(UTC).isoformat(),
        source_role="controller",
        summary="Agent offline",
        agent_id="agent-1",
    )


def _config(*, bearer: bool = True) -> SiemConfig:
    auth_token = (
        SecretRef(source=SecretSource.ENV, name="BACKUPLINT_SIEM_TOKEN")
        if bearer
        else None
    )
    return SiemConfig(
        enabled=True,
        endpoint="https://siem.example.internal/ingest",
        auth_type=SiemAuthType.BEARER if bearer else SiemAuthType.NONE,
        auth_token=auth_token,
        tls_verify=False,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=2.0,
        max_backoff_seconds=30.0,
    )


def test_successful_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKUPLINT_SIEM_TOKEN", "super-secret-token")
    captured: dict[str, Any] = {}

    def opener(req, **kwargs):
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        return _FakeResponse(status=200)

    transport = HttpsJsonTransport(opener=opener)
    result = transport.send(_event(), config=_config())
    assert result.delivered is True
    assert result.retryable is False
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["idempotency-key"] == _event().event_id
    assert headers["authorization"] == "Bearer super-secret-token"
    assert "super-secret-token" not in str(result.error or "")


@pytest.mark.parametrize(
    ("status", "retryable", "retry_after"),
    [
        (401, True, None),
        (403, True, None),
        (429, True, "2"),
        (500, True, None),
        (302, True, None),
    ],
)
def test_failure_classification(status, retryable, retry_after) -> None:
    result = classify_http_status(status, retry_after_seconds=parse_retry_after(retry_after))
    assert result.delivered is False
    assert result.retryable is retryable
    if status == 429:
        assert result.retry_after_seconds == 2.0
    if status == 302:
        assert result.error == "redirect not followed"


def test_auth_and_rate_limit_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKUPLINT_SIEM_TOKEN", "token")

    def opener_401(req, **kwargs):
        import urllib.error

        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            {"Retry-After": "0"},
            io.BytesIO(b""),
        )

    transport = HttpsJsonTransport(opener=opener_401)
    result = transport.send(_event(), config=_config())
    assert result.delivered is False
    assert result.retryable is True
    assert result.status_code == 401

    def opener_429(req, **kwargs):
        import urllib.error

        raise urllib.error.HTTPError(
            req.full_url,
            429,
            "Too Many Requests",
            {"Retry-After": "5"},
            io.BytesIO(b""),
        )

    transport = HttpsJsonTransport(opener=opener_429)
    result = transport.send(_event(), config=_config())
    assert result.retryable is True
    assert result.retry_after_seconds == 5.0


def test_timeout_is_retryable() -> None:
    def _boom(*args, **kwargs):
        raise TimeoutError("timed out")

    transport = HttpsJsonTransport(opener=_boom)
    result = transport.send(
        _event(),
        config=SiemConfig(
            enabled=True,
            endpoint="https://example.invalid/ingest",
            auth_type=SiemAuthType.NONE,
            tls_verify=False,
        ),
    )
    assert result.delivered is False
    assert result.retryable is True
    assert result.error == "request timed out"


def test_endpoint_must_be_https() -> None:
    transport = HttpsJsonTransport()
    result = transport.send(
        _event(),
        config=SiemConfig(enabled=True, endpoint="http://insecure.example/ingest"),
    )
    assert result.delivered is False
    assert result.retryable is False


def test_response_body_not_required_for_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKUPLINT_SIEM_TOKEN", "token")
    captured: dict[str, bytes] = {}

    def opener(req, **kwargs):
        captured["body"] = req.data or b""
        return _FakeResponse(status=200, body=b"not-json-garbage")

    transport = HttpsJsonTransport(opener=opener)
    result = transport.send(_event(), config=_config())
    assert result.delivered is True
    payload = json.loads(captured["body"].decode("utf-8"))
    assert payload["event_id"] == _event().event_id


def test_url_error_is_retryable() -> None:
    import urllib.error

    def opener(req, **kwargs):
        raise urllib.error.URLError("connection reset")

    transport = HttpsJsonTransport(opener=opener)
    result = transport.send(_event(), config=_config(bearer=False))
    assert result.retryable is True
    assert result.delivered is False
