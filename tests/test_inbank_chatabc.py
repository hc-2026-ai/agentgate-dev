from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from agentgate.application import ResultReader, RunManagement, TargetCatalog
from agentgate.application.dataset_management import DatasetManagement
from agentgate.application.evaluator_management import (
    build_default_evaluator_management,
)
from agentgate.domain import (
    Case,
    CaseTurn,
    Outcome,
    RunStatus,
    TargetDescriptor,
    TargetRef,
    TargetSnapshot,
    TargetType,
)
from agentgate.integrations.targets.inbank.chatabc import (
    ChatABCSettings,
    InbankChatABCTargetAdapter,
    load_chatabc_settings,
    resolve_chatabc_trace,
)
from agentgate.run.target_protocol import CaseExecutionRequest, TargetExecutionError
from agentgate.storage.sqlite import SQLiteRepository


TRACEPARENT = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"


class FakeTransport:
    def __init__(self, arrange_type: str) -> None:
        self.arrange_type = arrange_type
        self.calls: list[dict] = []
        self.chat_request_ids: list[str] = []
        self.session_fail = False
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
            return {"code": "0", "data": {"agentName": "pod-agent-1"}}
        if path.endswith("/health_check"):
            return {"data": {"status": "ok"}}
        if path.endswith("/init_session"):
            if self.session_fail:
                return {"resCode": "FAILED", "resMessage": "Bearer private"}
            return {"resCode": "FAIAG0000", "data": {"session_id": "session-1"}}
        if path.endswith("/workflow_trace"):
            request_id = parse_qs(urlparse(url).query)["request_id"][0]
            assert request_id in self.chat_request_ids
            return {
                "code": "0000",
                "data": [
                    {
                        "kind": "customer-step",
                        "request_id": request_id,
                        "authorization": "Bearer private",
                    }
                ],
            }
        if path.endswith("/deleteAgent"):
            assert parse_qs(urlparse(url).query) == {
                "agentName": ["pod-agent-1"]
            }
            return {"code": "9999" if self.delete_fail else "0", "data": {}}
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
        request_id = payload["requestId"]
        self.chat_request_ids.append(request_id)
        if self.arrange_type == "workflow":
            message = {
                "node_id": "end",
                "additional_kwargs": {"node_output": {"output": "workflow answer"}},
            }
        else:
            message = {"content": "base answer"}
        return (
            "event: message\n"
            f"data: {json.dumps(message)}\n\n"
            "event: done\n"
            "data: [DONE]\n\n"
        ).encode()


def settings(**updates) -> ChatABCSettings:
    values = {
        "create_base_url": "https://create.example.test/root",
        "health_base_url": "https://health.example.test",
        "pod_api_base_url": "https://pod.example.test",
        "delete_base_url": "https://delete.example.test",
        "request_timeout_seconds": 10,
        "health_wait_seconds": 10,
        "health_poll_interval_seconds": 0.01,
    }
    values.update(updates)
    return ChatABCSettings(**values)


def target(
    arrange_type: str, *, customer_task_id: str | None = "customer-task-1"
) -> TargetSnapshot:
    invocation_config = {
        "arrange_type": arrange_type,
        "agent_version": "published-version-1",
    }
    if customer_task_id is not None:
        invocation_config["customer_task_id"] = customer_task_id
    return TargetSnapshot(
        ref=TargetRef(
            source_id="inbank-agent-platform",
            target_type=TargetType.AGENT,
            external_target_id="agent-1",
            external_version_id="version-1",
        ),
        display_name="Customer Agent",
        adapter_type="inbank_chatabc",
        adapter_version="1",
        descriptor_sha256="c" * 64,
        invocation_config=invocation_config,
    )


def case(arrange_type: str, *, files=None, turns: int = 2) -> Case:
    if arrange_type == "base":
        initial = {
            "prompt_variables": [{"name": "channel", "value": "app", "type": "str"}],
            "tool_variables": [],
        }
    else:
        initial = {
            "config_variables": [
                {"name": "channel", "value": "app", "type": "str"}
            ]
        }
    return Case(
        id="case-1",
        name="Conversation",
        initial_state=initial,
        turns=tuple(
            CaseTurn(
                id=f"turn-{number}",
                input={
                    "txt": f"question {number}",
                    "files": [] if files is None else files,
                },
            )
            for number in range(1, turns + 1)
        ),
    )


