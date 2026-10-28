"""Execute in-bank base and workflow Agents through the ChatABC protocol."""

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
from typing import Any, Literal
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
_CHATABC_SUCCESS_CODE = "FAIAG0000"
_DELETE_SUCCESS_CODES = frozenset({"0", "0000", _CHATABC_SUCCESS_CODE})

ChatABCArrangeType = Literal["base", "workflow"]


# Adapted from customer workflow&bianpai.py; AgentGate owns scheduling and reports.
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
) -> dict:
    http = http or requests
    query = {"taskId": task_id}
    if is_vip_eval:
        query["evalType"] = "formal"
    url = _url(cfg["CCE_CREATE_BASE_URL"], "/web/agent_endpoint/createAgent", query)
    body = {"agentId": agent_id, "agentVersion": agent_version}

    def _attempt() -> dict:
        try:
            with http.post(
                url, json=body, timeout=timeout, headers={"Content-Type": "application/json"}
            ) as resp:
                resp.raise_for_status()
                data = resp.json()
            LOGGER.debug(
                "inbank_http_json adapter=chatabc url=%s %s shape=%s",
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
                "inbank_lifecycle_retry adapter=chatabc phase=create "
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
    url = _url(
        cfg["HEALTH_CHECK_BASE_URL"],
        f"/agent-api/{quote(agent_name, safe='')}/chatabc/health_check",
    )
    start = monotonic()
    while monotonic() - start < max_wait:
        try:
            with http.get(url, timeout=request_timeout) as resp:
                resp.raise_for_status()
                data = resp.json()
            inner = data.get("data") if isinstance(data, dict) else None
            if isinstance(inner, dict) and inner.get("status") == "ok":
                return True
        except Exception as exc:  # noqa: BLE001 - retain customer retry/cleanup boundary.
            LOGGER.warning(
                "inbank_health_pending adapter=chatabc error_type=%s", type(exc).__name__
            )
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


def make_request(method: str, url: str, *, http=None, **kwargs) -> dict:
    http = http or requests
    try:
        with http.request(method, url, **kwargs) as resp:
            resp.raise_for_status()
            data = resp.json()
        if data.get("resCode") == "FAIAG0000":
            return {
                "status": "success",
                "data": data.get("data"),
                "message": data.get("resMessage", ""),
            }
        return {
            "status": "fail",
            "error_code": "rejected",
            "message": data.get("resMessage") or data.get("message") or "未知错误",
        }
    except Exception as exc:  # noqa: BLE001 - retain customer retry/cleanup boundary.
        failure = _request_failure(exc)
        return {"status": "fail", "error_code": failure["error_code"], "message": failure["error"]}


class AgentChatClient:
    """Customer ChatABC client; Case/Turn mapping stays in the adapter."""

    def __init__(
        self,
        agent_name: str,
        cfg: Mapping,
        arrange_type: str = "base",
        *,
        http=None,
        sleep=time.sleep,
        headers: Mapping | None = None,
    ):
        self.agent_name = agent_name
        self.cfg = cfg
        self.arrange_type = arrange_type
        self.base_url = cfg["POD_API_BASE_URL"].rstrip("/")
        self.http = http or requests
        self.sleep = sleep
        self.headers = dict(headers or {})

    def _common_headers(self) -> dict:
        return {"Content-Type": "application/json", **self.headers}

    def _build_app_payload(self, data: dict) -> dict:
        return {
            "appId": "BDC201704_01",
            "trCode": "AISPNLPCHATBOT",
            "trVersion": "1",
            "timestamp": int(time.time() * 1000),
            "requestId": str(uuid4()),
            "data": data,
        }

    def init_session(
        self, prompt_variables: list, tool_variables: list | None = None, *, timeout: float = 30,
    ) -> str:
        url = _url(
            self.base_url, f"/agent-api/{quote(self.agent_name, safe='')}/chatabc/init_session"
        )
        data = (
            {
                "config_variables": [
                    {key: variable[key] for key in ("name", "value", "type") if key in variable}
                    for variable in (prompt_variables or []) + (tool_variables or [])
                    if variable.get("value") is not None
                    and (
                        not isinstance(variable["value"], str) or variable["value"].strip()
                    )
                ]
            }
            if self.arrange_type == "workflow"
            else {
                "prompt_variables": prompt_variables or [],
                "tool_variables": tool_variables or [],
            }
        )
        payload = self._build_app_payload(data)
        for attempt in range(1, 11):
            result = make_request(
                "POST",
                url,
                http=self.http,
                json=payload,
                timeout=timeout,
                headers=self._common_headers(),
            )
            if result.get("status") == "success":
                session_id = (result.get("data") or {}).get("session_id")
                if session_id:
                    return session_id
                result = {
                    "error_code": "protocol_error",
                    "message": "ChatABC session response has no session_id",
                }
            if attempt < 10:
                LOGGER.warning(
                    "inbank_lifecycle_retry adapter=chatabc phase=init_session "
                    "attempt=%d max_attempts=10 retry_delay_seconds=5",
                    attempt,
                )
                self.sleep(5)
        raise TargetExecutionError(result.get("error_code", "rejected"), result["message"])

    def chat_stream(
        self, session_id: str, question: str, files: list | None = None, timeout: float = 300
    ) -> tuple:
        url = _url(self.base_url, f"/agent-api/{quote(self.agent_name, safe='')}/chatabc/chat")
        payload = self._build_app_payload(
            {"session_id": session_id, "txt": question, "files": files or [], "stream": True}
        )
        request_id = payload["requestId"]
        LOGGER.info(
            "inbank_chat_request adapter=chatabc arrange_type=%s "
            "pod_name=%r session_id=%r request_id=%r",
            self.arrange_type,
            self.agent_name,
            session_id,
            request_id,
        )
        start = time.time()
        try:
            with self.http.post(
                url,
                json=payload,
                stream=True,
                timeout=timeout,
                headers={
                    **self._common_headers(),
                    "Accept": "text/event-stream",
                    "X-Request-ID": request_id,
                },
            ) as resp:
                resp.raise_for_status()
                resp.encoding = "utf-8-sig"
                events = self._parse_sse(resp, deadline=start + timeout)
            answer, full_answer, error = self._extract_chat_answer(events)
            return answer, full_answer, error, time.time() - start, events, request_id
        except Exception as exc:  # noqa: BLE001 - retain customer retry/cleanup boundary.
            failure = _request_failure(exc)
            return (
                "",
                "",
                f"{failure['error_code']}: {failure['error']}",
                (time.time() - start),
                [],
                request_id,
            )

    def _extract_chat_answer(self, events: list) -> tuple:
        messages = []
        done_seen = False
        for ev in events:
            event_name = ev.get("event", "").strip().lower()
            event_data = ev.get("data", "")
            if event_name in {"failed", "error"}:
                try:
                    failure = json.loads(event_data)
                except (TypeError, ValueError):
                    failure = None
                detail = (
                    failure.get("message") or failure.get("errorCode")
                    if isinstance(failure, dict)
                    else event_data
                )
                return (
                    "",
                    json.dumps(events, ensure_ascii=False),
                    f"rejected: {str(detail or 'customer chat returned an error event')[:500]}",
                )
            if event_name == "done":
                # Customer workflow&bianpai.py uses done only as an end marker.
                done_seen = True
                break
            if event_name == "message":
                messages.append(event_data)
        if not done_seen and events:
            LOGGER.warning("[chat] no done event; using the customer message output")
        if not messages:
            return (
                "",
                json.dumps(events, ensure_ascii=False),
                ("protocol_error: chat returned no message event"),
            )
        try:
            last_message = json.loads(messages[-1])
        except ValueError:
            last_message = {"content": str(messages[-1])}
        if self.arrange_type == "workflow":
            answer = ""
            for msg in messages:
                try:
                    item = json.loads(msg)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                additional = item.get("additional_kwargs") or {}
                # Also accept the end-node location already verified in-bank.
                if item.get("node_id", additional.get("node_id")) != "end":
                    continue
                node_output = additional.get("node_output", "")
                answer = (
                    node_output.get("output", "") if isinstance(node_output, dict) else node_output
                )
                if answer:
                    break
        else:
            answer = last_message.get("content", "")
        return (
            str(answer),
            json.dumps(
                {"answer": answer, "arrange_type": self.arrange_type, "events": events},
                ensure_ascii=False,
            ),
            "",
        )

    @staticmethod
    def _parse_sse(response, deadline: float | None = None) -> list:
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


@dataclass(frozen=True, slots=True)
class ChatABCSettings:
    """Non-secret endpoints and time limits for ChatABC execution."""

    create_base_url: str
    health_base_url: str
    pod_api_base_url: str
    delete_base_url: str
    agent_namespace: str = "chatabc"
    request_timeout_seconds: float = 180.0
    health_wait_seconds: float = 300.0
    health_poll_interval_seconds: float = 5.0
    create_timeout_seconds: float = 60.0
    init_timeout_seconds: float = 30.0
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
            value = _validated_base_url(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)
        if not isinstance(self.agent_namespace, str) or not self.agent_namespace.strip():
            raise ValueError("agent_namespace must be nonblank")
        for field_name in (
            "request_timeout_seconds",
            "health_wait_seconds",
            "health_poll_interval_seconds",
            "create_timeout_seconds", "delete_timeout_seconds", "health_request_timeout_seconds",
            "init_timeout_seconds",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if not isinstance(self.trace_settings, InbankTraceSettings):
            raise TypeError("trace_settings must be InbankTraceSettings")


class InbankChatABCTargetAdapter:
    """Own one lazily created ChatABC Pod for one EvaluationRun."""

    adapter_type = "inbank_chatabc"
    adapter_version = "1"

    def __init__(
        self,
        settings: ChatABCSettings,
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
            raise ValueError("ChatABC requires an explicit Trace Server client")
        self._trace_server = trace_client
        self._sleep = sleep
        self._monotonic = monotonic
        self._run_id: str | None = None
        self._agent_name: str | None = None
        self._pod_name: str | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._futures: dict[str, Future[CaseExecutionResult]] = {}
        self._cancelled: dict[str, Event] = {}
        self._session_tails: dict[str, Future[CaseExecutionResult]] = {}
        self._deadline: float | None = None
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
        if (request.execution_id in self._preparation_failures
                or request.execution_id in self._futures):
            raise TargetExecutionError("invalid_request", "duplicate execution ID")
        try:
            self._ensure_pod(request)
        except TargetExecutionError:
            self._preparation_failures.add(request.execution_id)
            raise
        if not self._futures:
            if request.max_parallel_cases > 1:
                self._pool = ThreadPoolExecutor(max_workers=request.max_parallel_cases)
            if self.settings.total_timeout_seconds > 0:
                self._deadline = self._monotonic() + self.settings.total_timeout_seconds
        handle = request.execution_id
        self._cancelled[handle] = Event()
        previous = self._session_tails.get(request.case.id)

        def run() -> CaseExecutionResult:
            if previous is not None:
                try:
                    previous.result()
                except (TargetExecutionError, CancelledError):
                    LOGGER.debug("Continuing Case %r after a prior failure", request.case.id)
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

        # Customer workflow&bianpai.py submits complete sessions, not individual turns.
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
        self._session_tails[request.case.id] = future
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
        """Best-effort, idempotent cleanup through the unified manager API."""

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
                "inbank_pod_lifecycle adapter=chatabc run_id=%r "
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
        customer_task_id = _task_id_from_run_id(request.run_id)
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
            raise TargetExecutionError("timeout", "Agent Pod health check timed out")

    def _execute_case(self, request: CaseExecutionRequest) -> CaseExecutionResult:
        assert self._agent_name is not None
        arrange_type = _arrange_type(request)
        initial = request.case.initial_state.to_dict()
        client = AgentChatClient(
            self._agent_name,
            self._cfg,
            arrange_type,
            http=self._http,
            sleep=self._sleep,
            headers=self._pod_headers(request.traceparent),
        )
        try:
            session_id = client.init_session(
                initial.get("config_variables", [])
                if arrange_type == "workflow"
                else initial.get("prompt_variables", []),
                initial.get("tool_variables", []) if arrange_type == "base" else [],
                timeout=self.settings.init_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - retain the customer's per-session failure boundary.
            error = exc if isinstance(exc, TargetExecutionError) else TargetExecutionError(
                "protocol_error", f"Session initialization failed ({type(exc).__name__})"
            )
            if error.code in {"cancelled", "invalid_request"}:
                raise error
            spans, outcomes = [], {}
            for turn in request.case.turns:
                span, outcomes[turn.id] = _failed_turn_evidence(
                    request, turn, error, started_at=utcnow(), adapter=arrange_type,
                )
                spans.append(span.model_copy(update={"sequence": len(spans)}))
            return _case_execution_result(request, spans, outcomes)
        LOGGER.info(
            "inbank_session_ready adapter=chatabc run_id=%r case_id=%r "
            "arrange_type=%s session_id=%r",
            request.run_id,
            request.case.id,
            arrange_type,
            session_id,
        )

        input_field = _input_field(request)
        spans: list[TraceSpan] = []
        outcomes: dict[str, dict[str, Any]] = {}
        project_id = request.target.ref.external_target_id
        for sequence, turn in enumerate(request.case.turns):
            started_at = utcnow()
            try:
                self._before_turn(request)
                turn_input = turn.input.to_dict()
                question = _chatabc_question(turn_input, input_field)
                started_at = utcnow()
                answer, full_answer, error, _duration, _chat_events, request_id = client.chat_stream(
                    session_id,
                    question,
                    timeout=min(request.timeout_seconds, self.settings.request_timeout_seconds),
                )
                if error:
                    if self.settings.debug_sse_failures:
                        LOGGER.error(
                            "inbank_sse_failure adapter=chatabc run_id=%r case_id=%r "
                            "turn_id=%r request_id=%r session_id=%r response_body=%r",
                            request.run_id,
                            request.case.id,
                            turn.id,
                            request_id,
                            session_id,
                            full_answer[:8192],
                        )
                    code, _, detail = error.partition(": ")
                    raise TargetExecutionError(code, detail)
                self._check_cancelled(request.execution_id)
                ended_at = utcnow()
                final_output = {"output": answer}
                outcomes[turn.id] = {
                    "input": turn_input,
                    "output": final_output,
                    "state": {},
                }
                LOGGER.info(
                    "inbank_trace_query adapter=chatabc arrange_type=%s run_id=%r "
                    "case_id=%r turn_id=%r project_id=%r request_id=%r output_chars=%d",
                    arrange_type,
                    request.run_id,
                    request.case.id,
                    turn.id,
                    project_id,
                    request_id,
                    len(answer),
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
                        request, turn, adapter=arrange_type, project_id=project_id,
                        request_id=request_id, started_at=started_at, ended_at=ended_at, error=exc,
                    ),)
                for span in turn_spans:
                    spans.append(span.model_copy(update={"sequence": len(spans)}))
                LOGGER.info(
                    "inbank_turn_complete adapter=chatabc run_id=%r case_id=%r "
                    "turn_id=%r sequence=%d request_id=%r response_bytes=%d "
                    "output_chars=%d trace_project_id=%r source_trace_id=%r "
                    "trace_event_count=%d evidence_mode=%s elapsed_ms=%.1f",
                    request.run_id,
                    request.case.id,
                    turn.id,
                    sequence,
                    request_id,
                    len(full_answer.encode("utf-8")),
                    len(answer),
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
                    request, turn, error, started_at=started_at, adapter=arrange_type,
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
                        "inbank_trace_ready adapter=chatabc run_id=%r source_trace_id=%r attempt=%d "
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
                "inbank_trace_wait adapter=chatabc run_id=%r source_trace_id=%r "
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
            "inbank_pod_lifecycle adapter=chatabc run_id=%r pod_name=%r phase=%s status=%s",
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
            raise TargetExecutionError("invalid_request", "ChatABC target must be an Agent")
        if target.adapter_type != self.adapter_type:
            raise TargetExecutionError("invalid_request", "adapter_type does not match")
        if target.adapter_version != self.adapter_version:
            raise TargetExecutionError("invalid_request", "adapter_version does not match")
        arrange_type = _arrange_type(request)
        initial = request.case.initial_state.to_dict()
        expected = (
            {"prompt_variables", "tool_variables"}
            if arrange_type == "base"
            else {"config_variables"}
        )
        unexpected = set(initial) - expected
        if unexpected:
            raise TargetExecutionError(
                "invalid_request", "Case initial_state has unsupported fields"
            )
        for name in expected:
            value = initial.get(name, [])
            if not isinstance(value, list):
                raise TargetExecutionError(
                    "invalid_request", f"initial_state {name} must be an array"
                )
        if arrange_type == "workflow":
            names: set[str] = set()
            for variable in initial.get("config_variables", []):
                if (
                    not isinstance(variable, dict)
                    or not isinstance(variable.get("name"), str)
                    or not variable["name"].strip()
                    or "value" not in variable
                ):
                    raise TargetExecutionError(
                        "invalid_request", "config_variables entries require name and value"
                    )
                if variable["name"] in names:
                    raise TargetExecutionError(
                        "invalid_request", "config_variables names must be unique"
                    )
                names.add(variable["name"])
        input_field = _input_field(request)
        for turn in request.case.turns:
            _chatabc_question(turn.input.to_dict(), input_field)


def _failed_turn_evidence(
    request: CaseExecutionRequest, turn: CaseTurn, error: TargetExecutionError,
    *, started_at: datetime, adapter: str,
) -> tuple[TraceSpan, dict]:
    if error.code == "cancelled":
        raise error
    detail = {"code": error.code, "message": error.message}
    span = TraceSpan(
        trace_id=request.traceparent.split("-")[1], span_id=uuid4().hex[:16],
        name=f"inbank.{adapter}.turn", operation_type="turn", sequence=0,
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


def _chatabc_question(value: dict[str, Any], input_field: str) -> str:
    """Keep text envelopes unchanged; encode complete business objects only at transport."""
    if not value:
        raise TargetExecutionError("invalid_request", "ChatABC input must not be empty")
    if set(value) <= {input_field, "files"}:
        question = value.get(input_field)
        if not isinstance(question, str) or not question.strip():
            raise TargetExecutionError(
                "invalid_request", f"CaseTurn {input_field} must be nonblank text"
            )
        files = value.get("files", [])
        if not isinstance(files, list) or files:
            raise TargetExecutionError(
                "invalid_request", "ChatABC attachments are not supported"
            )
        return question
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _input_field(request: CaseExecutionRequest) -> str:
    value = request.target.invocation_config.get("input_field", "txt")
    if not isinstance(value, str) or not value.strip():
        raise TargetExecutionError("invalid_request", "target input_field must be nonblank text")
    return value


def resolve_chatabc_trace(request: CaseExecutionRequest, result: CaseExecutionResult) -> Trace:
    """Return real trace evidence or explicitly marked output-only turn records."""

    if result.execution_id != request.execution_id:
        raise TargetExecutionError(
            "protocol_error", "ChatABC result execution does not match request"
        )
    if result.inline_trace is None:
        raise TargetExecutionError("protocol_error", "ChatABC execution returned no inline Trace")
    return result.inline_trace


def load_chatabc_settings(
    environ: Mapping[str, str] | None = None,
) -> ChatABCSettings:
    """Load ChatABC endpoint configuration without reading credentials."""

    values = os.environ if environ is None else environ
    required = {
        "create_base_url": "AGENTGATE_INBANK_CREATE_BASE_URL",
        "health_base_url": "AGENTGATE_INBANK_CHATABC_HEALTH_BASE_URL",
        "pod_api_base_url": "AGENTGATE_INBANK_CHATABC_POD_API_BASE_URL",
        "delete_base_url": "AGENTGATE_INBANK_CHATABC_DELETE_BASE_URL",
    }
    missing = [env_name for env_name in required.values() if not values.get(env_name)]
    if missing:
        raise ValueError("incomplete ChatABC environment: " + ", ".join(sorted(missing)))
    return ChatABCSettings(
        **{field: values[name] for field, name in required.items()},
        agent_namespace=values.get("AGENTGATE_INBANK_CHATABC_AGENT_NAMESPACE", "chatabc"),
        request_timeout_seconds=_positive_float(
            values, "AGENTGATE_INBANK_REQUEST_TIMEOUT_SECONDS", 180.0
        ),
        total_timeout_seconds=_load_total_timeout(values),
        trace_settings=load_inbank_trace_settings(values),
        create_timeout_seconds=_positive_float(values, "AGENTGATE_INBANK_CREATE_TIMEOUT_SECONDS", 60),
        init_timeout_seconds=_positive_float(values, "AGENTGATE_INBANK_INIT_TIMEOUT_SECONDS", 30),
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


def _load_total_timeout(values: Mapping[str, str]) -> float:
    name = "AGENTGATE_INBANK_TOTAL_TIMEOUT_SECONDS"
    value = float(values.get(name, "0"))
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


def _arrange_type(request: CaseExecutionRequest) -> ChatABCArrangeType:
    value = request.target.invocation_config.get("arrange_type")
    if value not in {"base", "workflow"}:
        raise TargetExecutionError(
            "invalid_request", "ChatABC arrange_type must be base or workflow"
        )
    return value


def _environment_flag(values: Mapping[str, str], name: str) -> bool:
    return values.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _task_id_from_run_id(run_id: str) -> str:
    if len(run_id) < 8:
        raise TargetExecutionError("invalid_request", "run_id must contain at least 8 characters")
    return run_id[-8:]


def _require_delete_success(response: Mapping[str, Any]) -> None:
    for field_name in ("resCode", "code"):
        if field_name in response and str(response[field_name]) not in _DELETE_SUCCESS_CODES:
            detail = response.get("resMessage") or response.get("message") or (
                f"{field_name}={response[field_name]}"
            )
            raise TargetExecutionError("rejected", str(detail))


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


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    try:
        result = default if raw is None else float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive number") from None
    if result <= 0:
        raise ValueError(f"{name} must be a positive number")
    return result


__all__ = [
    "ChatABCArrangeType",
    "ChatABCSettings",
    "InbankChatABCTargetAdapter",
    "load_chatabc_settings",
    "resolve_chatabc_trace",
]
