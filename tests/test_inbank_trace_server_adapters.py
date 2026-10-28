from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time
from typing import Any
from unittest.mock import Mock
from urllib.parse import urlparse
from uuid import UUID

import pytest
from test_trace_server_client import DETAIL, Response, sparse_inbank_detail

from agentgate.domain import Case, CaseTurn, TargetRef, TargetSnapshot, TargetType, Trace
from agentgate.integrations.observability.trace_server import InbankTraceSettings, TraceServerClient
from agentgate.integrations.targets.inbank.chatabc import (
    ChatABCSettings,
    InbankChatABCTargetAdapter,
    load_chatabc_settings,
)
from agentgate.integrations.targets.inbank.yunxia import (
    InbankYunxiaTargetAdapter,
    YunxiaSettings,
    load_yunxia_settings,
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
        self, project_id: str, trace_id: str, *, timeout: float = 30,
        customer_task_id: str | None = None,
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
    def __init__(
        self, arrange_type: str, *, references: list[list[Any]] | None = None,
        intent_codes: list[Any] | None = None,
    ) -> None:
        self.arrange_type = arrange_type
        self.references = references
        self.intent_codes = intent_codes
        self.chat_count = 0
        self.request_ids = []
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
        request_id = headers["X-Request-ID"]
        if self.arrange_type != "yunxia":
            assert payload["requestId"] == request_id
        self.request_ids.append(request_id)
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
                "intent_code": (
                    self.intent_codes[self.chat_count - 1]
                    if self.intent_codes is not None else "lookup-intent"
                ),
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
    if "sleep" not in options and "monotonic" not in options:
        clock = _Clock()
        options.update(sleep=clock.sleep, monotonic=clock.monotonic)
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
            YunxiaSettings(**settings, agent_namespace="chatabc"),
            http=transport,
            trace_client=trace_server,
            **options,
        )
    return InbankChatABCTargetAdapter(
        ChatABCSettings(**settings), http=transport, trace_client=trace_server, **options
    )


@pytest.mark.parametrize("arrange_type", ["base", "workflow"])
def test_chatabc_preserves_complete_business_inputs_and_case_sessions(monkeypatch, arrange_type):
    transport = _Transport(arrange_type)
    initializations, chats, payloads = [], [], []
    request_json, request_bytes = transport.request_json, transport.request_bytes

    def capture_json(method, url, **kwargs):
        if url.endswith("/init_session"):
            assert method == "POST" and url.endswith("/chatabc/init_session")
            payloads.append(deepcopy(kwargs["payload"]))
            initializations.append(deepcopy(kwargs["payload"]["data"]))
            return {"resCode": "FAIAG0000", "data": {
                "session_id": f"session-{len(initializations)}"
            }}
        return request_json(method, url, **kwargs)

    def capture_chat(method, url, **kwargs):
        assert method == "POST" and url.endswith("/chatabc/chat")
        payloads.append(deepcopy(kwargs["payload"]))
        chats.append(deepcopy(kwargs["payload"]["data"]))
        return request_bytes(method, url, **kwargs)

    monkeypatch.setattr(transport, "request_json", capture_json)
    monkeypatch.setattr(transport, "request_bytes", capture_chat)
    variables = [{"name": "centreId", "value": "810000"},
                 {"name": "userId", "value": "hk_yat"},
                 {"name": "bankNo", "value": "810000"}]
    inputs = [
        {"user_input_text": "帮我查一下收文"},
        {"user_input_text": "请翻译 acquisition", "menuId": "TRANSLATE",
         "lang_src": "auto_detect", "lang_tar": "fr_FR",
         "form": {"items": [0, False, None, {"amount": 12.5}]},
         "session_id": "business-value-not-transport-session"},
        {"user_input_text": "换一个例子", "intent": "follow-up", "collected_data": {}},
        {"user_input": "查询年休假政策"},
        {"txt": "business text", "menuId": "KNOWLEDGE_QUERY"},
    ]
    initial = (
        {"config_variables": variables}
        if arrange_type == "workflow"
        else {"prompt_variables": variables, "tool_variables": [
            {"name": "catalog", "value": "test", "type": "str"}
        ]}
    )
    request = _request(arrange_type)
    case = Case(name="Structured ChatABC input", initial_state=initial,
                turns=[CaseTurn(id=f"turn-{i}", input=value, extensions=[
                    {"key": "custom_variables", "value": "annotation-not-sent"}
                ]) for i, value in enumerate(inputs)])
    adapter = _adapter(arrange_type, transport, _TraceServer())
    before_ms = int(time() * 1000)
    try:
        first = adapter.wait(adapter.start(replace(request, case=case)), 30)
        second = adapter.wait(adapter.start(replace(
            request, execution_id="execution-2", case=case.model_copy(update={"id": "case-2"})
        )), 30)
    finally:
        adapter.close()
    after_ms = int(time() * 1000)
    assert initializations == [initial] * 2
    assert [chat["session_id"] for chat in chats] == (
        ["session-1"] * len(inputs) + ["session-2"] * len(inputs)
    )
    assert [json.loads(chat["txt"]) for chat in chats] == inputs * 2
    assert all(chat["files"] == [] and chat["stream"] is True for chat in chats)
    assert all(set(chat) == {"session_id", "txt", "files", "stream"} for chat in chats)
    assert len(payloads) == 2 * (len(inputs) + 1)
    assert len({payload["requestId"] for payload in payloads}) == len(payloads)
    for payload in payloads:
        assert set(payload) == {"appId", "trCode", "trVersion", "timestamp", "requestId", "data"}
        assert payload["appId"] == "BDC201704_01"
        assert payload["trCode"] == "AISPNLPCHATBOT"
        assert payload["trVersion"] == "1"
        assert isinstance(payload["timestamp"], int)
        assert before_ms <= payload["timestamp"] <= after_ms
        assert str(UUID(payload["requestId"])) == payload["requestId"]
    for result in (first, second):
        trace = result.inline_trace
        assert [trace.turn_outcomes[turn.id]["input"].to_dict()
                for turn in case.turns] == inputs
        assert trace.final_output["output"] == "customer answer"
        assert {span.attributes["agentgate.turn.id"] for span in trace.spans
                if span.operation_type == "turn"} == {turn.id for turn in case.turns}


@pytest.mark.parametrize("arrange_type", ["base", "workflow"])
def test_chatabc_mixes_text_json_strings_and_business_objects_without_double_encoding(monkeypatch, arrange_type):
    transport = _Transport(arrange_type)
    actual, original = [], transport.request_bytes

    def capture(method, url, **kwargs):
        actual.append(kwargs["payload"]["data"]["txt"])
        return original(method, url, **kwargs)

    monkeypatch.setattr(transport, "request_bytes", capture)
    encoded = '{"user_input_text":"帮我查一下收文"}'
    values = [{"txt": "普通文本"}, {"txt": encoded}, {"txt": "兼容空附件", "files": []},
              {"form": {"enabled": True}}]
    case = Case(name="Mixed inputs", turns=[CaseTurn(input=value) for value in values])
    adapter = _adapter(arrange_type, transport, _TraceServer())
    try:
        adapter.wait(adapter.start(replace(_request(arrange_type), case=case)), 30)
    finally:
        adapter.close()
    assert actual[:3] == ["普通文本", encoded, "兼容空附件"]
    assert json.loads(actual[3]) == values[3]


@pytest.mark.parametrize("variables", [
    ["invalid"], [{"name": " ", "value": "x"}], [{"name": "missing"}],
    [{"name": "same", "value": 0}, {"name": "same", "value": False}],
])
def test_workflow_rejects_malformed_initial_variables_before_remote_calls(variables):
    adapter = _adapter("workflow", _Transport("workflow"), _TraceServer())
    request = _request("workflow")
    case = request.case.model_copy(update={
        "initial_state": request.case.initial_state.__class__({"config_variables": variables})
    })
    try:
        with pytest.raises(TargetExecutionError, match="config_variables"):
            adapter.start(replace(request, case=case))
    finally:
        adapter.close()


@pytest.mark.parametrize("arrange_type", ["yunxia"])
def test_business_json_does_not_enable_other_agent_protocols(arrange_type):
    request = _request(arrange_type)
    case = Case(name="Unsupported JSON", turns=[CaseTurn(input={"menuId": "QUERY"})])
    adapter = _adapter(arrange_type, _Transport(arrange_type), _TraceServer())
    try:
        with pytest.raises(TargetExecutionError, match="input"):
            adapter.start(replace(request, case=case))
    finally:
        adapter.close()