def request(
    arrange_type: str,
    *,
    files=None,
    turns: int = 2,
    customer_task_id: str | None = "customer-task-1",
):
    return CaseExecutionRequest(
        execution_id="execution-1",
        run_id="run-1",
        case=case(arrange_type, files=files, turns=turns),
        target=target(arrange_type, customer_task_id=customer_task_id),
        timeout_seconds=20,
        traceparent=TRACEPARENT,
    )


@pytest.mark.parametrize(
    ("arrange_type", "answer"),
    (("base", "base answer"), ("workflow", "workflow answer")),
)
def test_adapter_executes_multiturn_case_and_collects_trace_before_cleanup(
    arrange_type, answer
) -> None:
    transport = FakeTransport(arrange_type)
    adapter = InbankChatABCTargetAdapter(settings(), transport=transport)
    execution = request(arrange_type)

    result = adapter.wait(adapter.start(execution), 20)

    trace = resolve_chatabc_trace(execution, result)
    assert trace.trace_id == "a" * 32
    assert trace.final_output == {"output": answer}
    assert len(trace.spans) == 2
    assert len(transport.chat_request_ids) == 2
    assert len(set(transport.chat_request_ids)) == 2
    assert all(
        span.attributes["inbank.session_id"] == "session-1"
        for span in trace.spans
    )
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
        "pod_name": "pod-agent-1",
        "cleanup_error_type": None,
    }
    assert all("inbank.trace_events" not in span.attributes for span in trace.spans)
    assert all(
        trace.turn_outcomes[turn.id]["input"] == turn.input
        for turn in execution.case.turns
    )

    paths_before_close = [urlparse(call["url"]).path for call in transport.calls]
    assert paths_before_close.count("/root/web/agent_endpoint/createAgent") == 1
    assert paths_before_close.count(
        "/agent-api/pod-agent-1/chatabc/init_session"
    ) == 1
    assert paths_before_close.count("/root/web/race_eval/workflow_trace") == 0
    assert not any(path.endswith("deleteAgent") for path in paths_before_close)

    init_call = next(
        call for call in transport.calls if call["url"].endswith("init_session")
    )
    if arrange_type == "base":
        assert set(init_call["payload"]["data"]) == {
            "prompt_variables",
            "tool_variables",
        }
    else:
        assert set(init_call["payload"]["data"]) == {"config_variables"}
    assert "Authorization" not in init_call["headers"]
    assert init_call["headers"]["traceparent"] == TRACEPARENT

    adapter.close()
    adapter.close()
    delete_calls = [
        call
        for call in transport.calls
        if urlparse(call["url"]).path.endswith("deleteAgent")
    ]
    assert len(delete_calls) == 1
    assert all("Authorization" not in call["headers"] for call in transport.calls)
    assert adapter.pod_lifecycle["delete"] == "succeeded"
    assert adapter.pod_lifecycle["cleanup_error_type"] is None


def test_adapter_exposes_delete_business_failure_without_losing_result() -> None:
    transport = FakeTransport("base")
    transport.delete_fail = True
    adapter = InbankChatABCTargetAdapter(settings(), transport=transport)

    adapter.wait(adapter.start(request("base", turns=1)), 20)
    adapter.close()

    assert adapter.pod_lifecycle["create"] == "succeeded"
    assert adapter.pod_lifecycle["health"] == "succeeded"
    assert adapter.pod_lifecycle["delete"] == "failed"
    assert adapter.pod_lifecycle["cleanup_error_type"] == "TargetExecutionError"


def test_adapter_rejects_nonempty_files_before_creating_pod() -> None:
    transport = FakeTransport("base")
    adapter = InbankChatABCTargetAdapter(settings(), transport=transport)

    with pytest.raises(TargetExecutionError, match="non-empty files"):
        adapter.start(request("base", files=[{"name": "document.pdf"}], turns=1))

    assert transport.calls == []


def test_adapter_requires_customer_task_id_and_never_falls_back_to_run_id() -> None:
    transport = FakeTransport("base")
    adapter = InbankChatABCTargetAdapter(settings(), transport=transport)

    with pytest.raises(TargetExecutionError, match="customer_task_id"):
        adapter.start(request("base", turns=1, customer_task_id=None))

    assert transport.calls == []


