from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from agentgate.domain import Case, CaseTurn, TargetRef, TargetSnapshot, TargetType
from agentgate.integrations.targets.inbank.yunxia import (
    InbankYunxiaTargetAdapter,
    YunxiaSettings,
    load_yunxia_settings,
    resolve_yunxia_trace,
)
from agentgate.run.target_protocol import CaseExecutionRequest, TargetExecutionError


TRACEPARENT = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"


class FakeTransport:
    def __init__(self, *, envelope: bool = False) -> None:
        self.envelope = envelope
        self.calls: list[dict] = []
        self.mismatched_request_id = False
        self.omit_trace = False
        self.after_done = False
        self.delete_fail = False

    def request_json(
        self, method, url, *, payload=None, headers=None, timeout
    ):
        self.calls.append(
            {
                "kind": "json",
                "method": method,
                "url": url,
                "payload": payload,
                "headers": dict(headers or {}),
            }
        )
        path = urlparse(url).path
        if path.endswith("/createAgent"):
            assert parse_qs(urlparse(url).query) == {
                "taskId": ["customer-task-1"]
            }
            return {"code": 0, "data": {"agentName": "yunxia-pod"}}
        if path.endswith("/health"):
            return {"status": "ok"}
        if path.endswith("/delete_agent"):
            return {"resCode": "FAILED" if self.delete_fail else "FAIAG0000"}
        raise AssertionError(url)

    def request_bytes(
        self, method, url, *, payload=None, headers=None, timeout
    ) -> bytes:
        self.calls.append(
            {
                "kind": "bytes",
                "method": method,
                "url": url,
                "payload": payload,
                "headers": dict(headers or {}),
            }
        )
        request_id = headers["X-Request-ID"]
        if self.mismatched_request_id:
            request_id = "different-request"
        events = [
            (
                "start",
                {
                    "session_id": payload["sessionId"],
                    "request_id": request_id,
                },
            ),
            ("progress", {"seq": 1}),
            (
                "message",
                {
                    "status": "completed",
                    "output": "yunxia answer",
                    "intent_code": "transfer-routing",
                    "slots": {"payee": "test-user"},
                    "workflow_calls": [{"name": "transfer"}],
                },
            ),
        ]
        if not self.omit_trace:
            events.append(
                (
                    "trace",
                    {"steps": [{"name": "route"}], "token": "private"},
                )
            )
        events.append(("done", "[DONE]"))
        if self.after_done:
            events.append(("progress", {"seq": 99}))
        if self.envelope:
            return "".join(
                "data: "
                + json.dumps({"event": name, "data": data})
                + "\n\n"
                for name, data in events
            ).encode()
        return "".join(
            f"event: {name}\ndata: {json.dumps(data)}\n\n"
            for name, data in events
        ).encode()


def settings(**updates) -> YunxiaSettings:
    values = {
        "create_base_url": "https://create.example.test",
        "health_base_url": "https://health.example.test",
        "pod_api_base_url": "https://pod.example.test",
        "delete_base_url": "https://delete.example.test",
        "agent_namespace": "evaluation",
        "test_customer_prefix": "TEST-",
        "request_timeout_seconds": 10,
        "health_wait_seconds": 10,
        "health_poll_interval_seconds": 0.01,
    }
    values.update(updates)
    return YunxiaSettings(**values)


def target(customer_task_id: str | None = "customer-task-1") -> TargetSnapshot:
    invocation_config = {
        "agent_version": "version-2",
        "branch_id": "branch-1",
        "arrange_type": "yunxia",
    }
    if customer_task_id is not None:
        invocation_config["customer_task_id"] = customer_task_id
    return TargetSnapshot(
        ref=TargetRef(
            source_id="inbank-agent-platform",
            target_type=TargetType.AGENT,
            external_target_id="agent-yunxia",
            external_version_id="branch-1:version-2",
        ),
        display_name="Yunxia Agent",
        adapter_type="inbank_yunxia",
        adapter_version="1",
        descriptor_sha256="e" * 64,
        invocation_config=invocation_config,
    )