@pytest.mark.parametrize("custom_session", [None, "customer-session"])
def test_yunxia_native_inputs_keep_txt_optional_defaults_and_per_turn_fields(monkeypatch, custom_session):
    transport = _Transport("yunxia")
    messages, calls = [], []
    original_json, original_bytes = transport.request_json, transport.request_bytes

    def capture_json(method, url, **kwargs):
        calls.append(url)
        return original_json(method, url, **kwargs)

    def capture_message(method, url, **kwargs):
        messages.append(deepcopy(kwargs["payload"]))
        return original_bytes(method, url, **kwargs)

    monkeypatch.setattr(transport, "request_json", capture_json)
    monkeypatch.setattr(transport, "request_bytes", capture_message)
    encoded = '{"user_input_text":"查询年休假政策"}'
    inputs = [
        {"txt": "只填文本"},
        {"txt": encoded},
        {"txt": "历史", "appHistory": [{"role": "user", "content": "之前的问题"}]},
        {"txt": "客户", "custID": "hk_yat"},
        {"txt": "变量", "config_variables": [{"name": "centreId", "value": "810000"}]},
        {"txt": "完整", "appHistory": {"role": "user", "content": "history"},
         "custID": "another-customer", "config_variables": [{"open": [0, False, None]}]},
        {"txt": "选填留空", "sessionId": None, "custID": None,
         "appHistory": None, "config_variables": None},
    ]
    if custom_session:
        inputs[0]["sessionId"] = custom_session
    case = Case(id="native-case", name="Native Yunxia input", turns=[
        CaseTurn(id=f"turn-{index}", input=value) for index, value in enumerate(inputs)
    ])
    adapter = _adapter("yunxia", transport, _TraceServer())
    try:
        first = adapter.wait(adapter.start(replace(_request("yunxia"), case=case)), 30)
        second = adapter.wait(adapter.start(replace(_request("yunxia"),
            execution_id="execution-2", case=Case(id="other-case", name="Only txt",
                turns=[CaseTurn(input={"txt": "另一条用例"})])
        )), 30)
    finally:
        adapter.close()
    assert not any(url.endswith("/init_session") for url in calls)
    assert [message["txt"] for message in messages] == [value["txt"] for value in inputs] + ["另一条用例"]
    session_id = custom_session or case.id
    assert [message["sessionId"] for message in messages] == [session_id] * len(inputs) + ["other-case"]
    prefix = session_id
    assert messages[0]["custID"].startswith(prefix)
    suffix = messages[0]["custID"][len(prefix):]
    assert UUID(suffix).hex == suffix
    assert messages[3]["custID"] == f"hk_yat{suffix}"
    assert messages[5]["custID"] == f"another-customer{suffix}"
    assert messages[-1]["custID"] == f"other-case{suffix}"
    assert messages[2]["appHistory"] == inputs[2]["appHistory"]
    assert messages[5]["appHistory"] == [inputs[5]["appHistory"]]
    assert messages[4]["config_variables"] == inputs[4]["config_variables"]
    assert messages[5]["config_variables"] == inputs[5]["config_variables"]
    for index in (0, 1, 6, len(inputs)):
        assert messages[index]["appHistory"] == messages[index]["config_variables"] == []
    for message in messages:
        assert set(message) == {"sessionId", "custID", "txt", "appHistory", "config_variables",
                                "executionMode", "stream", "debugTrace"}
        assert message["executionMode"] == "execute"
        assert message["stream"] is message["debugTrace"] is True
    assert [first.inline_trace.turn_outcomes[turn.id]["input"].to_dict()
            for turn in case.turns] == inputs
    assert second.inline_trace.final_output["message"] == "customer answer"


@pytest.mark.parametrize("customer_id", [None, "hk_yat"])
def test_yunxia_customer_id_directly_appends_one_uuid_per_run(monkeypatch, customer_id):
    turn_input = {"txt": "查询", "sessionId": "same-session"}
    if customer_id is not None:
        turn_input["custID"] = customer_id
    case = Case(id="same-case", name="Same customer", turns=[
        CaseTurn(id=f"turn-{index}", input=turn_input) for index in range(2)
    ])
    suffixes = []
    for index in range(2):
        transport = _Transport("yunxia")
        capture = Mock(wraps=transport.request_bytes)
        monkeypatch.setattr(transport, "request_bytes", capture)
        adapter = _adapter("yunxia", transport, _TraceServer())
        try:
            request = replace(_request("yunxia"), run_id=f"run-1234567{index}", case=case)
            result = adapter.wait(adapter.start(request), 30)
        finally:
            adapter.close()
        assert result.error is None
        messages = [call.kwargs["payload"] for call in capture.call_args_list]
        assert len(messages) == 2
        prefix = customer_id or "same-session"
        suffix = messages[0]["custID"][len(prefix):]
        assert UUID(suffix).hex == suffix
        assert all(message["custID"] == prefix + suffix for message in messages)
        assert all(message["sessionId"] == "same-session" for message in messages)
        suffixes.append(suffix)
    assert suffixes[0] != suffixes[1]


@pytest.mark.parametrize("trace_available", [True, False])
@pytest.mark.parametrize("message_kind", [
    "object", "minimal_object", "empty_object", "plain_text", "json_text",
    "array", "number", "boolean", "null",
])
def test_yunxia_final_output_preserves_each_last_message_without_span_copy(
    monkeypatch, trace_available, message_kind,
):
    transport = _Transport("yunxia")
    server = _TraceServer()
    request_bytes = transport.request_bytes
    messages = []
    expected_outputs = []
    for index in range(2):
        output = f"customer answer {index}"
        if message_kind == "object":
            message = {
                "status": "completed", "message": output,
                "intent_code": "lookup-intent", "slots": {"city": "上海"},
                "workflow_calls": [{"id": "workflow-one"}],
                "business_result": {"turn": index, "details": [0, False, None, {"任意字段": "保留"}]},
                "output": {"customer_owned": index},
            }
        elif message_kind == "minimal_object":
            message = {"business_result": {"turn": index, "enabled": False}}
        elif message_kind == "empty_object":
            message = {}
        elif message_kind in {"plain_text", "json_text"}:
            message = output
        else:
            message = {
                "array": [index, False, None, {"业务字段": "保留"}],
                "number": index,
                "boolean": False,
                "null": None,
            }[message_kind]
        raw = output if message_kind == "plain_text" else json.dumps(
            message, ensure_ascii=False, separators=(",", ":"),
        )
        messages.append(raw)
        expected_outputs.append(message if isinstance(message, dict) else {"output": message})

    def message_response(method, url, **kwargs):
        wire = request_bytes(method, url, **kwargs)
        _, _, rest = wire.partition(b"\n\n")
        interim = 'event: message\ndata: {"message":"interim answer"}\n\n'
        final = f"event: message\ndata: {messages[transport.chat_count - 1]}\n\n"
        return (interim + final).encode() + rest

    monkeypatch.setattr(transport, "request_bytes", message_response)
    if not trace_available:
        def reject(*_args, **_kwargs):
            raise TargetExecutionError("rejected", "trace not integrated")
        monkeypatch.setattr(server, "fetch_events", reject)
    adapter = _adapter("yunxia", transport, server)
    try:
        result = adapter.wait(adapter.start(_request("yunxia")), 30)
        trace = result.inline_trace
    finally:
        adapter.close()
    assert result.error is None
    restored = Trace.model_validate_json(trace.model_dump_json())
    assert restored == trace
    assert trace.final_output.to_dict() == expected_outputs[-1]
    assert len(trace.spans) == (4 if trace_available else 2) + (2 if message_kind == "object" else 0)
    assert [span.sequence for span in trace.spans] == list(range(len(trace.spans)))
    for index, turn_id in enumerate(("turn-1", "turn-2")):
        scoped = restored.for_turn(turn_id)
        roots = [span for span in scoped.spans if span.operation_type == "turn"]
        assert len(roots) == 1
        root = roots[0]
        assert root.attributes["inbank.evidence_mode"] == (
            "trace_server" if trace_available else "output_only"
        )
        assert scoped.final_output.to_dict() == expected_outputs[index]
        assert restored.turn_outcomes[turn_id]["output"].to_dict() == expected_outputs[index]
        assert all("inbank.response.message_data" not in span.attributes
                   for span in scoped.spans)
        routes = [span for span in scoped.spans if span.operation_type == "routing"]
        assert [span.attributes["selected_skill"] for span in routes] == (
            ["lookup-intent"] if message_kind == "object" else []
        )


