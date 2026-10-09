"""Customer-script wire behavior, independent of the shared virtual-bank parser."""

import json
from unittest.mock import Mock

import pytest
import requests
from test_inbank_trace_server_adapters import (
    _adapter,
    _request,
    _Response,
    _TraceServer,
    _Transport,
)

from agentgate.integrations.targets.inbank import chatabc, yunxia
from agentgate.run.target_protocol import TargetExecutionError

CFG = {
    "CCE_CREATE_BASE_URL": "https://bank.invalid",
    "HEALTH_CHECK_BASE_URL": "https://bank.invalid",
    "POD_API_BASE_URL": "https://bank.invalid",
    "DELETE_POD_BASE_URL": "https://bank.invalid",
    "AGENT_NAMESPACE": "chatabc",
}


def response(events):
    wire = "".join(
        f"event: {name}\ndata: {data if isinstance(data, str) else json.dumps(data)}\n\n"
        for name, data in events
    )
    return _Response(raw=wire.encode())


@pytest.mark.parametrize(
    "mode,events,answer",
    [
        (
            "base",
            [
                ("chat_started", "not JSON"),
                ("chunk", "partial"),
                ("message", {"content": "最终答复"}),
            ],
            "最终答复",
        ),
        ("base", [("message", "plain customer answer")], "plain customer answer"),
        ("base", [("message", {"content": ""}), ("done", "[DONE]")], ""),
        (
            "workflow",
            [
                ("message", {"node_id": "end", "additional_kwargs": {"node_output": "first"}}),
                ("message", {"node_id": "end", "additional_kwargs": {"node_output": "second"}}),
                ("done", "[DONE]"),
            ],
            "first",
        ),
        (
            "workflow",
            [
                ("message", {"node_id": "tool", "additional_kwargs": {"node_output": "ignore"}}),
                (
                    "message",
                    {"node_id": "end", "additional_kwargs": {"node_output": {"output": "结果"}}},
                ),
            ],
            "结果",
        ),
    ],
)
def test_chatabc_customer_answer_rules(mode, events, answer):
    peer = Mock()
    stream = response(events)
    peer.post.return_value = stream
    client = chatabc.AgentChatClient("pod-one", CFG, mode, http=peer)
    actual, _, error, _, observed, request_id = client.chat_stream("session-one", "你好")
    assert actual == answer
    assert error == ""
    assert len(observed) == len(events)
    assert stream.closed
    args = peer.post.call_args
    assert args.args == ("https://bank.invalid/agent-api/pod-one/chatabc/chat",)
    assert args.kwargs["stream"] is True
    assert args.kwargs["json"] == {
        "appId": "BDC201704_01",
        "trCode": "AISPNLPCHATBOT",
        "trVersion": "1",
        "timestamp": args.kwargs["json"]["timestamp"],
        "requestId": request_id,
        "data": {"session_id": "session-one", "txt": "你好", "files": [], "stream": True},
    }


@pytest.mark.parametrize("mode", ["base", "workflow"])
def test_init_uses_customer_variables_and_timeout(mode):
    peer = Mock()
    peer.request.return_value = _Response(
        data={"resCode": "FAIAG0000", "data": {"session_id": "sid"}}
    )
    sleep = Mock()
    prompt = [{"name": "topic", "value": "finance"}]
    tool = [{"name": "catalog", "value": "test"}]
    client = chatabc.AgentChatClient("pod", CFG, mode, http=peer, sleep=sleep)
    assert client.init_session(prompt, tool) == "sid"
    assert peer.request.call_count == 1
    assert peer.request.call_args.kwargs["timeout"] == 30
    assert peer.request.call_args.kwargs["json"]["data"] == (
        {"config_variables": prompt + tool}
        if mode == "workflow"
        else {"prompt_variables": prompt, "tool_variables": tool}
    )
    sleep.assert_not_called()


