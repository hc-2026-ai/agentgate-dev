from __future__ import annotations

import json
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest
from test_trace_server_client import DETAIL, LLM

from agentgate.domain import Case, CaseTurn, TargetRef, TargetSnapshot, TargetType
from agentgate.integrations.targets.inbank.chatabc import (
    ChatABCSettings,
    InbankChatABCTargetAdapter,
)
from agentgate.integrations.targets.inbank.yunxia import (
    InbankYunxiaTargetAdapter,
    YunxiaSettings,
)
from agentgate.run.target_protocol import (
    CaseExecutionRequest,
    CaseExecutionStatus,
    TargetExecutionError,
)


class _TraceServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def fetch_events(
        self, project_id: str, trace_id: str, *, timeout: float = 30
    ) -> list[dict[str, Any]]:
        self.calls.append((project_id, trace_id, timeout))
        common = {
            "project_id": project_id,
            "trace_id": trace_id,
            "started_at": "2026-09-23T00:00:00Z",
            "duration_ms": 5,
            "status": "success",
        }
        return [
            {
                **common,
                "event_type": "trace",
                "event_id": f"trace-event-{trace_id}",
                "input": {"txt": "recorded input"},
                "output": {"output": "customer answer"},
                "span_count": 1,
            },
            {
                **common,
                "event_type": "span",
                "event_id": f"span-event-{trace_id}",
                "span_id": f"span-{trace_id}",
                "parent_span_id": None,
                "name": "customer.lookup",
                "span_type": "tool",
                "tool_name": "lookup",
                "input": {"city": "上海"},
                "output": {"found": True},
            },
        ]


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class _Response:
    def __init__(self, data=None, raw=b""):
        self.data = data
        self.raw = raw
        self.status_code = 200
        self.encoding = "utf-8"
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    def iter_lines(self, decode_unicode=False):
        yield from self.raw.decode(self.encoding).splitlines()