@pytest.mark.parametrize("inputs", [
    [{"txt": " "}], [{"appHistory": []}], [{"txt": 1}],
    [{"txt": "hello", "custID": 123}], [{"txt": "hello", "sessionId": []}],
    [{"txt": "hello", "config_variables": {}}],
    [{"txt": "hello", "files": ["unsupported"]}],
    [{"txt": "hello", "app_history": []}],
    [{"txt": "one", "sessionId": "one"}, {"txt": "two", "sessionId": "two"}],
])
def test_yunxia_rejects_invalid_native_inputs_before_remote_calls(monkeypatch, inputs):
    transport = _Transport("yunxia")
    remote = Mock(wraps=transport.request_json)
    monkeypatch.setattr(transport, "request_json", remote)
    adapter = _adapter("yunxia", transport, _TraceServer())
    try:
        with pytest.raises(TargetExecutionError):
            adapter.start(replace(_request("yunxia"), case=Case(name="Invalid input", turns=[
                CaseTurn(input=value) for value in inputs
            ])))
        remote.assert_not_called()
    finally:
        adapter.close()


@pytest.mark.parametrize("initial", [{"customer_id": "test-user"}, {"config_variables": []}])
def test_yunxia_does_not_read_request_fields_from_case_initialization(initial):
    request = _request("yunxia")
    case = Case(name="Fields belong in input", initial_state=initial,
                turns=[CaseTurn(input={"txt": "hello"})])
    adapter = _adapter("yunxia", _Transport("yunxia"), _TraceServer())
    try:
        with pytest.raises(TargetExecutionError, match="input"):
            adapter.start(replace(request, case=case))
    finally:
        adapter.close()


@pytest.mark.parametrize("arrange_type", ["base", "workflow"])
@pytest.mark.parametrize("value", [
    {}, {"txt": " "}, {"txt": 7}, {"txt": "hello", "files": ["file"]},
    {"txt": "hello", "files": {}}, {"files": []},
])
def test_chatabc_rejects_empty_text_and_attachment_envelopes(value, arrange_type):
    adapter = _adapter(arrange_type, _Transport(arrange_type), _TraceServer())
    try:
        with pytest.raises(ValueError if not value else TargetExecutionError):
            adapter.start(replace(_request(arrange_type), case=Case(
                name="Invalid envelope", turns=[CaseTurn(input=value)]
            )))
    finally:
        adapter.close()


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
        ("customer-agent-id", request_id) for request_id in transport.request_ids for _ in range(3)
    ]
    assert len(set(transport.request_ids)) == 2
    assert len(result.inline_trace.spans) == (6 if arrange_type == "yunxia" else 4)
    for sequence, turn in enumerate(request.case.turns, 1):
        outcome = result.inline_trace.turn_outcomes[turn.id]
        assert outcome["input"] == turn.input
        assert outcome["output"]["message" if arrange_type == "yunxia" else "output"] == "customer answer"
        assert outcome["state"] == {"execution_status": "success"}
        if arrange_type == "yunxia":
            assert outcome["output"]["intent_code"] == "lookup-intent"
            assert outcome["output"]["slots"] == {"city": "上海"}
            assert outcome["output"]["workflow_calls"][0]["id"] == "workflow-one"
        turn_trace = result.inline_trace.for_turn(turn.id)
        assert turn_trace.spans[0].attributes["trace_sdk.trace_id"] == (
            transport.request_ids[sequence - 1]
        )
        assert turn_trace.spans[0].attributes["trace_sdk.replay"] is False
        tools = [span for span in turn_trace.spans if span.operation_type == "tool"]
        assert len(tools) == 1
        assert tools[0].name == "lookup"
        assert tools[0].attributes["arguments"] == {"city": "上海"}


