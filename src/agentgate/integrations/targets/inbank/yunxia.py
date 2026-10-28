"""Execute in-bank Yunxia Agents through their message/SSE protocol."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Mapping
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from uuid import uuid4

import requests

from agentgate.domain import CaseTurn, TargetType, Trace, TraceSpan, utcnow
from agentgate.integrations.observability.trace_server import (
    InbankTraceSettings,
    TraceServerClient,
    inbank_trace_snapshot,
    load_inbank_trace_settings,
    prepare_inbank_trace_events,
)
from agentgate.integrations.targets.inbank.diagnostics import (
    business_summary,
    mapping_shape,
    safe_url,
)
from agentgate.integrations.targets.inbank.evidence import (
    normalize_turn_evidence,
    output_only_turn_span,
    project_yunxia_intent_selection,
)
from agentgate.run.target_protocol import (
    CaseExecutionPending,
    CaseExecutionRequest,
    CaseExecutionResult,
    CaseExecutionStatus,
    TargetExecutionError,
)

LOGGER = logging.getLogger(__name__)
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_DELETE_SUCCESS_CODES = frozenset({"0", "0000", "FAIAG0000"})


# Adapted from customer yunxia.py; AgentGate owns scheduling and reports.
def _request_failure(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, TargetExecutionError):
        code = exc.code
    elif isinstance(exc, requests.exceptions.Timeout):
        code = "timeout"
    elif isinstance(exc, requests.exceptions.HTTPError):
        code = _http_error(exc.response.status_code).code
    elif isinstance(exc, requests.exceptions.ConnectionError):
        code = "unavailable"
    else:
        code = "protocol_error"
    return {
        "success": False,
        "error_code": code,
        "error": exc.message
        if isinstance(exc, TargetExecutionError)
        else f"customer request failed ({type(exc).__name__})",
    }


def create_agent_pod(
    task_id: str,
    agent_id: str,
    agent_version: str,
    cfg: Mapping,
    is_vip_eval: bool = False,
    timeout: float = 60,
    *,
    http=None,
    sleep=time.sleep,
    branch_id: str | None = None,
) -> dict:
    http = http or requests
    query = {"taskId": task_id}
    if is_vip_eval:
        query["evalType"] = "formal"
    url = _url(cfg["CCE_CREATE_BASE_URL"], "/web/agent_endpoint/createAgent", query)
    body = {"agentId": agent_id, "agentVersion": agent_version}
    if branch_id is not None and str(branch_id).strip() not in ("", "null", "None"):
        body["branchId"] = str(branch_id).strip()

    def _attempt() -> dict:
        try:
            with http.post(
                url, json=body, timeout=timeout, headers={"Content-Type": "application/json"}
            ) as resp:
                resp.raise_for_status()
                data = resp.json()
            LOGGER.debug(
                "inbank_http_json adapter=yunxia url=%s %s shape=%s",
                safe_url(url),
                business_summary(data),
                mapping_shape(data),
            )
            if str(data.get("code", "")) not in ("0", "0000"):
                code = str(data.get("code", ""))
                return {
                    "success": False,
                    "error_code": "rejected",
                    "error": f"code={code}, message={data.get('message', '未知错误')}",
                    "response": data,
                }
            agent_name = data.get("data", {}).get("agentName", "")
            if not agent_name:
                return {
                    "success": False,
                    "error_code": "protocol_error",
                    "error": "create Agent Pod returned no agentName",
                    "response": data,
                }
            return {"success": True, "agent_name": agent_name, "response": data}
        except Exception as exc:  # noqa: BLE001 - retain customer retry/cleanup boundary.
            return _request_failure(exc)

    for attempt in range(1, 4):
        result = _attempt()
        if result["success"]:
            return result
        if attempt < 3:
            LOGGER.warning(
                "inbank_lifecycle_retry adapter=yunxia phase=create "
                "attempt=%d max_attempts=3 retry_delay_seconds=10",
                attempt,
            )
            sleep(10)
    return result


def wait_for_pod_ready(
    agent_name: str,
    cfg: Mapping,
    interval: float = 5,
    max_wait: float = 300,
    *,
    http=None,
    sleep=time.sleep,
    monotonic=time.monotonic,
    request_timeout: float = 10,
) -> bool:
    http = http or requests
    url = _url(cfg["HEALTH_CHECK_BASE_URL"], f"/agent-api/{quote(agent_name, safe='')}/health")
    start = monotonic()
    while monotonic() - start < max_wait:
        try:
            with http.get(url, timeout=request_timeout) as resp:
                resp.raise_for_status()
                data = resp.json()
            if isinstance(data, dict) and data.get("status") == "ok":
                return True
        except Exception as exc:  # noqa: BLE001 - retain customer retry/cleanup boundary.
            LOGGER.warning("inbank_health_pending adapter=yunxia error_type=%s", type(exc).__name__)
        sleep(min(interval, max(0, max_wait - (monotonic() - start))))
    return False


def delete_agent_pod(agent_name: str, cfg: Mapping, timeout: float = 60, *, http=None) -> dict:
    http = http or requests
    url = _url(cfg["DELETE_POD_BASE_URL"], "/agent-api/agent-manager/chatabc/delete_agent")
    body = {
        "appId": "",
        "trCode": "",
        "trVersion": "",
        "timestamp": 1,
        "requestId": "",
        "data": {"agent_name": agent_name, "agent_namespace": cfg["AGENT_NAMESPACE"]},
    }
    try:
        with http.post(
            url, json=body, timeout=timeout, headers={"Content-Type": "application/json"}
        ) as resp:
            resp.raise_for_status()
            data = resp.json()
        _require_delete_success(data)
        return {"success": True, "response": data}
    except Exception as exc:  # noqa: BLE001 - retain customer retry/cleanup boundary.
        return _request_failure(exc)


def parse_sse_stream(response, deadline: float | None = None) -> list:
    events = []
    current_event = None
    current_data_lines = []
    size = 0
    for raw_line in response.iter_lines(decode_unicode=True):
        if deadline is not None and time.time() > deadline:
            raise requests.exceptions.Timeout("SSE exceeded deadline")
        if raw_line is None:
            continue
        size += len(raw_line.encode("utf-8"))
        if size > _MAX_RESPONSE_BYTES:
            raise TargetExecutionError("protocol_error", "customer response exceeds 10 MiB")
        line = raw_line.strip()
        if not line:
            if current_event is not None and current_data_lines:
                events.append({"event": current_event, "data": "\n".join(current_data_lines)})
            current_event, current_data_lines = None, []
            continue
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            current_data_lines.append(line[len("data:") :].strip())
        else:
            current_data_lines.append(line)
    if current_event is not None and current_data_lines:
        events.append({"event": current_event, "data": "\n".join(current_data_lines)})
    return events


def safe_json_loads(value):
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return str(value)


def try_parse_json(data_str: str) -> tuple:
    try:
        return True, json.loads(data_str)
    except (TypeError, ValueError):
        return False, "invalid JSON"


def build_request_payload(row: Mapping, cfg: Mapping) -> dict:
    """Customer row mapping, fed by Case/Turn instead of a pandas Series."""
    txt = str(row.get("user_input", "")).strip()
    session_id = str(row.get("questionId", "")).strip()
    raw_history = row.get("currentDisplay_1")
    if isinstance(raw_history, str):
        raw_history = raw_history.strip()
    parsed = safe_json_loads(raw_history) if raw_history is not None and raw_history != "" else None
    app_history = parsed if isinstance(parsed, list) else [] if parsed is None else [parsed]
    run_uuid = str(cfg.get("_RUN_CUST_UUID") or cfg.get("EVAL_CUST_ID", ""))
    customer_id = str(row.get("custId") or "").strip()
    return {
        "sessionId": session_id,
        "custID": (customer_id or session_id) + run_uuid,
        "txt": txt,
        "executionMode": "execute",
        "stream": True,
        "debugTrace": True,
        "appHistory": app_history,
        "config_variables": row.get("config_variables", []),
    }


def call_api_via_pod(
    payload: dict,
    agent_name: str,
    cfg: Mapping,
    timeout: float | None = None,
    *,
    http=None,
    headers=None,
) -> tuple:
    http = http or requests
    if timeout is None:
        timeout = cfg.get("EVAL_REQUEST_TIMEOUT", 180)
    url = _url(cfg["POD_API_BASE_URL"], f"/agent-api/{quote(agent_name, safe='')}/api/v1/message")
    start = time.time()
    try:
        with http.post(
            url,
            json=payload,
            stream=True,
            timeout=timeout,
            headers={"Content-Type": "application/json", **(headers or {})},
        ) as resp:
            resp.raise_for_status()
            resp.encoding = "utf-8-sig"
            events = parse_sse_stream(resp, deadline=start + timeout)
        return events, "", time.time() - start
    except Exception as exc:  # noqa: BLE001 - retain customer retry/cleanup boundary.
        failure = _request_failure(exc)
        return [], f"{failure['error_code']}: {failure['error']}", time.time() - start


def process_single_row(
    row: Mapping,
    agent_name: str,
    cfg: Mapping,
    *,
    timeout: float | None = None,
    http=None,
    headers=None,
) -> dict:
    """Customer message/error/trace handling without Excel or its scoring result model."""
    payload = build_request_payload(row, cfg)
    events, error, duration = call_api_via_pod(
        payload, agent_name, cfg, timeout, http=http, headers=headers
    )
    message = {}
    message_data = ""
    unstructured_output = ""
    trace_payloads = []
    # Customer yunxia.py ignores done and does not infer errors from message fields.
    for ev in events:
        event_type = ev.get("event", "").strip().lower()
        data_str = ev.get("data", "")
        if event_type == "message" and not error:
            message_data = data_str
            ok, parsed = try_parse_json(data_str)
            message = parsed if ok and isinstance(parsed, dict) else {}
            if ok and isinstance(parsed, dict):
                unstructured_output = ""
            elif ok and isinstance(parsed, str):
                unstructured_output = parsed
            else:
                unstructured_output = data_str
        elif event_type in {"error", "failed"}:
            ok, parsed = try_parse_json(data_str)
            detail = (
                parsed.get("message") or parsed.get("errorCode")
                if ok and isinstance(parsed, dict)
                else None
            )
            failure_detail = detail or data_str or "customer chat returned an error event"
            error = f"rejected: {str(failure_detail)[:500]}"
        elif event_type == "trace":
            ok, parsed = try_parse_json(data_str)
            trace_payloads.append(parsed if ok else None)
    output = message.get("message")
    if not isinstance(output, str) or not output.strip():
        output = message.get("output", "")
    if not isinstance(output, str) or not output.strip():
        output = unstructured_output
    return {
        "output": output if isinstance(output, str) else "",
        "message": message,
        "message_data": message_data,
        "events": events,
        "trace_payloads": tuple(trace_payloads),
        "error": error,
        "duration": duration,
    }


@dataclass(frozen=True, slots=True)
class YunxiaSettings:
    """Non-secret endpoints and safety settings for Yunxia execution."""

    create_base_url: str
    health_base_url: str
    pod_api_base_url: str
    delete_base_url: str
    agent_namespace: str
    request_timeout_seconds: float = 180.0
    health_wait_seconds: float = 300.0
    health_poll_interval_seconds: float = 5.0
    create_timeout_seconds: float = 60.0
    delete_timeout_seconds: float = 60.0
    health_request_timeout_seconds: float = 10.0
    debug_sse_failures: bool = False
    total_timeout_seconds: float = 0.0
    trace_settings: InbankTraceSettings = field(default_factory=InbankTraceSettings)

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
        if not isinstance(self.agent_namespace, str) or not self.agent_namespace.strip():
            raise ValueError("agent_namespace must be nonblank")
        for field_name in (
            "request_timeout_seconds",
            "health_wait_seconds",
            "health_poll_interval_seconds",
            "create_timeout_seconds", "delete_timeout_seconds", "health_request_timeout_seconds",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if not isinstance(self.trace_settings, InbankTraceSettings):
            raise TypeError("trace_settings must be InbankTraceSettings")


class InbankYunxiaTargetAdapter:
    """Own one lazily created Yunxia Pod for one EvaluationRun."""

    adapter_type = "inbank_yunxia"
    adapter_version = "1"

    def __init__(
        self,
        settings: YunxiaSettings,
        *,
        http: Any = None,
        trace_client: TraceServerClient,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.settings = settings
        self._http = http or requests
        self._cfg = {
            "CCE_CREATE_BASE_URL": settings.create_base_url,
            "HEALTH_CHECK_BASE_URL": settings.health_base_url,
            "POD_API_BASE_URL": settings.pod_api_base_url,
            "DELETE_POD_BASE_URL": settings.delete_base_url,
            "AGENT_NAMESPACE": settings.agent_namespace,
        }
        if trace_client is None:
            raise ValueError("Yunxia requires an explicit Trace Server client")
        self._trace_server = trace_client
        self._sleep = sleep
        self._monotonic = monotonic
        self._run_id: str | None = None
        self._run_customer_suffix: str | None = None
        self._agent_name: str | None = None
        self._pod_name: str | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._deadline: float | None = None
        self._futures: dict[str, Future[CaseExecutionResult]] = {}
        self._cancelled: dict[str, Event] = {}
        self._session_tails: dict[str, Future[CaseExecutionResult]] = {}
        self._preparation_failures: set[str] = set()
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
        if self._closed:
            raise TargetExecutionError("invalid_request", "adapter is closed")
        if request.execution_id in self._preparation_failures:
            raise TargetExecutionError("invalid_request", "duplicate execution ID")
        try:
            self._ensure_pod(request)
        except TargetExecutionError:
            self._preparation_failures.add(request.execution_id)
            raise
        if not self._futures:
            self._pool = (ThreadPoolExecutor(max_workers=request.max_parallel_cases)
                          if request.max_parallel_cases > 1 else None)
            if self.settings.total_timeout_seconds > 0:
                self._deadline = self._monotonic() + self.settings.total_timeout_seconds
        handle = request.execution_id
        if handle in self._futures:
            raise TargetExecutionError("invalid_request", "duplicate execution ID")
        self._cancelled[handle] = Event()
        session_id = _case_session_id(request)
        previous = self._session_tails.get(session_id)

        def run() -> CaseExecutionResult:
            if previous is not None:
                try:
                    previous.result()
                except (TargetExecutionError, CancelledError):
                    LOGGER.debug("Continuing session %r after a prior failure", session_id)
            self._check_cancelled(handle)
            LOGGER.info("inbank_case_start adapter=%s run_id=%r case_id=%r execution_id=%r",
                        self.adapter_type, request.run_id, request.case.id, handle)
            try:
                result = self._execute_case(request)
                self._check_cancelled(handle)
                LOGGER.info("inbank_case_complete adapter=%s run_id=%r case_id=%r failed=%s",
                            self.adapter_type, request.run_id, request.case.id,
                            result.error is not None)
                return result
            except TargetExecutionError:
                raise
            except Exception as exc:  # noqa: BLE001 - sanitize at the adapter boundary.
                raise TargetExecutionError(
                    "protocol_error", f"Case execution failed ({type(exc).__name__})"
                ) from None

        if self._pool is None:
            future: Future[CaseExecutionResult] = Future()
            self._futures[handle] = future
            try:
                future.set_result(run())
            except Exception as exc:
                future.set_exception(exc)
                raise
        else:
            future = self._pool.submit(run)
            self._futures[handle] = future
        self._session_tails[session_id] = future
        return handle

    def _future(self, handle: str) -> Future[CaseExecutionResult]:
        try:
            return self._futures[handle]
        except KeyError:
            raise TargetExecutionError("invalid_request", "unknown execution") from None

    def get_status(self, handle: str) -> CaseExecutionStatus:
        if handle in self._preparation_failures:
            return CaseExecutionStatus.FAILED
        future = self._future(handle)
        if self._cancelled[handle].is_set() or future.cancelled():
            return CaseExecutionStatus.CANCELLED
        if not future.done():
            return CaseExecutionStatus.RUNNING
        if future.exception() is not None or future.result().error is not None:
            return CaseExecutionStatus.FAILED
        return CaseExecutionStatus.COMPLETED

    def wait(self, handle: str, timeout_seconds: float) -> CaseExecutionResult:
        if timeout_seconds <= 0:
            raise TargetExecutionError("invalid_request", "timeout must be positive")
        future = self._future(handle)
        self._check_cancelled(handle)
        try:
            return future.result(timeout=min(timeout_seconds, 0.1))
        except TimeoutError:
            raise CaseExecutionPending() from None
        except CancelledError:
            raise TargetExecutionError("cancelled", "Case execution was cancelled") from None

    def _check_cancelled(self, handle: str) -> None:
        if self._cancelled[handle].is_set():
            raise TargetExecutionError("cancelled", "Case execution was cancelled")

    def _before_turn(self, request: CaseExecutionRequest) -> None:
        self._check_cancelled(request.execution_id)
        if self._deadline is not None and self._monotonic() > self._deadline:
            raise TargetExecutionError("timeout", "Overall evaluation timeout exceeded")

    def cancel(self, handle: str) -> None:
        future = self._future(handle)
        if not future.done():
            self._cancelled[handle].set()
            future.cancel()

    def close(self) -> None:
        """Best-effort, idempotent cleanup using the Yunxia manager API."""

        if self._closed:
            return
        self._closed = True
        for handle in self._futures:
            self.cancel(handle)
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
        agent_name = self._agent_name
        self._agent_name = None
        if agent_name is None:
            return
        self._pod_lifecycle["delete"] = "started"
        try:
            result = delete_agent_pod(
                agent_name, self._cfg, timeout=self.settings.delete_timeout_seconds, http=self._http,
            )
            if not result["success"]:
                raise TargetExecutionError(result["error_code"], result["error"])
            self._pod_lifecycle["delete"] = "succeeded"
            self._log_pod_lifecycle("delete", "succeeded")
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the Run failure.
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
            raise TargetExecutionError("invalid_request", "adapter already belongs to another Run")
        if self._agent_name is not None:
            return
        self._run_id = request.run_id
        self._run_customer_suffix = uuid4().hex
        customer_task_id = _task_id_from_run_id(request.run_id)
        branch_id = _branch_id(request)
        version = request.target.invocation_config.get(
            "agent_version", request.target.ref.external_version_id
        )
        if not isinstance(version, str) or not version.strip():
            raise TargetExecutionError("invalid_request", "target agent_version must be nonblank")
        self._pod_lifecycle["create"] = "started"
        result = create_agent_pod(
            customer_task_id,
            request.target.ref.external_target_id,
            version,
            self._cfg,
            branch_id=branch_id,
            http=self._http,
            sleep=self._sleep,
            timeout=self.settings.create_timeout_seconds,
        )
        if not result["success"]:
            self._pod_lifecycle["create"] = "failed"
            self._log_pod_lifecycle("create", "failed")
            raise TargetExecutionError(result["error_code"], result["error"])
        self._agent_name = result["agent_name"]
        self._pod_name = self._agent_name
        self._pod_lifecycle["create"] = "succeeded"
        self._log_pod_lifecycle("create", "succeeded")
        self._pod_lifecycle["health"] = "started"
        try:
            self._wait_until_healthy()
        except Exception:
            self._pod_lifecycle["health"] = "failed"
            self._log_pod_lifecycle("health", "failed")
            raise
        self._pod_lifecycle["health"] = "succeeded"
        self._log_pod_lifecycle("health", "succeeded")

    def _wait_until_healthy(self) -> None:
        assert self._agent_name is not None
        if not wait_for_pod_ready(
            self._agent_name,
            self._cfg,
            self.settings.health_poll_interval_seconds,
            self.settings.health_wait_seconds,
            http=self._http,
            sleep=self._sleep,
            monotonic=self._monotonic,
            request_timeout=self.settings.health_request_timeout_seconds,
        ):
            raise TargetExecutionError("timeout", "Yunxia Pod health check timed out")

    def _execute_case(self, request: CaseExecutionRequest) -> CaseExecutionResult:
        assert self._agent_name is not None
        assert self._run_customer_suffix is not None
        session_id = _case_session_id(request)
        cfg = {**self._cfg, "_RUN_CUST_UUID": self._run_customer_suffix}
        input_field = _input_field(request)
        spans: list[TraceSpan] = []
        outcomes: dict[str, dict[str, Any]] = {}
        project_id = request.target.ref.external_target_id

        for sequence, turn in enumerate(request.case.turns):
            started_at = utcnow()
            try:
                self._before_turn(request)
                turn_input = turn.input.to_dict()
                customer_id = (turn_input.get("custID") or "").strip() or session_id
                request_id = str(uuid4())
                row = {
                    "questionId": session_id,
                    "custId": customer_id,
                    "user_input": turn_input[input_field],
                    "currentDisplay_1": turn_input.get("appHistory", []),
                    "config_variables": turn_input.get("config_variables") or [],
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
                chat = process_single_row(
                    row,
                    self._agent_name,
                    cfg,
                    timeout=min(request.timeout_seconds, self.settings.request_timeout_seconds),
                    http=self._http,
                    headers=self._pod_headers(
                        request.traceparent, accept="text/event-stream", request_id=request_id
                    ),
                )
                if chat["error"]:
                    if self.settings.debug_sse_failures:
                        LOGGER.error(
                            "inbank_sse_failure adapter=yunxia run_id=%r case_id=%r "
                            "turn_id=%r request_id=%r session_id=%r response_body=%r",
                            request.run_id,
                            request.case.id,
                            turn.id,
                            request_id,
                            session_id,
                            json.dumps(chat["events"], ensure_ascii=False)[:8192],
                        )
                    code, _, detail = chat["error"].partition(": ")
                    raise TargetExecutionError(code, detail)
                self._check_cancelled(request.execution_id)
                ended_at = utcnow()
                parsed, message_data = try_parse_json(chat["message_data"])
                final_output = message_data if parsed and isinstance(message_data, dict) else {
                    "output": message_data if parsed else chat["message_data"],
                }
                outcomes[turn.id] = {
                    "input": turn_input,
                    "output": final_output,
                    "state": {},
                }
                LOGGER.info(
                    "inbank_trace_query adapter=yunxia run_id=%r case_id=%r turn_id=%r "
                    "project_id=%r request_id=%r output_chars=%d",
                    request.run_id, request.case.id, turn.id, project_id, request_id,
                    len(chat["output"]),
                )
                events: list[dict] = []
                try:
                    events = self._fetch_trace_events(
                        project_id,
                        request_id,
                        customer_task_id=_task_id_from_run_id(request.run_id),
                        final_output=final_output,
                        timeout=min(
                            request.timeout_seconds,
                            self.settings.request_timeout_seconds,
                            self.settings.trace_settings.wait_seconds,
                        ),
                    )
                    turn_spans = normalize_turn_evidence(
                        request, turn, project_id=project_id, request_id=request_id, events=events,
                    )
                    outcomes[turn.id]["state"] = json.loads(
                        turn_spans[0].attributes["agentgate.turn.state"]
                    )
                except TargetExecutionError as exc:
                    turn_spans = (output_only_turn_span(
                        request, turn, adapter="yunxia", project_id=project_id,
                        request_id=request_id, started_at=started_at, ended_at=ended_at, error=exc,
                    ),)
                turn_spans = project_yunxia_intent_selection(turn_spans, final_output)
                for span in turn_spans:
                    spans.append(span.model_copy(update={"sequence": len(spans)}))
                LOGGER.info(
                    "inbank_turn_complete adapter=yunxia run_id=%r case_id=%r "
                    "turn_id=%r sequence=%d request_id=%r response_bytes=%d "
                    "output_chars=%d trace_project_id=%r "
                    "source_trace_id=%r trace_event_count=%d evidence_mode=%s elapsed_ms=%.1f",
                    request.run_id,
                    request.case.id,
                    turn.id,
                    sequence,
                    request_id,
                    len(json.dumps(chat["events"]).encode("utf-8")),
                    len(chat["output"]),
                    project_id,
                    request_id,
                    len(events),
                    turn_spans[0].attributes["inbank.evidence_mode"],
                    (ended_at - started_at).total_seconds() * 1000,
                )
            except Exception as exc:  # noqa: BLE001 - retain the customer's per-row failure boundary.
                error = exc if isinstance(exc, TargetExecutionError) else TargetExecutionError(
                    "protocol_error", f"Customer turn failed ({type(exc).__name__})"
                )
                if error.code in {"cancelled", "invalid_request"}:
                    raise error
                span, outcomes[turn.id] = _failed_turn_evidence(
                    request, turn, error, started_at=started_at,
                )
                spans.append(span.model_copy(update={"sequence": len(spans)}))
        return _case_execution_result(request, spans, outcomes)

    def _fetch_trace_events(
        self, project_id: str, trace_id: str, *, customer_task_id: str,
        final_output: dict, timeout: float,
    ) -> list[dict]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise TargetExecutionError(
                "invalid_request", "Trace wait timeout must be finite and positive"
            )
        deadline = self._monotonic() + timeout
        trace_settings = self.settings.trace_settings
        attempt = 0
        reason = "not_queried"
        previous_snapshot = None
        stable_since = None
        stable_samples = 0
        while (remaining := deadline - self._monotonic()) > 0:
            attempt += 1
            try:
                events = self._trace_server.fetch_events(
                    project_id, trace_id, timeout=remaining, customer_task_id=customer_task_id
                )
            except TargetExecutionError as exc:
                if exc.code not in {"unavailable", "timeout"} and not (
                    exc.code == "rejected"
                    and exc.message
                    in {"trace server HTTP 404", "trace server HTTP 409", "trace server HTTP 429"}
                ):
                    raise
                reason = "trace_not_found" if exc.message == "inbank trace not found" else exc.code
                previous_snapshot = stable_since = None
                stable_samples = 0
            else:
                prepared = prepare_inbank_trace_events(
                    events, project_id=project_id, customer_task_id=customer_task_id,
                    trace_id=trace_id, final_output=final_output,
                )
                snapshot = inbank_trace_snapshot(events)
                now = self._monotonic()
                if prepared is None:
                    previous_snapshot = stable_since = None
                    stable_samples = 0
                    reason = "evidence_pending"
                else:
                    if snapshot != previous_snapshot:
                        previous_snapshot, stable_since = snapshot, now
                        stable_samples = 0
                    stable_samples += 1
                    reason = "snapshot_settling"
                    if (
                        stable_samples < trace_settings.stable_samples
                        or now - stable_since < trace_settings.settle_seconds
                    ):
                        prepared = None
                if prepared is not None:
                    if self._monotonic() >= deadline:
                        break
                    next(e for e in prepared if e["event_type"] == "trace")["inbank_source"]["readiness"] = {
                        "basis": "stable_snapshot",
                        "attempts": attempt,
                        "stable_samples": stable_samples,
                        "stable_seconds": now - stable_since,
                        "snapshot_sha256": snapshot,
                    }
                    LOGGER.info(
                        "inbank_trace_ready adapter=yunxia run_id=%r source_trace_id=%r attempt=%d "
                        "basis=stable_snapshot stable_samples=%d stable_seconds=%.3f snapshot=%s",
                        self._run_id,
                        trace_id,
                        attempt,
                        stable_samples,
                        now - stable_since,
                        snapshot,
                    )
                    return prepared
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            delay = min(trace_settings.poll_interval_seconds, remaining)
            LOGGER.info(
                "inbank_trace_wait adapter=yunxia run_id=%r source_trace_id=%r "
                "attempt=%d reason=%s retry_delay_seconds=%.3f",
                self._run_id,
                trace_id,
                attempt,
                reason,
                delay,
            )
            self._sleep(delay)
        raise TargetExecutionError(
            "timeout", f"Trace Server evidence not ready within {timeout:g}s ({reason})"
        )

    def _log_pod_lifecycle(self, phase: str, status: str) -> None:
        LOGGER.info(
            "inbank_pod_lifecycle adapter=yunxia run_id=%r pod_name=%r phase=%s status=%s",
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
            raise TargetExecutionError("invalid_request", "Yunxia target must be an Agent")
        if target.adapter_type != self.adapter_type:
            raise TargetExecutionError("invalid_request", "adapter_type does not match")
        if target.adapter_version != self.adapter_version:
            raise TargetExecutionError("invalid_request", "adapter_version does not match")
        _branch_id(request)
        if request.case.initial_state:
            raise TargetExecutionError(
                "invalid_request", "Yunxia request fields belong in each turn input, not initial_state"
            )
        input_field = _input_field(request)
        allowed = {input_field, "appHistory", "custID", "sessionId", "config_variables", "files"}
        for turn in request.case.turns:
            value = turn.input.to_dict()
            if set(value) - allowed or input_field not in value:
                raise TargetExecutionError(
                    "invalid_request",
                    f"Yunxia input requires {input_field}; optional fields are "
                    "appHistory, custID, sessionId, config_variables and empty files",
                )
            if not isinstance(value[input_field], str) or not value[input_field].strip():
                raise TargetExecutionError(
                    "invalid_request",
                    f"CaseTurn {input_field} must be nonblank text",
                )
            for key in ("custID", "sessionId"):
                if value.get(key) is not None and not isinstance(value[key], str):
                    raise TargetExecutionError("invalid_request", f"Yunxia {key} must be text")
            variables = value.get("config_variables")
            if variables is not None and not isinstance(variables, list):
                raise TargetExecutionError("invalid_request", "Yunxia config_variables must be an array")
            files = value.get("files", [])
            if not isinstance(files, list):
                raise TargetExecutionError("invalid_request", "CaseTurn files must be an array")
            if files:
                raise TargetExecutionError("invalid_request", "non-empty files are not supported")
        _case_session_id(request)


def _case_session_id(request: CaseExecutionRequest) -> str:
    session_ids = {
        value.strip()
        for turn in request.case.turns
        if isinstance(value := turn.input.to_dict().get("sessionId"), str) and value.strip()
    }
    if len(session_ids) > 1:
        raise TargetExecutionError("invalid_request", "Yunxia sessionId must be consistent within a Case")
    return next(iter(session_ids), request.case.id)


def _input_field(request: CaseExecutionRequest) -> str:
    value = request.target.invocation_config.get("input_field", "txt")
    if not isinstance(value, str) or not value.strip():
        raise TargetExecutionError("invalid_request", "target input_field must be nonblank text")
    return value


def _branch_id(request: CaseExecutionRequest) -> str:
    value = request.target.invocation_config.get("branch_id")
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.strip().casefold() in {"null", "none"}
    ):
        raise TargetExecutionError("invalid_request", "Yunxia target branch_id is required")
    return value.strip()


def resolve_yunxia_trace(request: CaseExecutionRequest, result: CaseExecutionResult) -> Trace:
    """Return real trace evidence or explicitly marked output-only turn records."""

    if result.execution_id != request.execution_id:
        raise TargetExecutionError(
            "protocol_error", "Yunxia result execution does not match request"
        )
    if result.inline_trace is None:
        raise TargetExecutionError("protocol_error", "Yunxia execution returned no inline Trace")
    return result.inline_trace


def _failed_turn_evidence(
    request: CaseExecutionRequest, turn: CaseTurn, error: TargetExecutionError,
    *, started_at: datetime,
) -> tuple[TraceSpan, dict]:
    if error.code == "cancelled":
        raise error
    detail = {"code": error.code, "message": error.message}
    span = TraceSpan(
        trace_id=request.traceparent.split("-")[1], span_id=uuid4().hex[:16],
        name="inbank.yunxia.turn", operation_type="turn", sequence=0,
        started_at=started_at, ended_at=utcnow(), status="error",
        attributes={"agentgate.turn.id": turn.id,
                    "agentgate.execution.id": request.execution_id,
                    "agentgate.execution.error": detail},
    )
    LOGGER.warning("inbank_turn_failed run_id=%r case_id=%r turn_id=%r code=%s",
                   request.run_id, request.case.id, turn.id, error.code)
    return span, {"input": turn.input.to_dict(), "output": {},
                  "state": {"execution_status": "error"}, "error": detail}


def _case_execution_result(
    request: CaseExecutionRequest, spans: list[TraceSpan], outcomes: dict,
) -> CaseExecutionResult:
    final = outcomes[request.case.turns[-1].id]
    trace = Trace(
        trace_id=request.traceparent.split("-")[1], run_id=request.run_id,
        case_id=request.case.id, spans=tuple(spans), turn_outcomes=outcomes,
        final_output=final["output"], final_state=final["state"],
    )
    failure = next((item["error"] for item in outcomes.values() if "error" in item), None)
    error = TargetExecutionError(failure["code"], failure["message"]) if failure else None
    return CaseExecutionResult(request.execution_id, trace.trace_id, trace, error)


def _load_total_timeout(values: Mapping[str, str]) -> float:
    name = "AGENTGATE_INBANK_TOTAL_TIMEOUT_SECONDS"
    value = float(values.get(name, "0"))
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


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
    }
    missing = [env_name for env_name in required.values() if not values.get(env_name)]
    if missing:
        raise ValueError("incomplete Yunxia environment: " + ", ".join(sorted(missing)))
    return YunxiaSettings(
        **{field: values[name] for field, name in required.items()},
        request_timeout_seconds=_positive_float(
            values, "AGENTGATE_INBANK_REQUEST_TIMEOUT_SECONDS", 180.0
        ),
        total_timeout_seconds=_load_total_timeout(values),
        trace_settings=load_inbank_trace_settings(values),
        create_timeout_seconds=_positive_float(values, "AGENTGATE_INBANK_CREATE_TIMEOUT_SECONDS", 60),
        delete_timeout_seconds=_positive_float(values, "AGENTGATE_INBANK_DELETE_TIMEOUT_SECONDS", 60),
        health_request_timeout_seconds=_positive_float(
            values, "AGENTGATE_INBANK_HEALTH_REQUEST_TIMEOUT_SECONDS", 10
        ),
        health_wait_seconds=_positive_float(values, "AGENTGATE_INBANK_HEALTH_WAIT_SECONDS", 300.0),
        health_poll_interval_seconds=_positive_float(
            values, "AGENTGATE_INBANK_HEALTH_POLL_INTERVAL_SECONDS", 5.0
        ),
        debug_sse_failures=_environment_flag(values, "AGENTGATE_INBANK_DEBUG_SSE_FAILURES"),
    )


def _task_id_from_run_id(run_id: str) -> str:
    if len(run_id) < 8:
        raise TargetExecutionError("invalid_request", "run_id must contain at least 8 characters")
    return run_id[-8:]


def _url(base: str, path: str, query: Mapping[str, str] | None = None) -> str:
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
            detail = response.get("resMessage") or response.get("message") or (
                f"{field_name}={response[field_name]}"
            )
            raise TargetExecutionError("rejected", str(detail))


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    try:
        result = default if raw is None else float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive number") from None
    if result <= 0:
        raise ValueError(f"{name} must be a positive number")
    return result


def _environment_flag(values: Mapping[str, str], name: str) -> bool:
    return values.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


__all__ = [
    "InbankYunxiaTargetAdapter",
    "YunxiaSettings",
    "load_yunxia_settings",
    "resolve_yunxia_trace",
]