@pytest.mark.parametrize("module", [chatabc, yunxia])
@pytest.mark.parametrize("status", [401, 502])
def test_health_polls_after_http_errors_using_customer_timeout(module, status):
    peer = Mock()
    failed = Mock(spec=requests.Response)
    failed.status_code = status
    peer.get.side_effect = [
        requests.HTTPError(response=failed),
        _Response(data={"data": {"status": "ok"}} if module is chatabc else {"status": "ok"}),
    ]
    now = [0.0]

    def advance(seconds):
        now[0] += seconds

    sleep = Mock(side_effect=advance)
    assert module.wait_for_pod_ready("pod", CFG, http=peer, sleep=sleep, monotonic=lambda: now[0])
    assert peer.get.call_count == 2
    assert all(call.kwargs["timeout"] == 10 for call in peer.get.call_args_list)
    sleep.assert_called_once_with(5)
    expected = "/chatabc/health_check" if module is chatabc else "/health"
    assert peer.get.call_args.args[0].endswith(expected)


@pytest.mark.parametrize("module", [chatabc, yunxia])
def test_create_and_delete_use_customer_sixty_second_timeout(module):
    peer = Mock()
    peer.post.return_value = _Response(data={"code": "0", "data": {"agentName": "pod"}})
    assert module.create_agent_pod("12345678", "agent", "v1", CFG, http=peer)["success"]
    assert peer.post.call_args.kwargs["timeout"] == 60
    peer.post.return_value = _Response(data={"resCode": "FAIAG0000"})
    assert module.delete_agent_pod("pod", CFG, http=peer)["success"]
    assert peer.post.call_args.args[0].endswith("/agent-manager/chatabc/delete_agent")
    assert peer.post.call_args.kwargs["timeout"] == 60
    assert peer.post.call_args.kwargs["json"]["data"] == {
        "agent_name": "pod",
        "agent_namespace": "chatabc",
    }


@pytest.mark.parametrize("module", [chatabc, yunxia])
def test_delete_checks_all_customer_business_codes_and_preserves_reason(module):
    peer = Mock()
    peer.post.return_value = _Response(
        data={"resCode": "FAIAG0000", "code": "-1", "message": "无权删除"}
    )
    rejected = module.delete_agent_pod("pod", CFG, http=peer)
    assert rejected["success"] is False
    assert rejected["error_code"] == "rejected"
    assert rejected["error"] == "无权删除"

    peer.post.return_value = _Response(data={})
    assert module.delete_agent_pod("pod", CFG, http=peer)["success"] is True


@pytest.mark.parametrize("module", [chatabc, yunxia])
def test_create_preserves_customer_business_failure(module):
    peer = Mock()
    peer.post.return_value = _Response(data={"code": "-1", "message": "版本不可用"})
    result = module.create_agent_pod("12345678", "agent", "v1", CFG, http=peer, sleep=Mock())
    assert result["success"] is False
    assert result["error_code"] == "rejected"
    assert result["error"] == "code=-1, message=版本不可用"
    assert peer.post.call_count == 3


def test_chatabc_init_preserves_customer_business_failure():
    peer = Mock()
    peer.request.return_value = _Response(
        data={"resCode": "FAIAG1005", "resMessage": "参数错误"}
    )
    result = chatabc.make_request("POST", "https://bank.invalid/init_session", http=peer)
    assert result == {"status": "fail", "error_code": "rejected", "message": "参数错误"}


def test_chatabc_request_timeout_defaults_to_customer_value():
    assert chatabc.ChatABCSettings(
        create_base_url="https://bank.invalid",
        health_base_url="https://bank.invalid",
        pod_api_base_url="https://bank.invalid",
        delete_base_url="https://bank.invalid",
    ).request_timeout_seconds == 180
    settings = chatabc.load_chatabc_settings({
        "AGENTGATE_INBANK_CREATE_BASE_URL": "https://bank.invalid",
        "AGENTGATE_INBANK_CHATABC_HEALTH_BASE_URL": "https://bank.invalid",
        "AGENTGATE_INBANK_CHATABC_POD_API_BASE_URL": "https://bank.invalid",
        "AGENTGATE_INBANK_CHATABC_DELETE_BASE_URL": "https://bank.invalid",
    })
    assert settings.request_timeout_seconds == 180


