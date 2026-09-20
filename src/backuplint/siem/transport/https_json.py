"""HTTPS JSON SIEM transport (stdlib HTTP client)."""

from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from email.utils import parsedate_to_datetime

from backuplint.secrets import ResolveContext, resolve_secret
from backuplint.siem.config import SiemConfig
from backuplint.siem.event import SiemEvent
from backuplint.siem.queue import redact_queue_error
from backuplint.siem.transport.base import TransportResult, classify_http_status

_MAX_RESPONSE_BYTES = 64 * 1024
_TLS_VERIFY_ENV = "BACKUPLINT_SIEM_TLS_VERIFY"


def parse_retry_after(header_value: str | None) -> float | None:
    if header_value is None:
        return None
    text = header_value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(int(text)))
    except ValueError:
        pass
    try:
        from datetime import UTC, datetime

        when = parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def tls_verify_enabled(*, config_verify: bool) -> bool:
    if not config_verify:
        env = os.environ.get(_TLS_VERIFY_ENV, "").strip().lower()
        if env in {"0", "false", "no", "off"}:
            return False
        return True
    return True


class HttpsJsonTransport:
    name = "https_json"

    def __init__(
        self,
        *,
        opener: Callable[..., object] | None = None,
        resolve_context: ResolveContext | None = None,
    ) -> None:
        self._opener = opener
        self._resolve_context = resolve_context

    def send(self, event: SiemEvent, *, config: SiemConfig) -> TransportResult:
        if not config.endpoint.lower().startswith("https://"):
            return TransportResult(
                delivered=False,
                retryable=False,
                status_code=None,
                retry_after_seconds=None,
                error="endpoint must use https://",
            )
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": event.event_id,
        }
        if config.auth_type == "bearer":
            if config.auth_token is None:
                return TransportResult(
                    delivered=False,
                    retryable=True,
                    status_code=None,
                    retry_after_seconds=None,
                    error="bearer auth configured without token ref",
                )
            try:
                token = resolve_secret(
                    config.auth_token,
                    environ=(
                        self._resolve_context.environ
                        if self._resolve_context is not None
                        else None
                    ),
                ).get_secret_value()
            except Exception as exc:
                return TransportResult(
                    delivered=False,
                    retryable=True,
                    status_code=None,
                    retry_after_seconds=None,
                    error=redact_queue_error(str(exc)),
                )
            headers["Authorization"] = f"Bearer {token}"

        body = event.to_json().encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - operator-configured https endpoint
            config.endpoint,
            data=body,
            method="POST",
            headers=headers,
        )
        context = self._ssl_context(config)
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(  # type: ignore[call-arg]
                req,
                context=context,
                timeout=config.connect_timeout_seconds + config.read_timeout_seconds,
            ) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                _drain_response(resp)
                capped_retry = _cap_retry_after(retry_after, config.max_backoff_seconds)
                return classify_http_status(
                    int(status),
                    retry_after_seconds=capped_retry,
                )
        except urllib.error.HTTPError as exc:
            retry_after = parse_retry_after(exc.headers.get("Retry-After"))
            capped_retry = _cap_retry_after(retry_after, config.max_backoff_seconds)
            _drain_response(exc)
            return classify_http_status(
                int(exc.code),
                retry_after_seconds=capped_retry,
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            return TransportResult(
                delivered=False,
                retryable=True,
                status_code=None,
                retry_after_seconds=None,
                error=redact_queue_error(str(reason)),
            )
        except TimeoutError:
            return TransportResult(
                delivered=False,
                retryable=True,
                status_code=None,
                retry_after_seconds=None,
                error="request timed out",
            )

    def _ssl_context(self, config: SiemConfig) -> ssl.SSLContext:
        verify = tls_verify_enabled(config_verify=config.tls_verify)
        if config.tls_ca_file:
            context = ssl.create_default_context(cafile=str(config.tls_ca_file))
        else:
            context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context


def _drain_response(resp: object) -> None:
    read = getattr(resp, "read", None)
    if read is None:
        return
    total = 0
    while total < _MAX_RESPONSE_BYTES:
        chunk = read(min(8192, _MAX_RESPONSE_BYTES - total))
        if not chunk:
            break
        total += len(chunk)


def _cap_retry_after(value: float | None, cap: float) -> float | None:
    if value is None:
        return None
    return min(float(value), cap)