@pytest.mark.parametrize("trace_available", [True, False])
@pytest.mark.parametrize("second_intent", ["balance", "transfer", None, "", 123])
def test_yunxia_uses_each_message_intent_without_reading_skill_files(
    monkeypatch, trace_available, second_intent,
):
    from test_rule_routing import evaluator_spec

    from agentgate.domain import Equals, SkillRouteExpectation
    from agentgate.evaluator.executor import execute_evaluators
    from agentgate.evaluator.rule.routing import SkillRoutingEvaluator

    transport = _Transport("yunxia", intent_codes=["balance", second_intent])
    server = _TraceServer()
    if not trace_available:
        monkeypatch.setattr(server, "fetch_events", Mock(side_effect=TargetExecutionError(
            "rejected", "trace not integrated",
        )))
    request = _request("yunxia")
    turns = tuple(turn.model_copy(update={"expectations": (
        SkillRouteExpectation(condition=Equals(expected=expected)),
    )}) for turn, expected in zip(request.case.turns, ("balance", "transfer"), strict=True))
    request = replace(request, case=request.case.model_copy(update={"turns": turns}))
    adapter = _adapter("yunxia", transport, server)
    try:
        result = adapter.wait(adapter.start(request), 30)
    finally:
        adapter.close()
    assert result.error is None
    assert transport.chat_count == 2 and transport.deleted
    trace = result.inline_trace
    assert all(span.name != "read_file" for span in trace.spans)
    for turn, intent in zip(turns, ("balance", second_intent), strict=True):
        scoped = trace.for_turn(turn.id)
        assert scoped.final_output["intent_code"] == intent
        assert scoped.final_output["message"] == "customer answer"
        assert scoped.spans[0].attributes["inbank.evidence_mode"] == (
            "trace_server" if trace_available else "output_only"
        )
        routes = [s for s in scoped.spans if s.operation_type == "routing"]
        assert [s.attributes["selected_skill"] for s in routes] == (
            [intent] if isinstance(intent, str) and intent else []
        )
        assert all(s.attributes["inbank.skill_selection"]["source_path"] == "message.intent_code"
                   for s in routes)
    evaluation = execute_evaluators(
        request.case, trace, (evaluator_spec(),), {("skill_routing", "1"): SkillRoutingEvaluator()},
    )[0]
    second_outcome = "pass" if second_intent == "transfer" else "fail"
    assert [c.turn_id for c in evaluation.checks] == [turn.id for turn in turns]
    assert [c.outcome.value for c in evaluation.checks] == ["pass", second_outcome]


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("missing_turns", [(), (1,), (2,), (1, 2)])
def test_execution_state_reaches_rules_without_leaking_across_turns(arrange_type, missing_turns, monkeypatch):
    from test_rule_state import evaluator_spec

    from agentgate.domain import Equals, StateExpectation
    from agentgate.evaluator.executor import execute_evaluators
    from agentgate.evaluator.rule.state import FinalStateEvaluator

    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    fetch = trace_server.fetch_events

    def selective_fetch(project_id, trace_id, *, timeout, customer_task_id):
        if transport.chat_count in missing_turns:
            raise TargetExecutionError("unavailable", "inbank trace not found")
        events = fetch(project_id, trace_id, timeout=timeout)
        # A failed tool or contradictory output must not replace the root status.
        events[0]["output"] = {"execution_status": "error"}
        events[1]["status"] = "error"
        return events

    monkeypatch.setattr(trace_server, "fetch_events", selective_fetch)
    clock = _Clock()
    adapter = _adapter(arrange_type, transport, trace_server, sleep=clock.sleep, monotonic=clock.monotonic)
    request = _request(arrange_type)
    turns = tuple(
        turn.model_copy(update={"expectations": (StateExpectation(
            path="execution_status", condition=Equals(expected=expected),
        ),)})
        for turn, expected in zip(request.case.turns, ("success", "error"), strict=True)
    )
    request = replace(request, case=request.case.model_copy(update={"turns": turns}))
    try:
        trace = adapter.wait(adapter.start(request), 30).inline_trace
    finally:
        adapter.close()

    result = execute_evaluators(
        request.case, trace, (evaluator_spec(),), {("final_state", "1"): FinalStateEvaluator()},
    )[0]
    assert len(result.checks) == 2
    for index, (turn, check) in enumerate(zip(turns, result.checks, strict=True), 1):
        scoped = trace.for_turn(turn.id)
        state = {} if index in missing_turns else {"execution_status": "success"}
        assert scoped.final_state == trace.turn_outcomes[turn.id]["state"] == state
        assert check.turn_id == turn.id
        if index in missing_turns:
            assert check.outcome.value == "not_applicable"
            assert check.actual_missing
        else:
            assert check.outcome.value == ("pass" if index == 1 else "fail")
            assert check.actual == "success"
            assert not check.actual_missing
        assert scoped.final_output["message" if arrange_type == "yunxia" else "output"] == "customer answer"
        if arrange_type == "yunxia":
            assert scoped.final_output["intent_code"] == "lookup-intent"
    assert trace.final_state == trace.turn_outcomes[turns[-1].id]["state"]
    assert transport.chat_count == len(set(transport.request_ids)) == 2
    assert transport.deleted


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
def test_sse_trace_references_are_not_used_for_query_binding(arrange_type, references) -> None:
    transport = _Transport(arrange_type, references=references + [[]])
    trace_server = _TraceServer()
    adapter = _adapter(arrange_type, transport, trace_server)

    try:
        result = adapter.wait(adapter.start(_request(arrange_type)), 30)
    finally:
        adapter.close()

    trace = result.inline_trace
    assert trace.final_output["message" if arrange_type == "yunxia" else "output"] == "customer answer"
    assert len(trace.turn_outcomes) == transport.chat_count == 2
    assert len(trace.spans) == (6 if arrange_type == "yunxia" else 4)
    assert all(span.attributes["inbank.evidence_mode"] == "trace_server"
               for span in trace.spans if span.operation_type == "turn")
    assert adapter.get_status("execution-1") is CaseExecutionStatus.COMPLETED
    assert [call[:2] for call in trace_server.calls] == [
        ("customer-agent-id", request_id) for request_id in transport.request_ids for _ in range(3)
    ]
    assert transport.deleted is True


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("trace_available", [True, False])
def test_trace_or_output_fallback_does_not_hide_later_chat_failure(arrange_type, trace_available, monkeypatch):
    transport = _Transport(arrange_type, references=[[], []])
    chat = transport.request_bytes

    def fail_second_chat(*args, **kwargs):
        if transport.chat_count == 1:
            raise TargetExecutionError("rejected", "customer chat rejected second Turn")
        return chat(*args, **kwargs)

    monkeypatch.setattr(transport, "request_bytes", fail_second_chat)
    trace_server = _TraceServer()
    if not trace_available:
        def reject(*_args, **_kwargs):
            raise TargetExecutionError("rejected", "trace not integrated")
        monkeypatch.setattr(trace_server, "fetch_events", reject)
    adapter = _adapter(arrange_type, transport, trace_server)
    try:
        result = adapter.wait(adapter.start(_request(arrange_type)), 30)
        assert result.error.code == "rejected"
        assert "second Turn" in result.error.message
        assert result.inline_trace.for_turn("turn-1").final_output[
            "message" if arrange_type == "yunxia" else "output"
        ] == "customer answer"
        failed = result.inline_trace.for_turn("turn-2")
        assert failed.final_output == {}
        assert not any(span.operation_type == "routing" for span in failed.spans)
        assert failed.spans[0].status == "error"
        assert failed.spans[0].attributes["agentgate.execution.error"]["code"] == "rejected"
        assert adapter.get_status("execution-1") is CaseExecutionStatus.FAILED
    finally:
        adapter.close()
    assert transport.deleted is True


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("failure", ["unavailable", "not-found", "empty", "wrong-trace", "failed-trace"])
def test_trace_failure_keeps_output_without_synthetic_execution_spans(arrange_type, failure, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    fetch = trace_server.fetch_events

    def failing_fetch(project_id, trace_id, *, timeout, customer_task_id):
        if failure == "unavailable":
            raise TargetExecutionError("unavailable", "trace server request failed")
        if failure == "not-found":
            raise TargetExecutionError("unavailable", "inbank trace not found")
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
        result = adapter.wait(adapter.start(_request(arrange_type)), 30)
        assert adapter.get_status("execution-1") is CaseExecutionStatus.COMPLETED
        trace = result.inline_trace
        assert len(trace.spans) == (4 if arrange_type == "yunxia" else 2)
        assert not any(span.operation_type in {"tool", "llm", "skill"} for span in trace.spans)
        assert trace.final_output["message" if arrange_type == "yunxia" else "output"] == "customer answer"
        assert trace.final_state == {}
        assert all(outcome["state"] == {} for outcome in trace.turn_outcomes.values())
        roots = [span for span in trace.spans if span.operation_type == "turn"]
        for span, request_id in zip(roots, transport.request_ids, strict=True):
            assert span.operation_type == "turn"
            assert span.attributes["inbank.evidence_mode"] == "output_only"
            assert span.attributes["inbank.request_id"] == request_id
            assert span.attributes["inbank.trace_query_project_id"] == "customer-agent-id"
            assert span.attributes["inbank.trace_fallback_reason"] == (
                "timeout" if failure in {"unavailable", "not-found"} else "protocol_error"
            )
            assert "trace_sdk.trace_id" not in span.attributes
        warnings = [r for r in caplog.records if r.msg.startswith("inbank_trace_fallback ")]
        assert len(warnings) == 2 and all(r.levelno == logging.WARNING for r in warnings)
        assert all("evidence_mode=output_only" in r.getMessage() for r in warnings)
        if failure == "not-found":
            assert all("trace_not_found" in r.getMessage() for r in warnings)
    finally:
        adapter.close()
    assert transport.chat_count == 2
    assert transport.deleted is True


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize(
    "pending",
    ["404", "409", "429", "unavailable", "running", "spans", "output", "duration", "span-running",
     "no-spans", "started-at", "span-duration", "span-started-at"],
)
def test_trace_readiness_retries_without_replaying_chat(arrange_type, pending, monkeypatch):
    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    fetch = trace_server.fetch_events
    clock = _Clock()

    def delayed(project_id, trace_id, *, timeout, customer_task_id):
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
            elif pending == "started-at":
                events[0]["started_at"] = None
            elif pending == "span-duration":
                events[1]["duration_ms"] = None
            elif pending == "span-started-at":
                events[1]["started_at"] = None
            elif pending == "no-spans":
                events.pop()
                events[0].pop("span_count")
        return events

    monkeypatch.setattr(trace_server, "fetch_events", delayed)
    adapter = _adapter(
        arrange_type, transport, trace_server, sleep=clock.sleep, monotonic=clock.monotonic
    )
    try:
        result = adapter.wait(adapter.start(_request(arrange_type)), 30)
        assert result.inline_trace.final_output[
            "message" if arrange_type == "yunxia" else "output"
        ] == "customer answer"
        assert [call[1] for call in trace_server.calls] == [
            *([transport.request_ids[0]] * 5), *([transport.request_ids[1]] * 3),
        ]
        assert clock.sleeps == [2] * 6
        assert [call[2] for call in trace_server.calls] == [10, 8, 6, 4, 2, 10, 8, 6]
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
        trace = adapter.wait(adapter.start(_request(arrange_type)), 30).inline_trace
        assert all(s.attributes["inbank.trace_fallback_detail"] == f"trace server HTTP {status}"
                   for s in trace.spans if s.operation_type == "turn")
        assert adapter.get_status("execution-1") is CaseExecutionStatus.COMPLETED
    finally:
        adapter.close()
    assert len(calls) == 2 and clock.sleeps == []
    assert transport.chat_count == 2 and transport.deleted


@pytest.mark.parametrize("arrange_type", ["base", "workflow"])
@pytest.mark.parametrize("failed_turn", [None, 1, 2])
def test_chatabc_query_ids_logged_before_trace_rejection(
    arrange_type, failed_turn, caplog, monkeypatch,
):
    caplog.set_level(logging.INFO)
    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    fetch = trace_server.fetch_events
    request = _request(arrange_type)

    def query(project_id, trace_id, **kwargs):
        records = [record for record in caplog.records
                   if record.msg.startswith("inbank_trace_query ")]
        assert len(records) == transport.chat_count
        record = records[-1]
        assert record.levelno == logging.INFO
        message = record.getMessage()
        assert f"project_id={project_id!r}" in message
        assert f"request_id={trace_id!r}" in message
        assert trace_id == transport.request_ids[-1]
        assert f"arrange_type={arrange_type}" in message
        assert f"run_id={request.run_id!r}" in message
        assert f"case_id={request.case.id!r}" in message
        assert f"turn_id={request.case.turns[transport.chat_count - 1].id!r}" in message
        if transport.chat_count == failed_turn:
            raise TargetExecutionError("rejected", "inbank trace server rejected the query")
        return fetch(project_id, trace_id, **kwargs)

    monkeypatch.setattr(trace_server, "fetch_events", query)
    adapter = _adapter(arrange_type, transport, trace_server)
    try:
        trace = adapter.wait(adapter.start(request), 30).inline_trace
        assert adapter.get_status(request.execution_id) is CaseExecutionStatus.COMPLETED
        for index, turn in enumerate(request.case.turns, 1):
            turn_trace = trace.for_turn(turn.id)
            expected = "output_only" if index == failed_turn else "trace_server"
            assert turn_trace.spans[0].attributes["inbank.evidence_mode"] == expected
            assert sum(s.operation_type == "tool" for s in turn_trace.spans) == (index != failed_turn)
    finally:
        adapter.close()

    assert transport.chat_count == 2
    assert len(set(transport.request_ids)) == transport.chat_count
    assert transport.deleted is True
    completed = [record for record in caplog.records
                 if record.msg.startswith("inbank_turn_complete ")]
    assert len(completed) == 2


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("problem", [
    "cycle", "cycle-with-boundary", "unknown-observation", "duplicate-span", "invalid-duration", "empty",
])
def test_sparse_root_never_repairs_or_fabricates_source_spans(arrange_type, problem):
    transport = _Transport(arrange_type)
    clock = _Clock()

    def opener(request, **_kwargs):
        detail = sparse_inbank_detail(DETAIL, "12345678")
        request_id = request.full_url.rsplit("/", 1)[1]
        detail["trace"]["id"] = request_id
        detail["spans"][0]["traceId"] = request_id
        if problem in {"cycle", "cycle-with-boundary"}:
            detail["spans"][0]["parentSpanId"] = "sp-1"
            if problem == "cycle-with-boundary":
                detail["spans"].append(dict(detail["spans"][0], id="boundary", eventId="boundary-event",
                                            parentSpanId="not-returned"))
        elif problem == "unknown-observation":
            detail["spans"][0]["parentSpanId"] = "not-returned"
            detail["observations"][0]["spanId"] = "not-returned"
        elif problem == "duplicate-span":
            detail["spans"][0]["parentSpanId"] = "not-returned"
            detail["spans"].append(dict(detail["spans"][0], eventId="duplicate-event"))
        elif problem == "invalid-duration":
            detail["spans"][0]["durationMs"] = -1
        else:
            detail["spans"] = []
            detail["observations"] = []
        return Response({"code": "0", "data": detail})

    trace_server = TraceServerClient(
        "https://trace.example", mode="inbank", token="test-token",
        source_timezone="Asia/Shanghai", opener=opener,
    )
    adapter = _adapter(
        arrange_type, transport, trace_server, sleep=clock.sleep, monotonic=clock.monotonic,
    )
    try:
        trace = adapter.wait(adapter.start(_request(arrange_type)), 30).inline_trace
        assert len(trace.spans) == (4 if arrange_type == "yunxia" else 2)
        assert not any(s.operation_type in {"tool", "llm", "skill"} for s in trace.spans)
        roots = [s for s in trace.spans if s.operation_type == "turn"]
        assert len(roots) == 2
        assert all(s.attributes["inbank.evidence_mode"] == "output_only" for s in roots)
        assert all(s.attributes["inbank.trace_fallback_reason"] == (
            "timeout" if problem == "empty" else "protocol_error") for s in roots)
    finally:
        adapter.close()
    assert transport.deleted


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
def test_export_boundary_keeps_seven_real_spans_and_observations_per_turn(arrange_type):
    """Synthetic structural fixture from screenshots, not the original private payload."""
    transport = _Transport(arrange_type)
    queried = []
    rows = [
        ("context-first", "model-first", "router.context.window", "context", None),
        ("llm-first", "model-first", "RouterChatOpenAI", "llm", None),
        ("model-first", "outer-first", "model", "chain", None),
        ("read-skill", "tools", "read_file", "tool", "read_file"),
        ("tools", "outer-first", "tools", "chain", None),
        ("context-second", "outer-second", "router.context.window", "context", None),
        ("llm-second", "outer-second", "RouterChatOpenAI", "llm", None),
    ]

    def opener(request, **_kwargs):
        detail = sparse_inbank_detail(DETAIL, "12345678")
        request_id = request.full_url.rsplit("/", 1)[1]
        queried.append(request_id)
        detail["trace"]["id"] = request_id
        template = detail["spans"][0]
        detail["spans"] = [
            dict(template, id=sid, eventId=f"event-{sid}", traceId=request_id,
                 parentSpanId=parent, name=name, spanType=kind, toolName=tool)
            for sid, parent, name, kind, tool in rows
        ]
        detail["observations"] = [
            dict(DETAIL["observations"][0], id=f"observation-{sid}", spanId=sid)
            for sid in ("llm-first", "llm-second")
        ]
        return Response({"code": "0", "data": detail})

    trace_server = TraceServerClient(
        "https://trace.example", mode="inbank", token="test-token",
        source_timezone="Asia/Shanghai", opener=opener,
    )
    adapter = _adapter(arrange_type, transport, trace_server)
    request = _request(arrange_type)
    try:
        result = adapter.wait(adapter.start(request), 30)
    finally:
        adapter.close()
    assert queried == [request_id for request_id in transport.request_ids for _ in range(3)]
    assert len(queried) == 6 and transport.deleted
    for turn in request.case.turns:
        spans = result.inline_trace.for_turn(turn.id).spans
        # Seven source spans, the turn wrapper, and Yunxia's reported route.
        assert len(spans) == (9 if arrange_type == "yunxia" else 8)
        wrapper = next(s for s in spans if s.operation_type == "turn")
        assert wrapper.attributes["inbank.evidence_mode"] == "trace_server"
        assert wrapper.attributes["inbank.trace_source"]["unreturned_parent_span_ids"] == (
            "outer-first", "outer-second",
        )
        by_source = {s.attributes["trace_sdk.span_id"]: s for s in spans
                     if "trace_sdk.span_id" in s.attributes}
        assert set(by_source) == {row[0] for row in rows}
        for sid, parent, *_ in rows:
            span = by_source[sid]
            if parent in by_source:
                assert span.parent_span_id == by_source[parent].span_id
            else:
                assert span.parent_span_id == wrapper.span_id
                assert span.attributes["trace_sdk.metadata"]["inbank.original_parent_span_id"] == parent
                assert span.attributes["trace_sdk.metadata"]["inbank.parent_span_not_returned"] is True
        assert by_source["read-skill"].operation_type == "tool"
        assert sum(len(span.events) for span in spans) == 2
        assert result.inline_trace.turn_outcomes[turn.id]["output"][
            "message" if arrange_type == "yunxia" else "output"
        ] == "customer answer"


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("pending", ["404", "running"])
def test_trace_readiness_deadline_falls_back_and_cleans_up(
    arrange_type, pending, monkeypatch
):
    transport = _Transport(arrange_type)
    trace_server = _TraceServer()
    fetch = trace_server.fetch_events
    clock = _Clock()

    def delayed(project_id, trace_id, *, timeout, customer_task_id):
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
        trace = adapter.wait(adapter.start(_request(arrange_type)), 30).inline_trace
        roots = [s for s in trace.spans if s.operation_type == "turn"]
        assert len(roots) == 2
        assert all(s.attributes["inbank.evidence_mode"] == "output_only" for s in roots)
        assert all("not ready within 10s" in s.attributes["inbank.trace_fallback_detail"] for s in roots)
        assert adapter.get_status("execution-1") is CaseExecutionStatus.COMPLETED
    finally:
        adapter.close()
    assert clock.now == 20 and clock.sleeps == [2] * 10
    assert len(trace_server.calls) == 10
    assert transport.chat_count == 2 and transport.deleted


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("missing_turn", [1, 2])
def test_each_turn_preserves_its_own_evidence_mode(arrange_type, missing_turn, monkeypatch):
    transport = _Transport(arrange_type)
    server = _TraceServer()
    fetch = server.fetch_events

    def query(*args, **kwargs):
        if transport.chat_count == missing_turn:
            raise TargetExecutionError("rejected", "trace not integrated")
        return fetch(*args, **kwargs)

    monkeypatch.setattr(server, "fetch_events", query)
    adapter = _adapter(arrange_type, transport, server)
    request = _request(arrange_type)
    try:
        trace = adapter.wait(adapter.start(request), 30).inline_trace
        for index, turn in enumerate(request.case.turns, 1):
            evidence = trace.for_turn(turn.id)
            assert evidence.spans[0].attributes["inbank.evidence_mode"] == (
                "output_only" if index == missing_turn else "trace_server"
            )
            assert sum(s.operation_type == "tool" for s in evidence.spans) == (index != missing_turn)
            assert len(evidence.spans) == 1 + (index != missing_turn) + (arrange_type == "yunxia")
            assert evidence.final_output[
                "message" if arrange_type == "yunxia" else "output"
            ] == "customer answer"
        assert [s.sequence for s in trace.spans] == list(range(len(trace.spans)))
    finally:
        adapter.close()
    assert transport.chat_count == 2 and transport.deleted


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("code", ["cancelled", "invalid_request"])
def test_trace_fallback_does_not_swallow_cancellation_or_configuration_errors(arrange_type, code, monkeypatch):
    transport = _Transport(arrange_type)
    server = _TraceServer()

    def query(*_args, **_kwargs):
        raise TargetExecutionError(code, "stop execution")

    monkeypatch.setattr(server, "fetch_events", query)
    adapter = _adapter(arrange_type, transport, server)
    try:
        with pytest.raises(TargetExecutionError, match="stop execution"):
            adapter.start(_request(arrange_type))
    finally:
        adapter.close()
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
                body = peer.request_bytes("POST", self.path, payload=payload, headers=self.headers, timeout=5)
                self.respond(body, "text/event-stream")
            else:
                result = peer.request_json("POST", self.path, payload=payload, timeout=5)
                self.respond(json.dumps(result).encode())

        def do_GET(self):
            if self.path.startswith("/web/agent_trace_otel/projects/"):
                queried.append(self.path)
                assert self.headers["Authorization"] == "Bearer bank-task-token"
                if initial_404 and len(queried) == 1:
                    self.respond(b"{}", status=404)
                    return
                detail = deepcopy(DETAIL)
                detail["trace"].update(
                    id=self.path.rsplit("/", 1)[-1],
                    projectId="customer-agent-id",
                    output={"messages": [{"type": "ai", "content": "raw trace answer"}]},
                )
                for span in detail["spans"]:
                    span["traceId"] = detail["trace"]["id"]
                result = {"code": "0", "data": detail}
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
            TraceServerClient(origin, mode="inbank", token="bank-task-token"),
            base_url=origin,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        try:
            result = adapter.wait(adapter.start(_request(arrange_type)), 30)
            trace = result.inline_trace
            assert trace is not None
            assert trace.final_output["message" if arrange_type == "yunxia" else "output"] == "customer answer"
            assert len(trace.turn_outcomes) == 2
            assert any(
                event["event_type"] == "observation"
                for span in trace.spans
                for event in span.events
            )
            expected = [
                f"/web/agent_trace_otel/projects/customer-agent-id/traces/{request_id}"
                for request_id in peer.request_ids for _ in range(3)
            ]
            if initial_404:
                expected.insert(0, expected[0])
            assert [urlparse(path).path for path in queried] == expected
            assert clock.sleeps == [2] * (5 if initial_404 else 4)
            assert peer.chat_count == 2
        finally:
            adapter.close()
            server.shutdown()
            thread.join(timeout=5)
    assert peer.deleted is True


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
def test_adapted_multicase_multiturn_evidence_reaches_rules_judge_and_saved_report(tmp_path, arrange_type):
    from test_inbank_evidence import skill_detail
    from test_judge_evaluator import RecordingModel, judge_spec

    from agentgate.application import ResultReader, RunManagement
    from agentgate.application.dataset_management import DatasetManagement
    from agentgate.application.evaluator_management import build_default_evaluator_management
    from agentgate.domain import TargetDescriptor
    from agentgate.integrations.observability.trace_server import detail_to_events
    from agentgate.integrations.targets.inbank.chatabc import resolve_chatabc_trace
    from agentgate.integrations.targets.inbank.yunxia import resolve_yunxia_trace
    from agentgate.storage.sqlite import SQLiteRepository

    class SkillTraceServer(_TraceServer):
        def fetch_events(self, project_id, trace_id, *, timeout=30, customer_task_id=None):
            self.calls.append((project_id, trace_id, timeout))
            sequence = len({call[1] for call in self.calls})
            detail = skill_detail(trace_id, (f"skill-{sequence}",))
            detail["trace"]["projectId"] = project_id
            # Two provisional snapshots precede the tool's arrival on each Turn.
            if sum(call[1] == trace_id for call in self.calls) <= 2:
                detail["trace"]["spanCount"] = 0
                detail["spans"] = detail["spans"][:2]
            return detail_to_events(detail, [])

    repo = SQLiteRepository(tmp_path / "inbank-report.db")
    datasets = DatasetManagement(repo)
    dataset = datasets.create_dataset("Synthetic customer evidence")
    datasets.create_draft(dataset.id)
    state_expectations = (
        {"kind": "state", "path": "execution_status",
         "condition": {"kind": "equals", "expected": "success"}},
    )
    for case_index in range(2):
        turns = []
        for turn_index in range(2):
            name = f"skill-{case_index * 2 + turn_index + 1}"
            route_expectations = (
                ({"kind": "skill_route", "condition": {"kind": "equals", "expected": f"reported-{name}"}},)
                if arrange_type == "yunxia" else ()
            )
            turns.append(CaseTurn(
                id=f"turn-{turn_index + 1}", input={"txt": f"case-{case_index} turn-{turn_index}"},
                expectations=route_expectations + (
                    {"kind": "tool_call", "tool": "read_file"},
                    {"kind": "tool_call", "tool": "delete", "mode": "forbidden"},
                    {"kind": "tool_argument", "tool": "read_file", "path": "arguments.file_path",
                     "condition": {"kind": "equals",
                                   "expected": f"/mnt/oneagent-skills/bundle/skills/{name}/SKILL.md"}},
                    {"kind": "output", "path": "message" if arrange_type == "yunxia" else "output",
                     "condition": {"kind": "equals", "expected": "customer answer"}},
                ) + state_expectations,
            ))
        datasets.save_case(dataset.id, Case(id=f"case-{case_index}", name="Customer case", turns=tuple(turns)))
    datasets.publish_draft(dataset.id)
    target = _request(arrange_type).target
    descriptor = TargetDescriptor(ref=target.ref, display_name=target.display_name)
    repo.save_target_descriptor(descriptor)
    target = TargetSnapshot(**{
        **target.model_dump(exclude={"content_sha256", "descriptor_sha256"}),
        "descriptor_sha256": descriptor.content_sha256,
    })
    model = RecordingModel()
    evaluators = build_default_evaluator_management(
        repo, judge_client=model, judge_model_id="mock-only", judge_credential_ref="env:UNUSED_TEST_KEY",
    )
    spec = judge_spec(input_selection="full_trajectory", max_input_chars=100000)
    judge, _ = evaluators.create_evaluator(
        "Synthetic trajectory judge", kind=spec.kind, dimension=spec.dimension, metric=spec.metric,
        implementation_id=spec.implementation_id, config=spec.config,
    )
    evaluators.publish_draft(judge.id)
    evaluators.update_evaluator(judge.id, enabled=True)
    runs = RunManagement(repo, evaluators)
    selected_evaluators = (["skill-routing"] if arrange_type == "yunxia" else []) + [
        "required-tool", "forbidden-tool", "tool-arguments", "final-output", "final-state", judge.id,
    ]
    run = runs.create_run(target, dataset_id=dataset.id, evaluator_ids=selected_evaluators)
    transport = _Transport(arrange_type, intent_codes=[f"reported-skill-{i}" for i in range(1, 5)])
    trace_server = SkillTraceServer()
    adapter = _adapter(arrange_type, transport, trace_server)
    resolver = resolve_yunxia_trace if arrange_type == "yunxia" else resolve_chatabc_trace
    try:
        completed = runs.execute_run(run.id, adapter, resolver)
    finally:
        adapter.close()
    assert completed.status.value == "completed"
    report = ResultReader(repo).get_report(run.id)
    assert len(report.results) == 2 * len(selected_evaluators)
    assert all(result.outcome.value == "pass" for result in report.results)
    assert len(model.requests) == 2
    assert len(trace_server.calls) == 20
    assert len({call[1] for call in trace_server.calls}) == 4
    reopened = SQLiteRepository(tmp_path / "inbank-report.db")
    assert ResultReader(reopened).get_report(run.id).results == report.results
    seen_route_ids = set()
    for case_index in range(2):
        trace = reopened.get_trace(run.id, f"case-{case_index}")
        assert trace == repo.get_trace(run.id, f"case-{case_index}")
        assert trace.final_state == {"execution_status": "success"}
        for turn_index in range(2):
            turn_trace = trace.for_turn(f"turn-{turn_index + 1}")
            assert turn_trace.final_state == {"execution_status": "success"}
            routes = [s for s in turn_trace.spans if s.operation_type == "routing"]
            tool = next(s for s in turn_trace.spans if s.operation_type == "tool")
            name = f"skill-{case_index * 2 + turn_index + 1}"
            if arrange_type == "yunxia":
                assert len(routes) == 1
                route = routes[0]
                assert route.attributes["selected_skill"] == f"reported-{name}"
                assert route.attributes["inbank.skill_selection"]["basis"] == "message_intent_code"
                assert route.span_id not in seen_route_ids
                seen_route_ids.add(route.span_id)
            else:
                assert routes == []
            assert tool.attributes["arguments"]["file_path"].endswith(f"/{name}/SKILL.md")
            assert tool.attributes["tool_call_id"] == "call-0"  # Same source ID in every turn.
        prompt = model.requests[case_index].user_prompt
        if arrange_type == "yunxia":
            assert f'"selected_skill":"reported-skill-{case_index * 2 + 1}"' in prompt
            assert f'"selected_skill":"reported-skill-{case_index * 2 + 2}"' in prompt
            assert f'"selected_skill":"reported-skill-{(1 - case_index) * 2 + 1}"' not in prompt
        else:
            assert '"selected_skill"' not in prompt


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
def test_customer_prefix_snapshots_four_then_two_spans_wait_for_all_nine(arrange_type, caplog):
    from test_inbank_evidence import skill_detail

    caplog.set_level(logging.INFO)
    transport = _Transport(arrange_type)
    queries = []
    clock = _Clock()

    def opener(request, **_kwargs):
        trace_id = request.full_url.rsplit("/", 1)[-1]
        queries.append(trace_id)
        detail = skill_detail(trace_id)
        model, llm, tool = detail["spans"]
        detail["trace"].update(projectId="customer-agent-id", spanCount=7)
        call_id = f"call-{trace_id}"
        llm["output"]["tool_calls"][0]["id"] = call_id
        tool["output"]["tool_call_id"] = call_id
        tool["parentSpanId"] = "tools"
        model["parentSpanId"] = "root"
        detail["spans"] = [
            dict(model, id="context-1", eventId="context-1", spanType="context", parentSpanId="model"),
            llm, model, tool,
            dict(model, id="tools", eventId="tools"),
            dict(model, id="context-2", eventId="context-2", spanType="context", parentSpanId="model-2"),
            dict(llm, id="llm-2", eventId="llm-2", parentSpanId="model-2", output={"content": "answer"}),
            dict(model, id="model-2", eventId="model-2"),
            dict(model, id="root", eventId="root", parentSpanId=None),
        ]
        if queries.count(trace_id) <= 2:
            detail["trace"]["spanCount"] = 0
            detail["spans"] = detail["spans"][:4 if transport.chat_count == 1 else 2]
        return Response({"code": "0", "data": detail})

    client = TraceServerClient("https://trace.test", mode="inbank", token="task-token", opener=opener)
    adapter = _adapter(arrange_type, transport, client, sleep=clock.sleep, monotonic=clock.monotonic)
    try:
        trace = adapter.wait(adapter.start(_request(arrange_type)), 30).inline_trace
    finally:
        adapter.close()
    assert transport.chat_count == 2 and transport.deleted
    assert queries == [item for request_id in transport.request_ids for item in [request_id] * 5]
    for turn, request_id in zip(("turn-1", "turn-2"), transport.request_ids):
        scoped = trace.for_turn(turn)
        source_spans = [s for s in scoped.spans if "trace_sdk.span_id" in s.attributes]
        assert len(source_spans) == 9
        tool = next(s for s in source_spans if s.operation_type == "tool")
        assert tool.attributes["arguments"]["limit"] == 1000
        assert tool.attributes["tool_call_id"] == f"call-{request_id}"
        source = scoped.spans[0].attributes["inbank.trace_source"]
        assert source["raw_trace"]["spanCount"] == 7
        assert source["readiness"]["attempts"] == 5
        assert source["readiness"]["stable_samples"] == 3
        assert source["readiness"]["stable_seconds"] == 4
    assert "received_spans=4" in caplog.text
    assert "received_spans=2" in caplog.text
    assert "received_spans=9" in caplog.text
    assert "reason=snapshot_settling" in caplog.text
    assert "basis=stable_snapshot" in caplog.text
    assert "Synthetic prompt" not in caplog.text and "task-token" not in caplog.text


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("span_count", [0, 2])
def test_stable_snapshot_without_tool_span_is_accepted_without_synthetic_tools(
    arrange_type, span_count, monkeypatch,
):
    from test_inbank_evidence import skill_detail

    from agentgate.integrations.observability.trace_server import detail_to_events

    transport = _Transport(arrange_type)
    server = _TraceServer()
    queries = []
    clock = _Clock()

    def fetch(project_id, trace_id, **_kwargs):
        queries.append(trace_id)
        detail = skill_detail(trace_id)
        detail["trace"].update(projectId=project_id, spanCount=span_count)
        detail["spans"] = detail["spans"][:2]
        return detail_to_events(detail, [])

    monkeypatch.setattr(server, "fetch_events", fetch)
    adapter = _adapter(arrange_type, transport, server, sleep=clock.sleep, monotonic=clock.monotonic)
    try:
        trace = adapter.wait(adapter.start(_request(arrange_type)), 30).inline_trace
    finally:
        adapter.close()
    assert transport.chat_count == 2 and transport.deleted
    assert queries == [item for request_id in transport.request_ids for item in [request_id] * 3]
    assert clock.now == 8
    assert trace.final_output["message" if arrange_type == "yunxia" else "output"] == "customer answer"
    for turn in ("turn-1", "turn-2"):
        scoped = trace.for_turn(turn)
        root = scoped.spans[0]
        assert root.attributes["inbank.evidence_mode"] == "trace_server"
        assert "inbank.trace_fallback_reason" not in root.attributes
        readiness = root.attributes["inbank.trace_source"]["readiness"]
        assert readiness["stable_samples"] == 3
        assert readiness["stable_seconds"] == 4
        assert len([s for s in scoped.spans if "trace_sdk.span_id" in s.attributes]) == 2
        assert not any(s.operation_type == "tool" for s in scoped.spans)
        llm = next(s for s in scoped.spans if s.operation_type == "llm")
        assert llm.attributes["trace_sdk.output"]["tool_calls"][0]["name"] == "read_file"


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("problem", ["changing-content", "interrupted-stability"])
def test_unstable_snapshot_times_out_without_synthetic_tools(arrange_type, problem, monkeypatch):
    from test_inbank_evidence import skill_detail

    from agentgate.integrations.observability.trace_server import detail_to_events

    transport = _Transport(arrange_type)
    server = _TraceServer()
    queries = []
    clock = _Clock()

    def fetch(project_id, trace_id, **_kwargs):
        queries.append(trace_id)
        detail = skill_detail(trace_id)
        detail["trace"].update(projectId=project_id, spanCount=0)
        if problem == "changing-content":
            detail["spans"][-1]["output"]["content"] = str(len(queries))
        elif queries.count(trace_id) % 3 == 0:
            raise TargetExecutionError("unavailable", "trace server request failed")
        return detail_to_events(detail, [])

    monkeypatch.setattr(server, "fetch_events", fetch)
    adapter = _adapter(arrange_type, transport, server, sleep=clock.sleep, monotonic=clock.monotonic)
    try:
        trace = adapter.wait(adapter.start(_request(arrange_type)), 30).inline_trace
    finally:
        adapter.close()
    assert transport.chat_count == 2 and transport.deleted
    assert len(queries) == 10 and clock.now == 20
    assert len(trace.spans) == (4 if arrange_type == "yunxia" else 2)
    assert not any(span.operation_type in {"tool", "llm", "skill"} for span in trace.spans)
    assert trace.final_output["message" if arrange_type == "yunxia" else "output"] == "customer answer"
    for span in trace.spans:
        if span.operation_type == "routing":
            assert span.attributes["inbank.skill_selection"]["basis"] == "message_intent_code"
            continue
        assert span.operation_type == "turn"
        assert span.attributes["inbank.evidence_mode"] == "output_only"
        assert span.attributes["inbank.trace_fallback_reason"] == "timeout"


def _trace_wait_settings(arrange_type, value=None, *, trace_environment=None):
    prefix = "YUNXIA" if arrange_type == "yunxia" else "CHATABC"
    environ = {
        "AGENTGATE_INBANK_CREATE_BASE_URL": "https://bank.test",
        f"AGENTGATE_INBANK_{prefix}_HEALTH_BASE_URL": "https://bank.test",
        f"AGENTGATE_INBANK_{prefix}_POD_API_BASE_URL": "https://bank.test",
        f"AGENTGATE_INBANK_{prefix}_DELETE_BASE_URL": "https://bank.test",
        "AGENTGATE_INBANK_YUNXIA_AGENT_NAMESPACE": "chatabc",
        "AGENTGATE_INBANK_REQUEST_TIMEOUT_SECONDS": "600",
    }
    if value is not None:
        environ["AGENTGATE_INBANK_TRACE_WAIT_SECONDS"] = value
    environ.update(trace_environment or {})
    loader = load_yunxia_settings if arrange_type == "yunxia" else load_chatabc_settings
    return loader(environ)


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("value,expected", [(None, 60.0), ("20", 20.0), ("24.5", 24.5)])
def test_trace_wait_settings_default_and_environment_override(arrange_type, value, expected):
    settings = _trace_wait_settings(arrange_type, value)
    assert settings.trace_settings == InbankTraceSettings(wait_seconds=expected)
    assert settings.request_timeout_seconds == 600


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "", "not-a-number"])
def test_trace_wait_settings_reject_invalid_environment(arrange_type, value):
    with pytest.raises(ValueError, match="TRACE_WAIT_SECONDS"):
        _trace_wait_settings(arrange_type, value)


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), float("-inf"), True])
def test_trace_wait_settings_reject_invalid_direct_values(arrange_type, value):
    with pytest.raises(ValueError, match="TRACE_WAIT_SECONDS"):
        replace(
            _trace_wait_settings(arrange_type),
            trace_settings=InbankTraceSettings(wait_seconds=value),
        )


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("value", [None, {}, 60])
def test_inbank_adapters_reject_invalid_trace_settings(arrange_type, value):
    with pytest.raises(TypeError, match="trace_settings must be InbankTraceSettings"):
        replace(_trace_wait_settings(arrange_type), trace_settings=value)


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("name", ["POLL_INTERVAL_SECONDS", "SETTLE_SECONDS", "STABLE_SAMPLES"])
def test_inbank_adapters_validate_shared_trace_environment(arrange_type, name):
    key = f"AGENTGATE_INBANK_TRACE_{name}"
    with pytest.raises(ValueError, match=key):
        _trace_wait_settings(arrange_type, trace_environment={key: "0"})


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("interval,settle,samples,attempts,elapsed", [
    (0.5, 0.5, 4, 4, 1.5),
    (1.5, 4.5, 2, 4, 4.5),
])
def test_inbank_adapters_use_configured_polling_and_both_stability_conditions(
    arrange_type, interval, settle, samples, attempts, elapsed,
):
    transport, server, clock = _Transport(arrange_type), _TraceServer(), _Clock()
    adapter = _adapter(
        arrange_type, transport, server, sleep=clock.sleep, monotonic=clock.monotonic,
    )
    adapter.settings = _trace_wait_settings(arrange_type, "90", trace_environment={
        "AGENTGATE_INBANK_TRACE_POLL_INTERVAL_SECONDS": str(interval),
        "AGENTGATE_INBANK_TRACE_SETTLE_SECONDS": str(settle),
        "AGENTGATE_INBANK_TRACE_STABLE_SAMPLES": str(samples),
    })
    assert adapter.settings.trace_settings == InbankTraceSettings(90, interval, settle, samples)
    request = replace(_request(arrange_type), timeout_seconds=600)
    try:
        trace = adapter.wait(adapter.start(request), 600).inline_trace
    finally:
        adapter.close()

    assert transport.chat_count == 2 and transport.deleted
    assert len(server.calls) == 2 * attempts
    assert clock.now == 2 * elapsed
    assert clock.sleeps == [interval] * (2 * (attempts - 1))
    for turn in request.case.turns:
        root = trace.for_turn(turn.id).spans[0]
        assert root.attributes["inbank.evidence_mode"] == "trace_server"
        readiness = root.attributes["inbank.trace_source"]["readiness"]
        assert readiness["stable_samples"] == attempts
        assert readiness["stable_seconds"] == elapsed


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
def test_inbank_adapters_cap_configured_poll_interval_at_deadline_and_fall_back(arrange_type):
    transport, server, clock = _Transport(arrange_type), _TraceServer(), _Clock()
    adapter = _adapter(
        arrange_type, transport, server, sleep=clock.sleep, monotonic=clock.monotonic,
    )
    adapter.settings = _trace_wait_settings(arrange_type, "5", trace_environment={
        "AGENTGATE_INBANK_TRACE_POLL_INTERVAL_SECONDS": "3",
        "AGENTGATE_INBANK_TRACE_SETTLE_SECONDS": "8",
        "AGENTGATE_INBANK_TRACE_STABLE_SAMPLES": "2",
    })
    request = _request(arrange_type)
    try:
        trace = adapter.wait(adapter.start(request), 30).inline_trace
    finally:
        adapter.close()

    assert transport.chat_count == 2 and transport.deleted
    assert len(server.calls) == 4 and clock.now == 10
    assert clock.sleeps == [3, 2, 3, 2]
    for turn in request.case.turns:
        root = trace.for_turn(turn.id).spans[0]
        assert root.attributes["inbank.evidence_mode"] == "output_only"
        assert root.attributes["inbank.trace_fallback_reason"] == "timeout"
        assert "within 5s" in root.attributes["inbank.trace_fallback_detail"]


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("wait_setting,available_at,mode", [
    (None, 6, "trace_server"),
    ("10", 6, "output_only"),
    ("20", 12, "trace_server"),
    (None, 62, "output_only"),
    ("20", 22, "output_only"),
])
def test_configured_trace_wait_handles_delayed_evidence_per_turn(
    arrange_type, wait_setting, available_at, mode, monkeypatch,
):
    transport, server, clock = _Transport(arrange_type), _TraceServer(), _Clock()
    fetch = server.fetch_events
    first_query = {}

    def delayed(project_id, trace_id, *, timeout, customer_task_id):
        first_query.setdefault(trace_id, clock.now)
        events = fetch(project_id, trace_id, timeout=timeout)
        if clock.now - first_query[trace_id] < available_at:
            raise TargetExecutionError("rejected", "trace server HTTP 404")
        return events

    monkeypatch.setattr(server, "fetch_events", delayed)
    adapter = _adapter(
        arrange_type, transport, server, sleep=clock.sleep, monotonic=clock.monotonic,
    )
    adapter.settings = _trace_wait_settings(arrange_type, wait_setting)
    budget = adapter.settings.trace_settings.wait_seconds
    request = replace(_request(arrange_type), timeout_seconds=600)
    try:
        trace = adapter.wait(adapter.start(request), 600).inline_trace
    finally:
        adapter.close()

    assert transport.chat_count == 2 and transport.deleted
    assert len(first_query) == 2
    assert {call[1] for call in server.calls} == set(transport.request_ids)
    assert clock.now == 2 * (available_at + 4 if mode == "trace_server" else budget)
    for turn in request.case.turns:
        scoped = trace.for_turn(turn.id)
        root = scoped.spans[0]
        assert root.attributes["inbank.evidence_mode"] == mode
        assert scoped.final_output["message" if arrange_type == "yunxia" else "output"] == "customer answer"
        if mode == "trace_server":
            readiness = root.attributes["inbank.trace_source"]["readiness"]
            assert readiness["stable_samples"] == 3
            assert readiness["stable_seconds"] == 4
            assert any(s.operation_type == "tool" for s in scoped.spans)
        else:
            assert root.attributes["inbank.trace_fallback_reason"] == "timeout"
            assert f"within {budget:g}s" in root.attributes["inbank.trace_fallback_detail"]
            assert len(scoped.spans) == (2 if arrange_type == "yunxia" else 1)