@pytest.mark.parametrize("branch", [None, "", "null", "None", " branch-one "])
def test_yunxia_create_retains_customer_branch_patch(branch):
    peer = Mock()
    peer.post.return_value = _Response(data={"code": "0", "data": {"agentName": "pod"}})
    result = yunxia.create_agent_pod("12345678", "agent", "v1", CFG, branch_id=branch, http=peer)
    assert result["success"]
    assert peer.post.call_args.kwargs["json"] == {
        "agentId": "agent",
        "agentVersion": "v1",
        **({"branchId": "branch-one"} if branch == " branch-one " else {}),
    }


@pytest.mark.parametrize(
    "history,expected",
    [
        ('[{"role":"user"}]', [{"role": "user"}]),
        ("previous message", ["previous message"]),
        ("  ", []),
        (0, [0]),
        (False, [False]),
    ],
)
def test_yunxia_normalizes_customer_app_history(history, expected):
    payload = yunxia.build_request_payload(
        {"questionId": "case-one", "user_input": "hello", "currentDisplay_1": history}, CFG
    )
    assert payload["appHistory"] == expected


def test_yunxia_preserves_message_and_ignores_auxiliary_frames():
    peer = Mock()
    stream = response(
        [
            ("chunk", "not JSON"),
            ("message", {"status": "running", "message": "working"}),
            ("message", {"status": "completed", "message": "最终答案", "output": "old value"}),
            ("trace", {"project_id": "p", "trace_id": "t"}),
            ("done", "[DONE]"),
        ]
    )
    peer.post.return_value = stream
    result = yunxia.process_single_row(
        {
            "questionId": "case-one",
            "user_input": "question",
            "currentDisplay_1": [{"role": "user"}],
        },
        "pod",
        {**CFG, "_RUN_CUST_UUID": "-run-one"},
        http=peer,
    )
    assert result["output"] == "最终答案"
    assert result["error"] == ""
    assert result["trace_payloads"] == ({"project_id": "p", "trace_id": "t"},)
    assert stream.closed
    assert peer.post.call_args.kwargs["json"] == {
        "sessionId": "case-one",
        "custID": "case-one-run-one",
        "txt": "question",
        "executionMode": "execute",
        "stream": True,
        "debugTrace": True,
        "appHistory": [{"role": "user"}],
        "config_variables": [],
    }


def test_yunxia_last_customer_message_can_be_plain_text():
    peer = Mock()
    peer.post.return_value = response([
        ("message", {"message": "earlier answer"}),
        ("message", "最终纯文本答复"),
        ("done", "[DONE]"),
    ])
    result = yunxia.process_single_row(
        {"questionId": "case-one", "user_input": "hello"}, "pod", CFG, http=peer
    )
    assert result["output"] == "最终纯文本答复"
    assert result["message"] == {}
    assert result["message_data"] == "最终纯文本答复"


@pytest.mark.parametrize("module,event", [
    (chatabc, "error"),
    (chatabc, "failed"),
    (yunxia, "error"),
    (yunxia, "failed"),
])
def test_explicit_customer_failure_is_not_a_success(module, event):
    peer = Mock()
    peer.post.return_value = response(
        [
            ("message", {"content": "partial", "message": "partial"}),
            (event, {"status": "fail", "message": "model_name cannot be empty"}),
        ]
    )
    if module is chatabc:
        result = chatabc.AgentChatClient("pod", CFG, http=peer).chat_stream("sid", "hello")
        error = result[2]
    else:
        error = yunxia.process_single_row(
            {"user_input": "hello", "questionId": "case"}, "pod", CFG, http=peer
        )["error"]
    assert error.startswith("rejected:")
    assert "model_name cannot be empty" in error