def case(*, files=None, customer_id="TEST-CUSTOMER") -> Case:
    return Case(
        id="case-1",
        name="Yunxia conversation",
        initial_state={
            "customer_id": customer_id,
            "config_variables": [{"name": "channel", "value": "app"}],
        },
        turns=(
            CaseTurn(
                id="turn-1",
                input={
                    "txt": "first",
                    "app_history": [{"role": "user", "content": "before"}],
                    "files": [] if files is None else files,
                },
            ),
            CaseTurn(id="turn-2", input={"txt": "second", "files": []}),
        ),
    )


def request(
    *, customer_task_id: str | None = "customer-task-1", **case_updates
) -> CaseExecutionRequest:
    return CaseExecutionRequest(
        execution_id="execution-1",
        run_id="run-1",
        case=case(**case_updates),
        target=target(customer_task_id),
        timeout_seconds=20,
        traceparent=TRACEPARENT,
    )


@pytest.mark.parametrize("envelope", (False, True))
def test_adapter_executes_yunxia_sse_and_preserves_evidence(envelope) -> None:
    transport = FakeTransport(envelope=envelope)
    adapter = InbankYunxiaTargetAdapter(settings(), transport=transport)
    execution = request()

    result = adapter.wait(adapter.start(execution), 20)
    trace = resolve_yunxia_trace(execution, result)

    assert trace.trace_id == "c" * 32
    assert trace.final_output["output"] == "yunxia answer"
    assert trace.final_output["intent_code"] == "transfer-routing"
    assert trace.final_output["slots"] == {"payee": "test-user"}
    assert len(trace.spans) == 2
    assert all(
        span.attributes["inbank.evidence_mode"] == "output_only"
        for span in trace.spans
    )
    assert all(span.attributes["inbank.pod_created"] is True for span in trace.spans)
    assert all(span.attributes["inbank.pod_healthy"] is True for span in trace.spans)
    assert adapter.pod_lifecycle == {
        "create": "succeeded",
        "health": "succeeded",
        "delete": "not_attempted",
        "pod_name": "yunxia-pod",
        "cleanup_error_type": None,
    }
    assert all("inbank.trace_events" not in span.attributes for span in trace.spans)

    message_calls = [call for call in transport.calls if call["kind"] == "bytes"]
    assert len(message_calls) == 2
    assert len({call["headers"]["X-Request-ID"] for call in message_calls}) == 2
    assert len({call["payload"]["sessionId"] for call in message_calls}) == 1
    assert len({call["payload"]["custID"] for call in message_calls}) == 1
    assert message_calls[0]["payload"]["custID"].startswith("TEST-CUSTOMER-")
    assert message_calls[0]["payload"]["appHistory"] == [
        {"role": "user", "content": "before"}
    ]
    assert message_calls[0]["payload"]["debugTrace"] is False
    assert message_calls[1]["payload"]["appHistory"] == []
    assert set(message_calls[0]["payload"]) == {
        "sessionId",
        "custID",
        "txt",
        "executionMode",
        "stream",
        "debugTrace",
        "config_variables",
        "appHistory",
    }
    assert not {
        "agentSessionId",
        "availableSkills",
        "safeGuardrail",
        "safe_guardrail",
    } & set(message_calls[0]["payload"])
    assert "Authorization" not in message_calls[0]["headers"]
    assert message_calls[0]["headers"]["traceparent"] == TRACEPARENT
    assert not any("init_session" in call["url"] for call in transport.calls)
    assert not any("workflow_trace" in call["url"] for call in transport.calls)

    adapter.close()
    adapter.close()
    delete_calls = [
        call
        for call in transport.calls
        if urlparse(call["url"]).path.endswith("delete_agent")
    ]
    assert len(delete_calls) == 1
    assert delete_calls[0]["method"] == "POST"
    assert delete_calls[0]["payload"]["data"] == {
        "agent_name": "yunxia-pod",
        "agent_namespace": "evaluation",
    }
    assert all("Authorization" not in call["headers"] for call in transport.calls)
    assert adapter.pod_lifecycle["delete"] == "succeeded"
    assert adapter.pod_lifecycle["cleanup_error_type"] is None