class _Transport:
    def __init__(self, arrange_type: str, *, references: list[list[Any]] | None = None) -> None:
        self.arrange_type = arrange_type
        self.references = references
        self.chat_count = 0
        self.deleted = False

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def request(self, method, url, *, json=None, stream=False, **kwargs):
        if stream:
            return _Response(raw=self.request_bytes(method, url, payload=json, **kwargs))
        return _Response(data=self.request_json(method, url, payload=json, **kwargs))

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        if "/createAgent" in url:
            if self.arrange_type == "yunxia":
                assert payload["branchId"] == "customer-branch"
            return {"code": "0", "data": {"agentName": "customer-agent"}}
        if url.endswith("/health_check"):
            return {"data": {"status": "ok"}}
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/init_session"):
            return {
                "resCode": "FAIAG0000",
                "data": {"session_id": "customer-session"},
            }
        if url.endswith("/delete_agent"):
            self.deleted = True
            return {"resCode": "FAIAG0000", "data": {"agent_name": "customer-agent"}}
        raise AssertionError(f"unexpected JSON request: {method} {url}")

    def request_bytes(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> bytes:
        if self.arrange_type == "yunxia":
            assert url.endswith("/api/v1/message")
            assert payload["debugTrace"] is True
        elif not url.endswith("/chat"):
            raise AssertionError(f"unexpected byte request: {method} {url}")
        self.chat_count += 1
        output = "customer answer"
        if self.arrange_type == "workflow":
            message = {
                "node_id": "end",
                "additional_kwargs": {"node_output": {"output": output}},
            }
        elif self.arrange_type == "yunxia":
            message = {
                "status": "completed",
                "message": output,
                "intent_code": "lookup-intent",
                "slots": {"city": "上海"},
                "workflow_calls": [{"id": "workflow-one"}],
            }
        else:
            message = {"content": output}
        frames = [f"event: message\ndata: {json.dumps(message)}\n\n"]
        references = (
            self.references[self.chat_count - 1]
            if self.references is not None
            else [
                {
                    "project_id": "customer-project",
                    "trace_id": f"customer-trace-{self.chat_count}",
                }
            ]
        )
        for reference in references:
            frames.append(f"event: trace\ndata: {json.dumps(reference)}\n\n")
        frames.append("event: done\ndata: [DONE]\n\n")
        return "".join(frames).encode()


def _request(arrange_type: str) -> CaseExecutionRequest:
    target = TargetSnapshot(
        ref=TargetRef(
            source_id="customer-platform",
            target_type=TargetType.AGENT,
            external_target_id="customer-agent-id",
            external_version_id="customer-version",
        ),
        display_name="Customer Agent",
        adapter_type="inbank_yunxia" if arrange_type == "yunxia" else "inbank_chatabc",
        adapter_version="1",
        descriptor_sha256="a" * 64,
        invocation_config={
            "arrange_type": arrange_type,
            "agent_version": "customer-version",
            **({"branch_id": "customer-branch"} if arrange_type == "yunxia" else {}),
        },
    )
    case = Case(
        id="case-1",
        name="Two-turn customer case",
        turns=(
            CaseTurn(id="turn-1", input={"txt": "first"}),
            CaseTurn(id="turn-2", input={"txt": "second"}),
        ),
    )
    return CaseExecutionRequest(
        execution_id="execution-1",
        run_id="run-12345678",
        case=case,
        target=target,
        timeout_seconds=30,
        traceparent=f"00-{'1' * 32}-{'2' * 16}-01",
    )


def _adapter(
    arrange_type: str, transport, trace_server, *, base_url="https://bank.test", **options
):
    settings = {
        "create_base_url": base_url,
        "health_base_url": base_url,
        "pod_api_base_url": base_url,
        "delete_base_url": base_url,
        "request_timeout_seconds": 10,
        "health_wait_seconds": 10,
        "health_poll_interval_seconds": 1,
    }
    if arrange_type == "yunxia":
        return InbankYunxiaTargetAdapter(
            YunxiaSettings(**settings, agent_namespace="chatabc", test_customer_prefix="test"),
            http=transport,
            trace_client=trace_server,
            **options,
        )
    return InbankChatABCTargetAdapter(
        ChatABCSettings(**settings), http=transport, trace_client=trace_server, **options
    )


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
def test_builds_multiturn_trace_from_trace_server(arrange_type: str) -> None:
    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    adapter = _adapter(arrange_type, transport, trace_server)
    request = _request(arrange_type)

    try:
        result = adapter.wait(adapter.start(request), 30)
    finally:
        adapter.close()

    assert result.inline_trace is not None
    assert transport.deleted is True
    assert [call[:2] for call in trace_server.calls] == [
        ("customer-project", "customer-trace-1"),
        ("customer-project", "customer-trace-2"),
    ]
    assert len(result.inline_trace.spans) == 4
    for sequence, turn in enumerate(request.case.turns, 1):
        outcome = result.inline_trace.turn_outcomes[turn.id]
        assert outcome["input"] == turn.input
        assert outcome["output"]["output"] == "customer answer"
        if arrange_type == "yunxia":
            assert outcome["output"]["intent_code"] == "lookup-intent"
            assert outcome["output"]["slots"] == {"city": "上海"}
            assert outcome["output"]["workflow_calls"][0]["id"] == "workflow-one"
        turn_trace = result.inline_trace.for_turn(turn.id)
        assert turn_trace.spans[0].attributes["trace_sdk.trace_id"] == (
            f"customer-trace-{sequence}"
        )
        assert turn_trace.spans[0].attributes["trace_sdk.replay"] is False
        assert turn_trace.spans[1].operation_type == "tool"
        assert turn_trace.spans[1].name == "lookup"
        assert turn_trace.spans[1].attributes["arguments"] == {"city": "上海"}


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize(
    "references",
    [
        [[]],
        [[None]],
        [[{"trace_id": "trace"}]],
        [[{"project_id": "project", "trace_id": " "}]],
        [[{"project_id": "project", "trace_id": "trace"}] * 2],
        [
            [{"project_id": "project", "trace_id": "trace"}],
            [{"project_id": "other-project", "trace_id": "trace-2"}],
        ],
        [
            [{"project_id": "project", "trace_id": "trace"}],
            [{"project_id": "project", "trace_id": "trace"}],
        ],
    ],
)
def test_rejects_missing_or_ambiguous_trace_references(arrange_type, references) -> None:
    transport = _Transport(arrange_type, references=references)
    trace_server = _TraceServer()
    adapter = _adapter(arrange_type, transport, trace_server)

    try:
        with pytest.raises(TargetExecutionError) as error:
            adapter.start(_request(arrange_type))
    finally:
        adapter.close()

    assert error.value.code == "protocol_error"
    assert len(trace_server.calls) == len(references) - 1
    assert adapter.get_status("execution-1") is CaseExecutionStatus.FAILED
    assert transport.deleted is True


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("failure", ["unavailable", "empty", "wrong-trace", "failed-trace"])
def test_trace_failure_does_not_fall_back_to_output_only(arrange_type, failure, monkeypatch):
    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    fetch = trace_server.fetch_events

    def failing_fetch(project_id, trace_id, *, timeout):
        if failure == "unavailable":
            raise TargetExecutionError("unavailable", "trace server request failed")
        events = fetch(project_id, trace_id, timeout=timeout)
        if failure == "empty":
            return []
        if failure == "wrong-trace":
            events[0]["trace_id"] = "unrelated"
        else:
            events[0]["status"] = "error"
        return events

    monkeypatch.setattr(trace_server, "fetch_events", failing_fetch)
    clock = _Clock()
    adapter = _adapter(
        arrange_type, transport, trace_server, sleep=clock.sleep, monotonic=clock.monotonic
    )
    try:
        with pytest.raises(TargetExecutionError) as error:
            adapter.start(_request(arrange_type))
        assert error.value.code == ("timeout" if failure == "unavailable" else "protocol_error")
        with pytest.raises(TargetExecutionError, match="not complete"):
            adapter.wait("execution-1", 10)
    finally:
        adapter.close()
    assert transport.deleted is True


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize(
    "pending",
    ["404", "409", "429", "unavailable", "running", "spans", "output", "duration", "span-running"],
)
def test_trace_readiness_retries_without_replaying_chat(arrange_type, pending, monkeypatch):
    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    fetch = trace_server.fetch_events
    clock = _Clock()

    def delayed(project_id, trace_id, *, timeout):
        events = fetch(project_id, trace_id, timeout=timeout)
        if len(trace_server.calls) <= 2:
            if pending.isdigit():
                raise TargetExecutionError("rejected", f"trace server HTTP {pending}")
            if pending == "unavailable":
                raise TargetExecutionError("unavailable", "trace server request failed")
            if pending == "running":
                events[0]["status"] = "running"
            elif pending == "span-running":
                events[1]["status"] = "running"
            elif pending == "spans":
                events[0]["span_count"] = 1
                events.pop()
            elif pending == "output":
                events[0]["output"] = None
            elif pending == "duration":
                events[0]["duration_ms"] = None
        return events

    monkeypatch.setattr(trace_server, "fetch_events", delayed)
    adapter = _adapter(
        arrange_type, transport, trace_server, sleep=clock.sleep, monotonic=clock.monotonic
    )
    try:
        result = adapter.wait(adapter.start(_request(arrange_type)), 30)
        assert result.inline_trace.final_output["output"] == "customer answer"
        assert [call[1] for call in trace_server.calls] == [
            "customer-trace-1",
            "customer-trace-1",
            "customer-trace-1",
            "customer-trace-2",
        ]
        assert clock.sleeps == [2, 2]
        assert [call[2] for call in trace_server.calls] == [5, 4, 3, 5]
        assert transport.chat_count == 2
    finally:
        adapter.close()
    assert transport.deleted


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_trace_permanent_rejection_does_not_retry(arrange_type, status, monkeypatch):
    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    calls = []
    clock = _Clock()

    def rejected(*args, **kwargs):
        calls.append(args)
        raise TargetExecutionError("rejected", f"trace server HTTP {status}")

    monkeypatch.setattr(trace_server, "fetch_events", rejected)
    adapter = _adapter(
        arrange_type, transport, trace_server, sleep=clock.sleep, monotonic=clock.monotonic
    )
    try:
        with pytest.raises(TargetExecutionError, match=f"HTTP {status}"):
            adapter.start(_request(arrange_type))
    finally:
        adapter.close()
    assert len(calls) == 1 and clock.sleeps == []
    assert transport.chat_count == 1 and transport.deleted


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("pending", ["404", "running"])
def test_trace_readiness_deadline_preserves_failure_and_cleanup(arrange_type, pending, monkeypatch):
    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    fetch = trace_server.fetch_events
    clock = _Clock()

    def delayed(project_id, trace_id, *, timeout):
        events = fetch(project_id, trace_id, timeout=timeout)
        if pending == "404":
            raise TargetExecutionError("rejected", "trace server HTTP 404")
        events[0]["status"] = "running"
        return events

    monkeypatch.setattr(trace_server, "fetch_events", delayed)
    adapter = _adapter(
        arrange_type, transport, trace_server, sleep=clock.sleep, monotonic=clock.monotonic
    )
    try:
        with pytest.raises(TargetExecutionError, match="not ready within 10s") as error:
            adapter.start(_request(arrange_type))
        assert error.value.code == "timeout"
        assert adapter.get_status("execution-1") is CaseExecutionStatus.FAILED
    finally:
        adapter.close()
    assert clock.now == 10 and clock.sleeps == [2] * 5
    assert len(trace_server.calls) == 5
    assert transport.chat_count == 1 and transport.deleted


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("initial_404", [False, True])
def test_adapters_fetch_trace_server_over_http(arrange_type, initial_404, monkeypatch):
    peer = _Transport(arrange_type)
    queried = []
    clock = _Clock()

    class Handler(BaseHTTPRequestHandler):
        def respond(self, body, content_type="application/json", status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith(("/chat", "/api/v1/message")):
                body = peer.request_bytes("POST", self.path, payload=payload, timeout=5)
                self.respond(body, "text/event-stream")
            else:
                result = peer.request_json("POST", self.path, payload=payload, timeout=5)
                self.respond(json.dumps(result).encode())

        def do_GET(self):
            if self.path.startswith("/api/v1/projects/"):
                queried.append(self.path)
                if initial_404 and len(queried) == 1:
                    self.respond(b"{}", status=404)
                    return
                if self.path.endswith("/llm_requests"):
                    result = LLM
                else:
                    result = deepcopy(DETAIL)
                    result["trace"].update(
                        id=self.path.rsplit("/", 1)[-1],
                        projectId="customer-project",
                        output={"output": "customer answer"},
                    )
            else:
                result = peer.request_json("GET", self.path, timeout=5)
            self.respond(json.dumps(result).encode())

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setenv("AGENTGATE_TRACE_SERVER_URL", origin)
        adapter = _adapter(
            arrange_type,
            None,
            None,
            base_url=origin,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        try:
            result = adapter.wait(adapter.start(_request(arrange_type)), 30)
            trace = result.inline_trace
            assert trace is not None
            assert trace.final_output["output"] == "customer answer"
            assert len(trace.turn_outcomes) == 2
            assert any(
                event["event_type"] == "llm_request"
                for span in trace.spans
                for event in span.events
            )
            expected = [
                f"/api/v1/projects/customer-project/traces/customer-trace-{turn}{suffix}"
                for turn in (1, 2)
                for suffix in ("", "/llm_requests")
            ]
            if initial_404:
                expected.insert(0, expected[0])
            assert [urlparse(path).path for path in queried] == expected
            assert clock.sleeps == ([2] if initial_404 else [])
            assert peer.chat_count == 2
        finally:
            adapter.close()
            server.shutdown()
            thread.join(timeout=5)
    assert peer.deleted is True