@pytest.mark.parametrize("terminal", [
    "[DONE]",
    "not JSON",
    {"status": "completed"},
    {"status": "fail"},
    {"status": "failed"},
    {"status": "error"},
    {"ok": False},
])
def test_yunxia_done_does_not_reject_or_stop_event_collection(terminal):
    # Customer yunxia.py ignores done and continues collecting message/trace events.
    peer = Mock()
    stream = response([
        ("message", {"message": "earlier answer"}),
        ("done", terminal),
        ("message", {"message": "最终答案"}),
        ("trace", {"project_id": "p", "trace_id": "t"}),
    ])
    peer.post.return_value = stream

    result = yunxia.process_single_row(
        {"questionId": "case-one", "user_input": "hello"}, "pod", CFG, http=peer
    )

    assert result["error"] == ""
    assert result["output"] == "最终答案"
    assert result["trace_payloads"] == ({"project_id": "p", "trace_id": "t"},)
    assert len(result["events"]) == 4
    assert stream.closed


@pytest.mark.parametrize("metadata", [
    {},
    {"status": "completed"},
    {"status": "running"},
    {"status": "fail"},
    {"status": "failed"},
    {"status": "error"},
    {"ok": False},
])
@pytest.mark.parametrize("answer_field", ["message", "output"])
def test_yunxia_message_metadata_does_not_define_execution_failure(metadata, answer_field):
    message = {
        **metadata,
        "output": "fallback answer",
        answer_field: "客户最终答复",
        "intent_code": "customer-intent",
        "slots": {"city": "上海"},
        "workflow_calls": [{"id": "customer-workflow"}],
    }
    peer = Mock()
    peer.post.return_value = response([("message", message)])

    result = yunxia.process_single_row(
        {"questionId": "case-one", "user_input": "hello"}, "pod", CFG, http=peer
    )

    assert result["error"] == ""
    assert result["output"] == "客户最终答复"
    assert result["message"] == message


@pytest.mark.parametrize("event", ["error", "failed"])
@pytest.mark.parametrize("failure_after_done", [False, True])
def test_yunxia_done_and_later_message_do_not_override_explicit_failure(
    event, failure_after_done
):
    failure = (event, {"message": "customer execution failed"})
    done = ("done", "[DONE]")
    events = [
        ("message", {"message": "partial answer"}),
        *([done, failure] if failure_after_done else [failure, done]),
        ("message", {"message": "must not replace the failed result", "ok": True}),
        ("trace", {"project_id": "p", "trace_id": "t"}),
    ]
    peer = Mock()
    peer.post.return_value = response(events)

    result = yunxia.process_single_row(
        {"questionId": "case-one", "user_input": "hello"}, "pod", CFG, http=peer
    )

    assert result["error"].startswith("rejected:")
    assert result["message"] == {"message": "partial answer"}
    assert result["trace_payloads"] == ({"project_id": "p", "trace_id": "t"},)


@pytest.mark.parametrize("mode", ["base", "workflow"])
@pytest.mark.parametrize("terminal", [
    "[DONE]",
    "not JSON",
    {"status": "completed"},
    {"status": "fail"},
    {"status": "failed"},
    {"status": "error"},
    {"ok": False},
])
def test_chatabc_done_only_ends_answer_collection(mode, terminal):
    # Customer workflow&bianpai.py treats done as a marker, not a business response.
    answer = "客户最终答复"
    message = (
        {"node_id": "end", "additional_kwargs": {"node_output": {"output": answer}}}
        if mode == "workflow"
        else {"content": answer}
    )
    peer = Mock()
    stream = response([
        ("message", message),
        ("done", terminal),
        ("message", {"content": "must not replace the answer"}),
        ("failed", {"message": "after the end marker"}),
    ])
    peer.post.return_value = stream

    actual, _, error, _, _, _ = chatabc.AgentChatClient(
        "pod", CFG, mode, http=peer
    ).chat_stream("sid", "hello")

    assert actual == answer
    assert error == ""
    assert stream.closed


