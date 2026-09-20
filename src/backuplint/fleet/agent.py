"""Fleet agent: enroll, queue, submit local BackupLint results."""

from __future__ import annotations

import json
import platform
import random
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from backuplint import __version__
from backuplint.audit import run_audit
from backuplint.events import new_run_id
from backuplint.fleet.protocol import (
    MAX_BODY_BYTES,
    PROTOCOL_VERSION,
    ResultEnvelope,
    new_submission_id,
    parse_envelope,
)


class AgentError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AgentTransientError(AgentError):
    """Retryable controller response (503/429) — keep queue item, back off."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass
class AgentIdentity:
    agent_id: str
    ca_cert: Path
    client_cert: Path
    client_key: Path
    root: Path

    @classmethod
    def load(cls, root: Path) -> AgentIdentity:
        agent_id = (root / "agent_id").read_text(encoding="utf-8").strip()
        return cls(
            agent_id=agent_id,
            ca_cert=root / "ca.crt",
            client_cert=root / "client.crt",
            client_key=root / "client.key",
            root=root,
        )


class AgentQueue:
    """Durable bounded JSONL queue (0600).

    Uses saturation-safe retention: healthy PASS items may be compacted into an
    explicit DATA_GAP / QUEUE_OVERFLOW marker. Critical FAIL/ERROR items are
    preserved; oldest items are never silently dropped.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_items: int = 200,
        reserved_critical: int = 32,
    ) -> None:
        from backuplint.fleet.queue_policy import SaturatedAgentQueue

        self._impl = SaturatedAgentQueue(
            path, max_items=max_items, reserved_critical=reserved_critical
        )
        self.path = self._impl.path
        self.max_items = self._impl.max_items

    def enqueue(self, envelope: ResultEnvelope) -> None:
        self._impl.enqueue(envelope)

    def peek_all(self) -> list[dict[str, object]]:
        return self._impl.peek_all()

    def remove(self, submission_id: str) -> None:
        self._impl.remove(submission_id)


def parse_retry_after(header_value: str | None) -> float | None:
    """Parse Retry-After as delta-seconds or HTTP-date; return seconds to wait."""
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
        when = parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def retry_backoff_seconds(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 60.0,
    rng: Callable[[], float] | None = None,
) -> float:
    """Exponential backoff with jitter. ``attempt`` is 0-based retry count."""
    delay = min(cap, base * (2 ** max(0, attempt)))
    pick = rng if rng is not None else random.random
    # Full jitter in [0.5, 1.0] of computed delay.
    return delay * (0.5 + 0.5 * pick())


def compute_retry_delay(
    attempt: int,
    retry_after: float | None = None,
    *,
    base: float = 1.0,
    cap: float = 60.0,
    rng: Callable[[], float] | None = None,
) -> float:
    """Prefer Retry-After (with light jitter); else exponential backoff."""
    pick = rng if rng is not None else random.random
    if retry_after is not None and retry_after > 0:
        return min(cap, retry_after * (0.8 + 0.4 * pick()))
    return retry_backoff_seconds(attempt, base=base, cap=cap, rng=pick)