@pytest.mark.parametrize("arrange_type", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("case_timeout,http_timeout,budget", [(12, 600, 12), (600, 12, 12)])
def test_configured_trace_wait_respects_other_timeout_limits(
    arrange_type, case_timeout, http_timeout, budget, monkeypatch,
):
    transport, server, clock = _Transport(arrange_type), _TraceServer(), _Clock()
    fetch = server.fetch_events

    def unavailable(project_id, trace_id, *, timeout, customer_task_id):
        fetch(project_id, trace_id, timeout=timeout)
        raise TargetExecutionError("rejected", "trace server HTTP 404")

    monkeypatch.setattr(server, "fetch_events", unavailable)
    adapter = _adapter(
        arrange_type, transport, server, sleep=clock.sleep, monotonic=clock.monotonic,
    )
    adapter.settings = replace(
        _trace_wait_settings(arrange_type), request_timeout_seconds=http_timeout,
    )
    request = replace(_request(arrange_type), timeout_seconds=case_timeout)
    try:
        trace = adapter.wait(adapter.start(request), 600).inline_trace
    finally:
        adapter.close()

    assert clock.now == 2 * budget
    assert max(call[2] for call in server.calls) == budget
    assert transport.chat_count == 2 and transport.deleted
    for turn in request.case.turns:
        root = trace.for_turn(turn.id).spans[0]
        assert root.attributes["inbank.evidence_mode"] == "output_only"
        assert f"within {budget:g}s" in root.attributes["inbank.trace_fallback_detail"]