@pytest.mark.parametrize("mode", ["base", "workflow"])
@pytest.mark.parametrize("event", ["error", "failed"])
def test_chatabc_done_does_not_override_an_explicit_failure(mode, event):
    peer = Mock()
    peer.post.return_value = response([
        ("message", {"content": "partial"}),
        (event, {"message": "model_name cannot be empty"}),
        ("done", "[DONE]"),
    ])

    answer, _, error, _, _, _ = chatabc.AgentChatClient(
        "pod", CFG, mode, http=peer
    ).chat_stream("sid", "hello")

    assert answer == ""
    assert error.startswith("rejected:")


@pytest.mark.parametrize("mode", ["base", "workflow"])
def test_chatabc_done_without_message_is_not_an_answer(mode):
    peer = Mock()
    peer.post.return_value = response([("done", {"status": "completed"})])

    answer, _, error, _, _, _ = chatabc.AgentChatClient(
        "pod", CFG, mode, http=peer
    ).chat_stream("sid", "hello")

    assert answer == ""
    assert error == "protocol_error: chat returned no message event"


@pytest.mark.parametrize("module", [chatabc, yunxia])
def test_sse_parser_checks_deadline(module):
    stream = response([("message", {"content": "late"})])
    parse = chatabc.AgentChatClient._parse_sse if module is chatabc else yunxia.parse_sse_stream
    with pytest.raises(requests.exceptions.Timeout):
        parse(stream, deadline=0)


@pytest.mark.parametrize("module", [chatabc, yunxia])
def test_real_request_errors_exhaust_creation_and_stop(module):
    peer = Mock()
    peer.post.side_effect = requests.exceptions.Timeout()
    sleep = Mock()
    result = module.create_agent_pod("12345678", "agent", "v1", CFG, http=peer, sleep=sleep)
    assert result["success"] is False
    assert result["error_code"] == "timeout"
    assert peer.post.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [10, 10]


@pytest.mark.parametrize("mode", ["base", "workflow", "yunxia"])
def test_unhealthy_created_pod_is_still_deleted(mode, monkeypatch):
    peer = _Transport(mode)
    original = peer.request_json
    health_calls = []

    def not_ready(method, url, **kwargs):
        if "/health" in url:
            health_calls.append(kwargs["timeout"])
            return {"status": "starting", "data": {"status": "starting"}}
        return original(method, url, **kwargs)

    monkeypatch.setattr(peer, "request_json", not_ready)
    now = [0.0]

    def advance(seconds):
        now[0] += seconds

    adapter = _adapter(mode, peer, _TraceServer(), sleep=advance, monotonic=lambda: now[0])
    try:
        with pytest.raises(TargetExecutionError, match="health check timed out"):
            adapter.start(_request(mode))
    finally:
        adapter.close()
    assert adapter.pod_lifecycle["health"] == "failed"
    assert adapter.pod_lifecycle["delete"] == "succeeded"
    assert health_calls == [10] * 10
    assert peer.chat_count == 0
    assert peer.deleted


@pytest.mark.parametrize("module", [chatabc, yunxia])
def test_sse_exception_closes_response(module):
    stream = _Response()
    stream.iter_lines = Mock(side_effect=requests.exceptions.Timeout())
    peer = Mock()
    peer.post.return_value = stream
    if module is chatabc:
        error = chatabc.AgentChatClient("pod", CFG, http=peer).chat_stream("sid", "hello")[2]
    else:
        error = yunxia.call_api_via_pod({}, "pod", CFG, http=peer)[1]
    assert error.startswith("timeout:")
    assert stream.closed