def test_adapter_exposes_explicit_yunxia_delete_failure() -> None:
    transport = FakeTransport()
    transport.delete_fail = True
    adapter = InbankYunxiaTargetAdapter(settings(), transport=transport)

    adapter.wait(adapter.start(request()), 20)
    adapter.close()

    assert adapter.pod_lifecycle["create"] == "succeeded"
    assert adapter.pod_lifecycle["health"] == "succeeded"
    assert adapter.pod_lifecycle["delete"] == "failed"
    assert adapter.pod_lifecycle["cleanup_error_type"] == "TargetExecutionError"


@pytest.mark.parametrize(
    ("case_updates", "message"),
    (
        ({"files": [{"name": "document.pdf"}]}, "non-empty files"),
        ({"customer_id": "REAL-CUSTOMER"}, "approved test prefix"),
    ),
)
def test_adapter_rejects_unsafe_case_before_creating_pod(
    case_updates, message
) -> None:
    transport = FakeTransport()
    adapter = InbankYunxiaTargetAdapter(settings(), transport=transport)

    with pytest.raises(TargetExecutionError, match=message):
        adapter.start(request(**case_updates))

    assert transport.calls == []


def test_adapter_requires_customer_task_id_and_never_falls_back_to_run_id() -> None:
    transport = FakeTransport()
    adapter = InbankYunxiaTargetAdapter(settings(), transport=transport)

    with pytest.raises(TargetExecutionError, match="customer_task_id"):
        adapter.start(request(customer_task_id=None))

    assert transport.calls == []


@pytest.mark.parametrize("failure", ("mismatch", "after_done"))
def test_adapter_rejects_incomplete_or_uncorrelated_sse_without_retry(failure) -> None:
    transport = FakeTransport()
    transport.mismatched_request_id = failure == "mismatch"
    transport.after_done = failure == "after_done"
    adapter = InbankYunxiaTargetAdapter(settings(), transport=transport)

    with pytest.raises(TargetExecutionError):
        adapter.start(request())

    message_calls = [call for call in transport.calls if call["kind"] == "bytes"]
    assert len(message_calls) == 1
    adapter.close()


def test_adapter_accepts_output_without_customer_trace_event() -> None:
    transport = FakeTransport()
    transport.omit_trace = True
    adapter = InbankYunxiaTargetAdapter(settings(), transport=transport)

    result = adapter.wait(adapter.start(request()), 20)
    trace = resolve_yunxia_trace(request(), result)

    assert trace.final_output["output"] == "yunxia answer"
    assert all("inbank.trace_events" not in span.attributes for span in trace.spans)
    adapter.close()


def test_settings_loader_requires_customer_safety_configuration() -> None:
    environment = {
        "AGENTGATE_INBANK_CREATE_BASE_URL": "https://create.example.test",
        "AGENTGATE_INBANK_YUNXIA_HEALTH_BASE_URL": "https://health.example.test",
        "AGENTGATE_INBANK_YUNXIA_POD_API_BASE_URL": "https://pod.example.test",
        "AGENTGATE_INBANK_YUNXIA_DELETE_BASE_URL": "https://delete.example.test",
        "AGENTGATE_INBANK_YUNXIA_AGENT_NAMESPACE": "evaluation",
        "AGENTGATE_INBANK_YUNXIA_TEST_CUSTOMER_PREFIX": "TEST-",
    }
    loaded = load_yunxia_settings(environment)
    assert loaded.test_customer_prefix == "TEST-"
    assert "bearer" not in repr(loaded).lower()

    environment.pop("AGENTGATE_INBANK_YUNXIA_TEST_CUSTOMER_PREFIX")
    with pytest.raises(ValueError, match="incomplete Yunxia environment"):
        load_yunxia_settings(environment)
