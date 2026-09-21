"""Execute in-bank Yunxia Agents through their message/SSE protocol."""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from agentgate.domain import SpanStatus, TargetType, Trace, TraceSpan, utcnow
from agentgate.integrations.targets.inbank.diagnostics import (
    business_summary,
    mapping_shape,
    safe_url,
)
from agentgate.run.target_protocol import (
    CaseExecutionRequest,
    CaseExecutionResult,
    CaseExecutionStatus,
    TargetExecutionError,
)
from agentgate.trace.redaction import redact_value


LOGGER = logging.getLogger(__name__)
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_SUCCESS_CODES = frozenset({"0", "0000"})
_DELETE_SUCCESS_CODES = frozenset({"0", "0000", "FAIAG0000"})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Transport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> Mapping[str, Any]: ...

    def request_bytes(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> bytes: ...


class _UrlLibTransport:
    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirect())

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> Mapping[str, Any]:
        raw = self.request_bytes(
            method,
            url,
            payload=payload,
            headers=headers,
            timeout=timeout,
        )
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise TargetExecutionError(
                "protocol_error", "customer endpoint returned invalid JSON"
            ) from None
        if not isinstance(value, dict):
            raise TargetExecutionError(
                "protocol_error", "customer endpoint JSON must be an object"
            )
        LOGGER.debug(
            "inbank_http_json adapter=yunxia method=%s url=%s %s shape=%s",
            method,
            safe_url(url),
            business_summary(value),
            mapping_shape(value),
        )
        return value

    def request_bytes(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> bytes:
        body = None
        request_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=body, headers=request_headers, method=method)
        started = time.monotonic()
        LOGGER.debug(
            "inbank_http_request adapter=yunxia method=%s url=%s timeout_seconds=%.3f "
            "payload_%s header_names=%r",
            method,
            safe_url(url),
            timeout,
            mapping_shape(payload),
            sorted(request_headers),
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            LOGGER.error(
                "inbank_http_failure adapter=yunxia method=%s url=%s "
                "status_code=%s elapsed_ms=%.1f error_type=HTTPError",
                method,
                safe_url(url),
                exc.code,
                (time.monotonic() - started) * 1000,
            )
            raise _http_error(exc.code) from None
        except (TimeoutError, socket.timeout):
            LOGGER.error(
                "inbank_http_failure adapter=yunxia method=%s url=%s "
                "elapsed_ms=%.1f error_type=TimeoutError",
                method,
                safe_url(url),
                (time.monotonic() - started) * 1000,
            )
            raise TargetExecutionError("timeout", "customer endpoint timed out") from None
        except (URLError, OSError):
            LOGGER.error(
                "inbank_http_failure adapter=yunxia method=%s url=%s "
                "elapsed_ms=%.1f error_type=ConnectionError",
                method,
                safe_url(url),
                (time.monotonic() - started) * 1000,
            )
            raise TargetExecutionError(
                "unavailable", "customer endpoint is unavailable"
            ) from None
        LOGGER.debug(
            "inbank_http_response adapter=yunxia method=%s url=%s "
            "status_code=%s response_bytes=%d elapsed_ms=%.1f",
            method,
            safe_url(url),
            getattr(response, "status", None),
            len(raw),
            (time.monotonic() - started) * 1000,
        )
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise TargetExecutionError(
                "protocol_error", "customer response exceeds 10 MiB"
            )
        return raw


@dataclass(frozen=True, slots=True)
class YunxiaSettings:
    """Non-secret endpoints and safety settings for Yunxia execution."""

    create_base_url: str
    health_base_url: str
    pod_api_base_url: str
    delete_base_url: str
    agent_namespace: str
    test_customer_prefix: str
    request_timeout_seconds: float = 180.0
    health_wait_seconds: float = 300.0
    health_poll_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        for field_name in (
            "create_base_url",
            "health_base_url",
            "pod_api_base_url",
            "delete_base_url",
        ):
            object.__setattr__(
                self,
                field_name,
                _validated_base_url(getattr(self, field_name), field_name),
            )
        for field_name in ("agent_namespace", "test_customer_prefix"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be nonblank")
        for field_name in (
            "request_timeout_seconds",
            "health_wait_seconds",
            "health_poll_interval_seconds",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")


@dataclass(frozen=True, slots=True)
class _YunxiaChat:
    output: str
    intent_code: str | None
    slots: Mapping[str, Any]
    workflow_calls: tuple[Any, ...]


class InbankYunxiaTargetAdapter:
    """Own one lazily created Yunxia Pod for one EvaluationRun."""

    adapter_type = "inbank_yunxia"
    adapter_version = "1"

    def __init__(
        self,
        settings: YunxiaSettings,
        *,
        transport: _Transport | None = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.settings = settings
        self._transport = transport or _UrlLibTransport()
        self._sleep = sleep
        self._monotonic = monotonic
        self._run_id: str | None = None
        self._run_customer_suffix: str | None = None
        self._agent_name: str | None = None
        self._pod_name: str | None = None
        self._statuses: dict[str, CaseExecutionStatus] = {}
        self._results: dict[str, CaseExecutionResult] = {}
        self._pod_lifecycle = {
            "create": "not_attempted",
            "health": "not_attempted",
            "delete": "not_attempted",
        }
        self._cleanup_error_type: str | None = None
        self._closed = False

    @property
    def pod_lifecycle(self) -> dict[str, str | None]:
        """Return a secret-free snapshot of Pod lifecycle outcomes."""

        return {
            **self._pod_lifecycle,
            "pod_name": self._pod_name,
            "cleanup_error_type": self._cleanup_error_type,
        }

    def start(self, request: CaseExecutionRequest) -> str:
        self._validate_request(request)
        handle = request.execution_id
        if handle in self._statuses:
            raise TargetExecutionError("invalid_request", "duplicate execution ID")
        if self._closed:
            raise TargetExecutionError("invalid_request", "adapter is closed")
        self._statuses[handle] = CaseExecutionStatus.RUNNING
        started = time.monotonic()
        LOGGER.info(
            "inbank_case_start adapter=yunxia run_id=%r case_id=%r "
            "execution_id=%r turn_count=%d timeout_seconds=%.3f",
            request.run_id,
            request.case.id,
            handle,
            len(request.case.turns),
            request.timeout_seconds,
        )
        try:
            self._ensure_pod(request)
            result = self._execute_case(request)
        except TargetExecutionError as exc:
            self._statuses[handle] = CaseExecutionStatus.FAILED
            LOGGER.error(
                "inbank_case_failed adapter=yunxia run_id=%r case_id=%r "
                "execution_id=%r elapsed_ms=%.1f error_code=%s",
                request.run_id,
                request.case.id,
                handle,
                (time.monotonic() - started) * 1000,
                exc.code,
            )
            raise
        except Exception as exc:
            self._statuses[handle] = CaseExecutionStatus.FAILED
            LOGGER.exception(
                "inbank_case_failed adapter=yunxia run_id=%r case_id=%r "
                "execution_id=%r elapsed_ms=%.1f error_type=%s",
                request.run_id,
                request.case.id,
                handle,
                (time.monotonic() - started) * 1000,
                type(exc).__name__,
            )
            raise TargetExecutionError(
                "protocol_error",
                f"Yunxia execution failed ({type(exc).__name__})",
            ) from None
        self._results[handle] = result
        self._statuses[handle] = CaseExecutionStatus.COMPLETED
        LOGGER.info(
            "inbank_case_complete adapter=yunxia run_id=%r case_id=%r "
            "execution_id=%r elapsed_ms=%.1f",
            request.run_id,
            request.case.id,
            handle,
            (time.monotonic() - started) * 1000,
        )
        return handle

    def get_status(self, handle: str) -> CaseExecutionStatus:
        try:
            return self._statuses[handle]
        except KeyError:
            raise TargetExecutionError("invalid_request", "unknown execution") from None

    def wait(self, handle: str, timeout_seconds: float) -> CaseExecutionResult:
        if timeout_seconds <= 0:
            raise TargetExecutionError("invalid_request", "timeout must be positive")
        if self.get_status(handle) is not CaseExecutionStatus.COMPLETED:
            raise TargetExecutionError("protocol_error", "execution is not complete")
        return self._results[handle]

    def cancel(self, handle: str) -> None:
        status = self.get_status(handle)
        if status in {
            CaseExecutionStatus.PENDING,
            CaseExecutionStatus.RUNNING,
        }:
            self._statuses[handle] = CaseExecutionStatus.CANCELLED

    def close(self) -> None:
        """Best-effort, idempotent cleanup using the Yunxia manager API."""

        if self._closed:
            return
        self._closed = True
        agent_name = self._agent_name
        self._agent_name = None
        if agent_name is None:
            return
        self._pod_lifecycle["delete"] = "started"
        try:
            response = self._transport.request_json(
                "POST",
                _url(
                    self.settings.delete_base_url,
                    "/agent-api/agent-manager/chatabc/delete_agent",
                ),
                payload={
                    "appId": "",
                    "trCode": "",
                    "trVersion": "",
                    "timestamp": 1,
                    "requestId": "",
                    "data": {
                        "agent_name": agent_name,
                        "agent_namespace": self.settings.agent_namespace,
                    },
                },
                timeout=self.settings.request_timeout_seconds,
            )
            _require_delete_success(response)
            self._pod_lifecycle["delete"] = "succeeded"
            self._log_pod_lifecycle("delete", "succeeded")
        except Exception as exc:
            self._pod_lifecycle["delete"] = "failed"
            self._cleanup_error_type = type(exc).__name__
            LOGGER.warning(
                "inbank_pod_lifecycle adapter=yunxia run_id=%r "
                "pod_name=%r phase=delete status=failed error_type=%s",
                self._run_id,
                self._pod_name,
                type(exc).__name__,
            )

    def _ensure_pod(self, request: CaseExecutionRequest) -> None:
        if self._run_id is not None and self._run_id != request.run_id:
            raise TargetExecutionError(
                "invalid_request", "adapter already belongs to another Run"
            )
        if self._agent_name is not None:
            return
        self._run_id = request.run_id
        self._run_customer_suffix = uuid4().hex
        customer_task_id = request.target.invocation_config.get(
            "customer_task_id"
        )
        if not isinstance(customer_task_id, str) or not customer_task_id.strip():
            raise TargetExecutionError(
                "invalid_request", "target customer_task_id must be nonblank"
            )
        version = request.target.invocation_config.get(
            "agent_version", request.target.ref.external_version_id
        )
        if not isinstance(version, str) or not version.strip():
            raise TargetExecutionError(
                "invalid_request", "target agent_version must be nonblank"
            )
        self._pod_lifecycle["create"] = "started"
        try:
            response = self._transport.request_json(
                "POST",
                _url(
                    self.settings.create_base_url,
                    "/web/agent_endpoint/createAgent",
                    {"taskId": customer_task_id},
                ),
                payload={
                    "agentId": request.target.ref.external_target_id,
                    "agentVersion": version,
                },
                timeout=min(
                    request.timeout_seconds, self.settings.request_timeout_seconds
                ),
            )
            if str(response.get("code", "")) not in _SUCCESS_CODES:
                raise TargetExecutionError("rejected", "create Agent Pod was rejected")
            data = response.get("data")
            agent_name = data.get("agentName") if isinstance(data, Mapping) else None
            if not isinstance(agent_name, str) or not agent_name.strip():
                raise TargetExecutionError(
                    "protocol_error", "create Agent Pod returned no agentName"
                )
        except Exception:
            self._pod_lifecycle["create"] = "failed"
            self._log_pod_lifecycle("create", "failed")
            raise
        self._agent_name = agent_name
        self._pod_name = agent_name
        self._pod_lifecycle["create"] = "succeeded"
        self._log_pod_lifecycle("create", "succeeded")
        self._pod_lifecycle["health"] = "started"
        try:
            self._wait_until_healthy(request.timeout_seconds)
        except Exception:
            self._pod_lifecycle["health"] = "failed"
            self._log_pod_lifecycle("health", "failed")
            raise
        self._pod_lifecycle["health"] = "succeeded"
        self._log_pod_lifecycle("health", "succeeded")

    def _wait_until_healthy(self, case_timeout_seconds: float) -> None:
        assert self._agent_name is not None
        wait_seconds = min(case_timeout_seconds, self.settings.health_wait_seconds)
        deadline = self._monotonic() + wait_seconds
        url = _url(
            self.settings.health_base_url,
            f"/agent-api/{quote(self._agent_name, safe='')}/health",
        )
        last_error: TargetExecutionError | None = None
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._transport.request_json(
                    "GET",
                    url,
                    headers=self._pod_headers(),
                    timeout=min(
                        self.settings.request_timeout_seconds,
                        max(0.001, deadline - self._monotonic()),
                    ),
                )
                if response.get("status") == "ok":
                    LOGGER.info(
                        "inbank_health_ready adapter=yunxia run_id=%r "
                        "pod_name=%r attempt=%d",
                        self._run_id,
                        self._pod_name,
                        attempt,
                    )
                    return
                LOGGER.debug(
                    "inbank_health_pending adapter=yunxia run_id=%r "
                    "pod_name=%r attempt=%d",
                    self._run_id,
                    self._pod_name,
                    attempt,
                )
                last_error = None
            except TargetExecutionError as exc:
                if exc.code == "unauthorized":
                    raise
                last_error = exc
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                detail = " after endpoint failures" if last_error else ""
                raise TargetExecutionError(
                    "timeout", f"Yunxia Pod health check timed out{detail}"
                )
            self._sleep(min(self.settings.health_poll_interval_seconds, remaining))

    def _execute_case(self, request: CaseExecutionRequest) -> CaseExecutionResult:
        assert self._agent_name is not None
        assert self._run_customer_suffix is not None
        initial = request.case.initial_state.to_dict()
        session_id = str(uuid4())
        customer_id = (
            initial.get("customer_id", self.settings.test_customer_prefix)
            + "-"
            + self._run_customer_suffix
        )
        config_variables = initial.get("config_variables", [])
        input_field = _input_field(request)
        trace_id = request.traceparent.split("-")[1]
        parent_span_id = request.traceparent.split("-")[2]
        spans: list[TraceSpan] = []
        outcomes: dict[str, dict[str, Any]] = {}
        final_output: dict[str, Any] = {}
        path = (
            f"/agent-api/{quote(self._agent_name, safe='')}/api/v1/message"
        )

        for sequence, turn in enumerate(request.case.turns):
            turn_input = turn.input.to_dict()
            request_id = str(uuid4())
            payload = {
                "sessionId": session_id,
                "custID": customer_id,
                "txt": turn_input[input_field],
                "executionMode": "execute",
                "stream": True,
                "debugTrace": False,
                "config_variables": config_variables,
                "appHistory": turn_input.get("app_history", []),
            }
            started_at = utcnow()
            LOGGER.info(
                "inbank_turn_start adapter=yunxia run_id=%r case_id=%r "
                "turn_id=%r sequence=%d request_id=%r session_id=%r input_chars=%d",
                request.run_id,
                request.case.id,
                turn.id,
                sequence,
                request_id,
                session_id,
                len(turn_input[input_field]),
            )
            raw = self._transport.request_bytes(
                "POST",
                _url(self.settings.pod_api_base_url, path),
                payload=payload,
                headers=self._pod_headers(
                    request.traceparent,
                    accept="text/event-stream",
                    request_id=request_id,
                ),
                timeout=min(
                    request.timeout_seconds, self.settings.request_timeout_seconds
                ),
            )
            chat = _parse_yunxia_sse(raw, request_id, session_id)
            ended_at = utcnow()
            final_output = {
                "output": chat.output,
                "intent_code": chat.intent_code,
                "slots": dict(chat.slots),
                "workflow_calls": list(chat.workflow_calls),
            }
            LOGGER.info(
                "inbank_turn_complete adapter=yunxia run_id=%r case_id=%r "
                "turn_id=%r sequence=%d request_id=%r response_bytes=%d "
                "output_chars=%d workflow_call_count=%d elapsed_ms=%.1f",
                request.run_id,
                request.case.id,
                turn.id,
                sequence,
                request_id,
                len(raw),
                len(chat.output),
                len(chat.workflow_calls),
                (ended_at - started_at).total_seconds() * 1000,
            )
            outcomes[turn.id] = {
                "input": turn_input,
                "output": final_output,
                "state": {},
            }
            spans.append(
                TraceSpan(
                    trace_id=trace_id,
                    span_id=uuid4().hex[:16],
                    parent_span_id=parent_span_id,
                    name="inbank.yunxia.turn",
                    operation_type="turn",
                    sequence=sequence,
                    started_at=started_at,
                    ended_at=ended_at,
                    status=SpanStatus.OK,
                    attributes={
                        "agentgate.turn.id": turn.id,
                        "inbank.session_id": session_id,
                        "inbank.request_id": request_id,
                        "inbank.customer_id": customer_id,
                        "inbank.intent_code": chat.intent_code,
                        "inbank.slots": redact_value(chat.slots),
                        "inbank.workflow_calls": redact_value(chat.workflow_calls),
                        "inbank.evidence_mode": "output_only",
                        "inbank.pod_created": True,
                        "inbank.pod_healthy": True,
                    },
                )
            )

        trace = Trace(
            trace_id=trace_id,
            run_id=request.run_id,
            case_id=request.case.id,
            spans=tuple(spans),
            turn_outcomes=outcomes,
            final_output=final_output,
            final_state={},
        )
        return CaseExecutionResult(request.execution_id, trace_id, trace)

    def _log_pod_lifecycle(self, phase: str, status: str) -> None:
        LOGGER.info(
            "inbank_pod_lifecycle adapter=yunxia run_id=%r pod_name=%r "
            "phase=%s status=%s",
            self._run_id,
            self._pod_name,
            phase,
            status,
        )

    def _pod_headers(
        self,
        traceparent: str | None = None,
        *,
        accept: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if traceparent:
            headers["traceparent"] = traceparent
        if accept:
            headers["Accept"] = accept
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _validate_request(self, request: CaseExecutionRequest) -> None:
        target = request.target
        if target.ref.target_type is not TargetType.AGENT:
            raise TargetExecutionError(
                "invalid_request", "Yunxia target must be an Agent"
            )
        if target.adapter_type != self.adapter_type:
            raise TargetExecutionError("invalid_request", "adapter_type does not match")
        if target.adapter_version != self.adapter_version:
            raise TargetExecutionError(
                "invalid_request", "adapter_version does not match"
            )
        initial = request.case.initial_state.to_dict()
        if set(initial) - {"customer_id", "config_variables"}:
            raise TargetExecutionError(
                "invalid_request", "Case initial_state has unsupported fields"
            )
        customer_id = initial.get("customer_id", self.settings.test_customer_prefix)
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise TargetExecutionError(
                "invalid_request", "initial_state customer_id must be nonblank text"
            )
        if not customer_id.startswith(self.settings.test_customer_prefix):
            raise TargetExecutionError(
                "invalid_request", "customer_id must use the approved test prefix"
            )
        if not isinstance(initial.get("config_variables", []), list):
            raise TargetExecutionError(
                "invalid_request", "initial_state config_variables must be an array"
            )
        input_field = _input_field(request)
        for turn in request.case.turns:
            value = turn.input.to_dict()
            if (
                set(value) - {input_field, "app_history", "files"}
                or input_field not in value
            ):
                raise TargetExecutionError(
                    "invalid_request",
                    "Yunxia CaseTurn input supports only "
                    f"{input_field}, app_history and files",
                )
            if (
                not isinstance(value[input_field], str)
                or not value[input_field].strip()
            ):
                raise TargetExecutionError(
                    "invalid_request",
                    f"CaseTurn {input_field} must be nonblank text",
                )
            if not isinstance(value.get("app_history", []), list):
                raise TargetExecutionError(
                    "invalid_request", "CaseTurn app_history must be an array"
                )
            files = value.get("files", [])
            if not isinstance(files, list):
                raise TargetExecutionError(
                    "invalid_request", "CaseTurn files must be an array"
                )
            if files:
                raise TargetExecutionError(
                    "invalid_request", "non-empty files are not supported"
                )


def _input_field(request: CaseExecutionRequest) -> str:
    value = request.target.invocation_config.get("input_field", "txt")
    if not isinstance(value, str) or not value.strip():
        raise TargetExecutionError(
            "invalid_request", "target input_field must be nonblank text"
        )
    return value


def resolve_yunxia_trace(
    request: CaseExecutionRequest, result: CaseExecutionResult
) -> Trace:
    """Return the output-only AgentGate execution record for evaluation."""

    if result.execution_id != request.execution_id:
        raise TargetExecutionError(
            "protocol_error", "Yunxia result execution does not match request"
        )
    if result.inline_trace is None:
        raise TargetExecutionError(
            "protocol_error", "Yunxia execution returned no inline Trace"
        )
    return result.inline_trace


def load_yunxia_settings(
    environ: Mapping[str, str] | None = None,
) -> YunxiaSettings:
    """Load Yunxia endpoint configuration without reading credentials."""

    values = os.environ if environ is None else environ
    required = {
        "create_base_url": "AGENTGATE_INBANK_CREATE_BASE_URL",
        "health_base_url": "AGENTGATE_INBANK_YUNXIA_HEALTH_BASE_URL",
        "pod_api_base_url": "AGENTGATE_INBANK_YUNXIA_POD_API_BASE_URL",
        "delete_base_url": "AGENTGATE_INBANK_YUNXIA_DELETE_BASE_URL",
        "agent_namespace": "AGENTGATE_INBANK_YUNXIA_AGENT_NAMESPACE",
        "test_customer_prefix": "AGENTGATE_INBANK_YUNXIA_TEST_CUSTOMER_PREFIX",
    }
    missing = [env_name for env_name in required.values() if not values.get(env_name)]
    if missing:
        raise ValueError(
            "incomplete Yunxia environment: " + ", ".join(sorted(missing))
        )
    return YunxiaSettings(
        **{field: values[name] for field, name in required.items()},
        request_timeout_seconds=_positive_float(
            values, "AGENTGATE_INBANK_REQUEST_TIMEOUT_SECONDS", 180.0
        ),
        health_wait_seconds=_positive_float(
            values, "AGENTGATE_INBANK_HEALTH_WAIT_SECONDS", 300.0
        ),
        health_poll_interval_seconds=_positive_float(
            values, "AGENTGATE_INBANK_HEALTH_POLL_INTERVAL_SECONDS", 5.0
        ),
    )


def _parse_yunxia_sse(
    raw: bytes, request_id: str, session_id: str
) -> _YunxiaChat:
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        raise TargetExecutionError(
            "protocol_error", "Yunxia stream is not UTF-8"
        ) from None
    frames: list[tuple[str, Any]] = []
    event_name = ""
    data_lines: list[str] = []

    def consume() -> None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = ""
            return
        raw_data = "\n".join(data_lines)
        try:
            payload = json.loads(raw_data)
        except ValueError:
            payload = raw_data
        name = event_name
        if not name:
            if not isinstance(payload, dict):
                raise TargetExecutionError(
                    "protocol_error", "Yunxia SSE envelope must be an object"
                )
            name = payload.get("event")
            payload = payload.get("data")
        if not isinstance(name, str) or not name.strip():
            raise TargetExecutionError(
                "protocol_error", "Yunxia SSE event name is missing"
            )
        frames.append((name.strip().lower(), payload))
        event_name, data_lines = "", []

    for line in lines:
        line = line.rstrip("\r")
        if not line:
            consume()
        elif line.startswith(":"):
            continue
        else:
            field, separator, value = line.partition(":")
            value = value.removeprefix(" ")
            if separator and field == "event":
                event_name = value
            elif separator and field == "data":
                data_lines.append(value)
    consume()

    starts = [payload for name, payload in frames if name == "start"]
    messages = [payload for name, payload in frames if name == "message"]
    done_count = sum(1 for name, _ in frames if name == "done")
    unknown = {
        name for name, _ in frames if name not in {"start", "progress", "message", "trace", "done", "error", "failed"}
    }
    terminal_seen = False
    for name, _ in frames:
        if terminal_seen:
            raise TargetExecutionError(
                "protocol_error", "Yunxia SSE data followed the terminal event"
            )
        terminal_seen = name == "done"
    if unknown:
        raise TargetExecutionError("protocol_error", "unknown Yunxia SSE event")
    if any(name in {"error", "failed"} for name, _ in frames):
        raise TargetExecutionError("rejected", "Yunxia returned an error event")
    if len(starts) != 1 or not isinstance(starts[0], Mapping):
        raise TargetExecutionError(
            "protocol_error", "Yunxia requires exactly one start event"
        )
    returned_request_id = starts[0].get("request_id", starts[0].get("requestId"))
    returned_session_id = starts[0].get("session_id", starts[0].get("sessionId"))
    if returned_request_id != request_id:
        raise TargetExecutionError("protocol_error", "Yunxia request ID mismatch")
    if returned_session_id is not None and returned_session_id != session_id:
        raise TargetExecutionError("protocol_error", "Yunxia session ID mismatch")
    if done_count != 1 or not messages:
        raise TargetExecutionError("protocol_error", "incomplete Yunxia SSE response")
    message = messages[-1]
    if not isinstance(message, Mapping) or message.get("status") != "completed":
        raise TargetExecutionError(
            "protocol_error", "Yunxia message did not complete"
        )
    output = message.get("output")
    if not isinstance(output, str) or not output.strip():
        raise TargetExecutionError(
            "protocol_error", "completed Yunxia message has no output"
        )
    intent_code = message.get("intent_code")
    if intent_code is not None and not isinstance(intent_code, str):
        raise TargetExecutionError(
            "protocol_error", "Yunxia intent_code must be text"
        )
    slots = message.get("slots", {})
    workflow_calls = message.get("workflow_calls", [])
    if not isinstance(slots, Mapping) or not isinstance(workflow_calls, list):
        raise TargetExecutionError(
            "protocol_error", "Yunxia message metadata has invalid types"
        )
    return _YunxiaChat(
        output=output,
        intent_code=intent_code,
        slots=slots,
        workflow_calls=tuple(workflow_calls),
    )


def _url(
    base: str, path: str, query: Mapping[str, str] | None = None
) -> str:
    value = base.rstrip("/") + "/" + path.lstrip("/")
    return value + ("?" + urlencode(query) if query else "")


def _validated_base_url(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be nonblank")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} must be an HTTP(S) base URL without credentials")
    return value.rstrip("/")


def _http_error(status_code: int) -> TargetExecutionError:
    if status_code in {401, 403}:
        code = "unauthorized"
    elif status_code == 404:
        code = "target_not_found"
    elif status_code == 429:
        code = "rate_limited"
    elif status_code in {408, 504}:
        code = "timeout"
    elif status_code >= 500:
        code = "unavailable"
    else:
        code = "rejected"
    return TargetExecutionError(code, f"customer endpoint returned HTTP {status_code}")


def _require_delete_success(response: Mapping[str, Any]) -> None:
    """Honor the customer's HTTP contract and reject explicit business failures."""

    for field_name in ("resCode", "code"):
        if field_name in response and str(response[field_name]) not in _DELETE_SUCCESS_CODES:
            raise TargetExecutionError("rejected", "delete Yunxia Pod was rejected")


def _positive_float(
    values: Mapping[str, str], name: str, default: float
) -> float:
    raw = values.get(name)
    try:
        result = default if raw is None else float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive number") from None
    if result <= 0:
        raise ValueError(f"{name} must be a positive number")
    return result


__all__ = [
    "InbankYunxiaTargetAdapter",
    "YunxiaSettings",
    "load_yunxia_settings",
    "resolve_yunxia_trace",
]