class FleetAgent:
    def __init__(
        self,
        *,
        controller_url: str,
        identity: AgentIdentity,
        queue: AgentQueue,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self.controller_url = controller_url.rstrip("/")
        self.identity = identity
        self.queue = queue
        self._sleep = sleep
        self._rng = rng

    @staticmethod
    def enroll_with_ca(
        *,
        controller_url: str,
        token: str,
        ca_cert: Path,
        identity_dir: Path,
        hostname: str | None = None,
        agent_id: str | None = None,
    ) -> AgentIdentity:
        """Enroll with agent-generated private key + CSR (key never leaves agent)."""
        from backuplint.fleet.certs import CertError, generate_agent_key_and_csr

        if not controller_url.lower().startswith("https://"):
            raise AgentError("controller URL must use https://")
        if not agent_id or not str(agent_id).strip():
            raise AgentError("agent_id required for CSR enrollment")
        resolved_id = str(agent_id).strip()
        identity_dir.mkdir(parents=True, exist_ok=True)
        identity_dir.chmod(0o700)
        try:
            csr_pem = generate_agent_key_and_csr(identity_dir, agent_id=resolved_id)
        except CertError as exc:
            raise AgentError(exc.message) from exc
        context = ssl.create_default_context(cafile=str(ca_cert))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        body = json.dumps(
            {
                "token": token,
                "hostname": hostname or platform.node(),
                "agent_id": resolved_id,
                "csr": csr_pem,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            controller_url.rstrip("/") + "/v1/enroll",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # nosec B310 - HTTPS-only, verified TLS
                req, context=context, timeout=30
            ) as resp:
                raw = resp.read(MAX_BODY_BYTES)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AgentError(f"enrollment failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"enrollment failed: {exc}") from exc
        data = json.loads(raw.decode("utf-8"))
        if "client_key" in data:
            raise AgentError(
                "controller returned a private key; refusing insecure enrollment"
            )
        for key in ("agent_id", "ca_cert", "client_cert"):
            if key not in data or not isinstance(data[key], str):
                raise AgentError(f"enrollment response missing {key}")
        if data["agent_id"] != resolved_id:
            raise AgentError("enrollment agent_id mismatch")
        (identity_dir / "agent_id").write_text(resolved_id + "\n", encoding="utf-8")
        (identity_dir / "ca.crt").write_text(data["ca_cert"], encoding="utf-8")
        (identity_dir / "client.crt").write_text(data["client_cert"], encoding="utf-8")
        # Private key already written locally by generate_agent_key_and_csr.
        key_path = identity_dir / "client.key"
        if not key_path.exists():
            raise AgentError("local client.key missing after CSR generation")
        key_path.chmod(0o600)
        identity_dir.chmod(0o700)
        return AgentIdentity.load(identity_dir)

    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=str(self.identity.ca_cert))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(
            certfile=str(self.identity.client_cert),
            keyfile=str(self.identity.client_key),
        )
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        if not self.controller_url.lower().startswith("https://"):
            raise AgentError("controller URL must use https://")
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.controller_url + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # nosec B310 - HTTPS-only, mTLS context
                req, context=self._ssl_context(), timeout=30
            ) as resp:
                raw = resp.read(MAX_BODY_BYTES)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in (429, 503):
                retry_after = parse_retry_after(exc.headers.get("Retry-After"))
                raise AgentTransientError(
                    f"request failed: HTTP {exc.code} {detail}",
                    status=exc.code,
                    retry_after=retry_after,
                ) from exc
            raise AgentError(f"request failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"request failed: {exc}") from exc
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise AgentError("invalid controller response")
        return data

    def _get(self, path: str) -> dict[str, object]:
        if not self.controller_url.lower().startswith("https://"):
            raise AgentError("controller URL must use https://")
        req = urllib.request.Request(self.controller_url + path, method="GET")
        try:
            with urllib.request.urlopen(  # nosec B310 - HTTPS-only, mTLS context
                req, context=self._ssl_context(), timeout=30
            ) as resp:
                raw = resp.read(MAX_BODY_BYTES)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in (429, 503):
                retry_after = parse_retry_after(exc.headers.get("Retry-After"))
                raise AgentTransientError(
                    f"request failed: HTTP {exc.code} {detail}",
                    status=exc.code,
                    retry_after=retry_after,
                ) from exc
            raise AgentError(f"request failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"request failed: {exc}") from exc
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise AgentError("invalid controller response")
        return data

    def managed_policy_dir(self) -> Path:
        path = self.identity.root / "managed-policy"
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    def fetch_desired_policy(
        self,
        *,
        current_assignment_generation: int | None = None,
        current_revision_id: str | None = None,
        current_content_sha256: str | None = None,
        max_attempts: int = 5,
    ) -> dict[str, object]:
        params: list[str] = []
        if current_assignment_generation is not None:
            params.append(
                f"current_assignment_generation={int(current_assignment_generation)}"
            )
        if current_revision_id:
            params.append(f"current_revision_id={current_revision_id}")
        if current_content_sha256:
            params.append(f"current_content_sha256={current_content_sha256}")
        path = "/v1/policy/desired"
        if params:
            path = path + "?" + "&".join(params)
        attempt = 0
        while True:
            try:
                return self._get(path)
            except AgentTransientError as exc:
                if attempt + 1 >= max_attempts:
                    raise
                self._wait_before_retry(attempt, exc.retry_after)
                attempt += 1

    def poll_and_apply_policy(self, *, max_attempts: int = 5) -> dict[str, object]:
        """Pull desired policy, apply locally, and acknowledge to controller."""
        from backuplint.fleet.capabilities import collect_local_capabilities
        from backuplint.fleet.policy_apply import ManagedPolicyDir, apply_policy_response
        from backuplint.fleet.protocol import utc_now_iso

        # Jitter so large fleets do not synchronize on the same second.
        pick = self._rng if self._rng is not None else random.random
        self._sleep(2.0 * float(pick()))
        managed = ManagedPolicyDir(self.managed_policy_dir())
        state = managed.load_state()
        current_gen = state.applied.assignment_generation if state.applied else None
        current_rev = state.applied.revision_id if state.applied else None
        current_sha = state.applied.content_sha256 if state.applied else None
        desired = self.fetch_desired_policy(
            current_assignment_generation=current_gen,
            current_revision_id=current_rev,
            current_content_sha256=current_sha,
            max_attempts=max_attempts,
        )
        applied = apply_policy_response(
            managed,
            desired,
            capabilities=collect_local_capabilities(),
            applied_at=utc_now_iso(),
        )
        if str(desired.get("status") or "") != "NO_CHANGE":
            attempt = 0
            while True:
                try:
                    self._post(
                        "/v1/policy/applied",
                        {
                            "assignment_generation": applied.assignment_generation,
                            "revision_id": applied.revision_id,
                            "content_sha256": applied.content_sha256,
                            "apply_status": applied.apply_status,
                            "drift_status": applied.drift_status,
                            "reason": applied.reason,
                            "applied_at": applied.applied_at,
                            "local_config_sha256": applied.local_config_sha256,
                        },
                    )
                    break
                except AgentTransientError as exc:
                    if attempt + 1 >= max_attempts:
                        raise
                    self._wait_before_retry(attempt, exc.retry_after)
                    attempt += 1
        return {
            "desired_status": desired.get("status"),
            "apply_status": applied.apply_status,
            "drift_status": applied.drift_status,
            "assignment_generation": applied.assignment_generation,
            "revision_id": applied.revision_id,
        }

    def _wait_before_retry(self, attempt: int, retry_after: float | None) -> None:
        delay = compute_retry_delay(attempt, retry_after, rng=self._rng)
        self._sleep(delay)

    def heartbeat(self, *, max_attempts: int = 5) -> None:
        from backuplint.fleet.capabilities import collect_local_capabilities

        payload = {
            "agent_id": self.identity.agent_id,
            "protocol_version": PROTOCOL_VERSION,
            "software_version": __version__,
            "capabilities": collect_local_capabilities(),
        }
        attempt = 0
        while True:
            try:
                self._post("/v1/heartbeat", payload)
                return
            except AgentTransientError as exc:
                if attempt + 1 >= max_attempts:
                    raise
                self._wait_before_retry(attempt, exc.retry_after)
                attempt += 1

    def submit_envelope(self, envelope: ResultEnvelope) -> None:
        self.queue.enqueue(envelope)
        remaining = self.flush()
        if any(
            item.get("submission_id") == envelope.submission_id
            for item in self.queue.peek_all()
        ):
            raise AgentError(
                "submission not acknowledged by controller; queued for retry"
            )
        _ = remaining

    def flush(self, *, max_attempts_per_item: int = 5) -> int:
        """Drain queue; return number of successful ACKs.

        Durable ACK rule: any HTTP 200 from POST /v1/results confirms the
        controller accepted the submission. The queue item is removed without
        inspecting the advisory ``created`` field (true = newly inserted,
        false = idempotent duplicate / lost-ACK confirmation).

        Transient errors (429/503) back off with Retry-After / retry_backoff_seconds
        and retry the same item. Other client errors keep the item and stop draining
        so callers can retry later — never silently drop.
        """
        acked = 0
        for item in list(self.queue.peek_all()):
            envelope = parse_envelope(item)
            attempt = 0
            while True:
                try:
                    self._post("/v1/results", envelope.to_dict())
                    break
                except AgentTransientError as exc:
                    if attempt + 1 >= max_attempts_per_item:
                        return acked
                    self._wait_before_retry(attempt, exc.retry_after)
                    attempt += 1
                except AgentError:
                    # Keep in queue for retry; stop to preserve order.
                    return acked
            self.queue.remove(envelope.submission_id)
            acked += 1
        return acked

    def run_check_and_submit(
        self,
        *,
        compose_file: Path,
        config_path: Path,
    ) -> ResultEnvelope:
        from backuplint.engine import classify_engine_exception
        from backuplint.reporting import format_audit_json, format_operational_error_json
        from backuplint.restic import ResticError

        try:
            outcome = run_audit(compose_file, config_path=config_path)
            result_obj = json.loads(
                format_audit_json(
                    outcome.findings,
                    integrity=outcome.integrity,
                    restore=outcome.restore,
                )
            )
        except ResticError as exc:
            # Fleet path: normalize operational engine failures into a controller-
            # visible ERROR envelope (distinct from coverage FAIL).
            eng_err = classify_engine_exception(exc)
            result_obj = json.loads(
                format_operational_error_json(
                    message=eng_err.message,
                    kind=eng_err.kind.value,
                    engine=eng_err.engine_id,
                )
            )
        envelope = ResultEnvelope(
            agent_id=self.identity.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version=__version__,
            platform=f"{platform.system()} {platform.machine()} {platform.release()}",
            result=result_obj,
            protocol_version=PROTOCOL_VERSION,
            run_id=new_run_id(),
        )
        try:
            self.submit_envelope(envelope)
        except AgentError:
            # submit_envelope already queued before flush; keep durable.
            pass
        return envelope