def test_output_only_agent_execution_reaches_persisted_final_report(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "inbank-output-report.db")
    ref = TargetRef(
        source_id="inbank-agent-platform",
        target_type=TargetType.AGENT,
        external_target_id="agent-1",
        external_version_id="version-1",
    )
    descriptor = TargetDescriptor(ref=ref, display_name="Customer Agent")
    TargetCatalog(repository).register_descriptor(descriptor)
    snapshot = TargetSnapshot(
        ref=ref,
        display_name=descriptor.display_name,
        adapter_type="inbank_chatabc",
        adapter_version="1",
        descriptor_sha256=descriptor.content_sha256,
        invocation_config={
            "arrange_type": "base",
            "agent_version": "published-version-1",
            "customer_task_id": "customer-task-1",
        },
    )
    output_case = Case(
        id="case-output",
        name="Output-only customer Agent case",
        initial_state={"prompt_variables": [], "tool_variables": []},
        turns=(
            CaseTurn(
                id="turn-output",
                input={"txt": "question", "files": []},
                expectations=(
                    {
                        "id": "expected-output",
                        "kind": "output",
                        "path": "output",
                        "condition": {
                            "kind": "equals",
                            "expected": "base answer",
                        },
                    },
                ),
            ),
        ),
    )
    datasets = DatasetManagement(repository)
    dataset = datasets.create_dataset("In-bank output-only dataset")
    datasets.create_draft(dataset.id)
    datasets.save_case(dataset.id, output_case)
    published = datasets.publish_draft(dataset.id)
    runs = RunManagement(
        repository, build_default_evaluator_management(repository)
    )
    run = runs.create_run(
        snapshot,
        dataset_id=dataset.id,
        dataset_version=published.version,
        evaluator_ids=["final-output"],
        max_parallel_cases=1,
        max_retries=0,
        case_max_parallel=1,
    )
    adapter = InbankChatABCTargetAdapter(
        settings(), transport=FakeTransport("base")
    )

    try:
        completed = runs.execute_run(run.id, adapter, resolve_chatabc_trace)
    finally:
        adapter.close()
    report = ResultReader(repository).get_report(completed.id)

    assert completed.status is RunStatus.COMPLETED
    assert len(report.results) == 1
    assert report.results[0].outcome is Outcome.PASS
    stored_trace = repository.get_trace(completed.id, output_case.id)
    assert stored_trace is not None
    assert stored_trace.final_output == {"output": "base answer"}
    assert stored_trace.spans[0].attributes["inbank.evidence_mode"] == (
        "output_only"
    )


def test_session_failure_is_not_retried_and_error_is_sanitized() -> None:
    transport = FakeTransport("workflow")
    transport.session_fail = True
    adapter = InbankChatABCTargetAdapter(settings(), transport=transport)

    with pytest.raises(TargetExecutionError) as raised:
        adapter.start(request("workflow", turns=1))

    init_calls = [
        call for call in transport.calls if call["url"].endswith("init_session")
    ]
    assert len(init_calls) == 1
    assert "private" not in str(raised.value)
    adapter.close()


def test_settings_loader_requires_complete_nonsecret_environment() -> None:
    environment = {
        "AGENTGATE_INBANK_CREATE_BASE_URL": "https://create.example.test",
        "AGENTGATE_INBANK_CHATABC_HEALTH_BASE_URL": "https://health.example.test",
        "AGENTGATE_INBANK_CHATABC_POD_API_BASE_URL": "https://pod.example.test",
        "AGENTGATE_INBANK_CHATABC_DELETE_BASE_URL": "https://delete.example.test",
    }
    loaded = load_chatabc_settings(environment)
    assert "bearer" not in repr(loaded).lower()

    with pytest.raises(ValueError, match="incomplete ChatABC environment"):
        load_chatabc_settings({})


@pytest.mark.parametrize(
    "url",
    (
        "ftp://example.test",
        "https://user:password@example.test",
        "https://example.test?token=private",
    ),
)
def test_settings_reject_unsafe_base_urls(url) -> None:
    with pytest.raises(ValueError):
        settings(create_base_url=url)
