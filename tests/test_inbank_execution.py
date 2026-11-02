"""Customer-style failure isolation, session concurrency, and independent time budgets."""

import json
import threading
from contextlib import closing
from dataclasses import replace
from time import monotonic
from unittest.mock import Mock

import pytest
from test_inbank_trace_server_adapters import (
    _adapter,
    _Clock,
    _request,
    _trace_wait_settings,
    _TraceServer,
    _Transport,
)
from test_run_engine import pending_run

from agentgate.application.result_reader import ResultReader
from agentgate.domain import Case, CaseTurn, EvaluatorSpec, Outcome, RunStatus, Trace
from agentgate.evaluator.executor import execute_evaluators
from agentgate.evaluator.rule.output import FinalOutputEvaluator
from agentgate.integrations.targets.inbank import yunxia
from agentgate.run.engine import RunEngine
from agentgate.run.target_protocol import (
    CaseExecutionPending,
    CaseExecutionStatus,
    TargetExecutionError,
)
from agentgate.storage.sqlite import SQLiteRepository


def case(case_id, *texts, initial=None, session_id=None, mode=None):
    return Case(id=case_id, name=case_id, initial_state=initial or {}, turns=[
        CaseTurn(id=f"{case_id}-{index}", input={"txt": text, **(
            {"sessionId": session_id} if session_id else {}
        )}, expectations=[{"kind": "output", "path": "message" if mode == "yunxia" else "output",
                          "condition": {"kind": "equals", "expected": "customer answer"}}])
        for index, text in enumerate(texts)
    ])


def evaluate(case, trace, specs):
    return execute_evaluators(case, trace, specs, {("final_output", "1"): FinalOutputEvaluator()})


def wait(adapter, handle):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        try:
            return adapter.wait(handle, 1)
        except CaseExecutionPending:
            continue
    raise AssertionError("local execution did not finish")


@pytest.mark.parametrize("mode", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("failure_index", [0, 1, 2])
def test_failed_round_keeps_other_rounds_and_cases_and_is_not_scored(tmp_path, monkeypatch, mode, failure_index):
    peer = _Transport(mode)
    original = peer.request_bytes
    attempted = []

    def respond(method, url, **kwargs):
        text = kwargs["payload"]["txt"] if mode == "yunxia" else kwargs["payload"]["data"]["txt"]
        attempted.append(text)
        if text == "fail":
            raise TargetExecutionError("timeout", "customer timed out; token=do-not-save")
        return original(method, url, **kwargs)

    monkeypatch.setattr(peer, "request_bytes", respond)
    texts = ["first", "middle", "last"]
    texts[failure_index] = "fail"
    cases = (case("partial", *texts, mode=mode), case("independent", "next", mode=mode))
    run = pending_run(cases=cases, target=_request(mode).target).model_copy(update={"id": "run-12345678"})
    with closing(SQLiteRepository(tmp_path / "isolation.db")) as repository:
        repository.save_run(run)
        adapter = _adapter(mode, peer, _TraceServer())
        try:
            completed = RunEngine(repository, evaluate, lambda _, result: result.inline_trace).execute(run, adapter)
        finally:
            adapter.close()
        assert completed.status is RunStatus.COMPLETED
        results = {result.case_id: result for result in repository.list_results(run.id)}
        assert results["partial"].outcome is Outcome.ERROR
        assert results["partial"].score is None
        assert results["partial"].error_detail.category == "target_execution"
        assert len(results["partial"].checks) == 2
        assert all(check.outcome is Outcome.PASS for check in results["partial"].checks)
        assert results["independent"].outcome is Outcome.PASS
        assert results["independent"].score == 1
        trace = repository.get_trace(run.id, "partial")
        assert Trace.model_validate_json(trace.model_dump_json()) == trace
        assert len(trace.turn_outcomes) == 3
        assert trace.turn_outcomes[f"partial-{failure_index}"]["output"] == {}
        assert "do-not-save" not in trace.model_dump_json()
        report = ResultReader(repository).get_report(run.id)
        assert report is not None
    assert attempted == texts + ["next"]
    assert peer.deleted


@pytest.mark.parametrize("mode", ["base", "workflow"])
def test_init_failure_records_whole_case_and_continues_next_case(monkeypatch, mode):
    peer = _Transport(mode)
    original = peer.request_json
    init_calls = []

    def initialize(method, url, **kwargs):
        if url.endswith("/init_session"):
            data = kwargs["payload"]["data"]
            init_calls.append(data)
            if any(data.values()):
                raise TargetExecutionError("rejected", "session init rejected")
        return original(method, url, **kwargs)

    monkeypatch.setattr(peer, "request_json", initialize)
    key = "config_variables" if mode == "workflow" else "prompt_variables"
    adapter = _adapter(mode, peer, _TraceServer())
    try:
        failed = adapter.wait(adapter.start(replace(_request(mode), case=case(
            "failed-init", "one", "two", initial={key: [{"name": "centreId", "value": "810000"}]},
        ))), 30)
        succeeded = adapter.wait(adapter.start(replace(_request(mode), execution_id="next",
            case=case("next-case", "next"))), 30)
    finally:
        adapter.close()
    assert failed.error.code == "rejected"
    assert len(failed.inline_trace.spans) == 2
    assert all(span.status == "error" for span in failed.inline_trace.spans)
    assert succeeded.error is None
    assert peer.chat_count == 1
    assert len(init_calls) == 11  # Ten customer retries for the failed init, one for the next Case.


@pytest.mark.parametrize("mode", ["base", "workflow", "yunxia"])
def test_real_case_workers_are_parallel_bounded_and_turns_stay_ordered(tmp_path, monkeypatch, mode):
    peer = _Transport(mode)
    original = peer.request_bytes
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    active = maximum = 0
    observed = []

    def respond(method, url, **kwargs):
        nonlocal active, maximum
        text = kwargs["payload"]["txt"] if mode == "yunxia" else kwargs["payload"]["data"]["txt"]
        with lock:
            active += 1
            maximum = max(maximum, active)
            observed.append(text)
        try:
            if text.endswith("-first"):
                barrier.wait(timeout=3)
            with lock:
                return original(method, url, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(peer, "request_bytes", respond)
    cases = tuple(case(f"case-{i}", f"{i}-first", f"{i}-second", mode=mode) for i in range(4))
    run = pending_run(cases=cases, target=_request(mode).target, max_parallel_cases=2).model_copy(
        update={"id": "run-12345678"}
    )
    with closing(SQLiteRepository(tmp_path / "parallel.db")) as repository:
        repository.save_run(run)
        adapter = _adapter(mode, peer, _TraceServer())
        try:
            completed = RunEngine(repository, evaluate, lambda _, result: result.inline_trace).execute(run, adapter)
        finally:
            adapter.close()
        assert completed.status is RunStatus.COMPLETED
        assert len(repository.list_traces(run.id)) == 4
        assert all(item.outcome is Outcome.PASS for item in repository.list_results(run.id))
    assert maximum == 2
    for index in range(4):
        assert observed.index(f"{index}-first") < observed.index(f"{index}-second")
    assert peer.deleted


def test_yunxia_shared_session_cases_do_not_overlap_or_reorder(monkeypatch):
    peer = _Transport("yunxia")
    original = peer.request_bytes
    entered, release = threading.Event(), threading.Event()
    messages = []

    def respond(method, url, **kwargs):
        payload = kwargs["payload"]
        messages.append(dict(payload))
        if payload["txt"] == "first":
            entered.set()
            assert release.wait(3)
        return original(method, url, **kwargs)

    monkeypatch.setattr(peer, "request_bytes", respond)
    adapter = _adapter("yunxia", peer, _TraceServer())
    request = replace(_request("yunxia"), max_parallel_cases=3)
    try:
        first = adapter.start(replace(request, case=case("first-case", "first", "second", session_id="shared")))
        assert entered.wait(2)
        second = adapter.start(replace(request, execution_id="second", case=case("second-case", "third", session_id="shared")))
        other = adapter.start(replace(request, execution_id="other", case=case("other-case", "independent")))
        wait(adapter, other)
        assert [message["txt"] for message in messages] == ["first", "independent"]
        release.set()
        wait(adapter, first)
        wait(adapter, second)
    finally:
        release.set()
        adapter.close()
    shared = [message for message in messages if message["sessionId"] == "shared"]
    assert [message["txt"] for message in shared] == ["first", "second", "third"]
    assert len({message["custID"] for message in shared}) == 1


@pytest.mark.parametrize("mode", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("total_timeout", [0, 5])
def test_overall_timeout_only_checks_before_turn_and_does_not_shrink_request(monkeypatch, mode, total_timeout):
    peer = _Transport(mode)
    original = peer.request_bytes
    clock = _Clock()
    timeouts = []

    def respond(method, url, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock.now += 6
        return original(method, url, **kwargs)

    monkeypatch.setattr(peer, "request_bytes", respond)
    server = _TraceServer()
    monkeypatch.setattr(server, "fetch_events", lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(TargetExecutionError("rejected", "no trace"))
    ))
    adapter = _adapter(mode, peer, server, monotonic=clock.monotonic, sleep=clock.sleep)
    adapter.settings = replace(adapter.settings, total_timeout_seconds=total_timeout)
    try:
        result = adapter.wait(adapter.start(_request(mode)), 30)
        next_result = adapter.wait(adapter.start(replace(_request(mode), execution_id="next")), 30)
    finally:
        adapter.close()
    assert timeouts == ([10] if total_timeout else [10, 10, 10, 10])
    output_key = "message" if mode == "yunxia" else "output"
    assert result.inline_trace.for_turn("turn-1").final_output[output_key] == "customer answer"
    if total_timeout:
        assert result.error.code == next_result.error.code == "timeout"
        assert result.inline_trace.turn_outcomes["turn-2"]["output"] == {}
    else:
        assert result.error is next_result.error is None


@pytest.mark.parametrize("mode", ["base", "workflow", "yunxia"])
def test_cancel_waits_for_inflight_request_before_deleting_pod(monkeypatch, mode):
    peer = _Transport(mode)
    original = peer.request_bytes
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    def respond(method, url, **kwargs):
        entered.set()
        assert release.wait(3)
        assert not peer.deleted
        result = original(method, url, **kwargs)
        returned.set()
        return result

    monkeypatch.setattr(peer, "request_bytes", respond)
    adapter = _adapter(mode, peer, _TraceServer())
    try:
        handle = adapter.start(replace(_request(mode), max_parallel_cases=2))
        assert entered.wait(2)
        adapter.cancel(handle)
        assert adapter.get_status(handle) is CaseExecutionStatus.CANCELLED
        release.set()
        with pytest.raises(TargetExecutionError, match="cancelled"):
            adapter.wait(handle, 1)
    finally:
        release.set()
        adapter.close()
    assert returned.is_set() and peer.deleted and peer.chat_count == 1


def test_failed_case_does_not_invoke_case_level_judge(monkeypatch):
    from test_judge_evaluator import judge_spec
    peer = _Transport("yunxia")
    monkeypatch.setattr(peer, "request_bytes", lambda *_a, **_k: (
        (_ for _ in ()).throw(TargetExecutionError("rejected", "customer rejected"))
    ))
    adapter = _adapter("yunxia", peer, _TraceServer())
    try:
        trace = adapter.wait(adapter.start(_request("yunxia")), 30).inline_trace
    finally:
        adapter.close()

    class Judge:
        kind, implementation_id, implementation_version = "llm_judge", "judge", "1"

        def evaluate_case(self, *_args):
            raise AssertionError("must not score an incomplete conversation")

    spec = EvaluatorSpec(id="judge", name="Judge", kind="llm_judge", dimension="answer",
                         metric="answer", implementation_id="judge", config=judge_spec().config)
    result, = execute_evaluators(_request("yunxia").case, trace, (spec,), {("judge", "1"): Judge()})
    assert result.outcome is Outcome.ERROR and result.score is None
    assert result.error_detail.category == "target_execution"


@pytest.mark.parametrize("response", [
    {"message": "customer answer"},
    {"message": "customer answer", "business": {
        "amount": 0, "approved": False, "reason": None, "details": {"policy": "annual leave"},
    }},
    {"message": "customer answer", "output": "separate business output", "slots": {"days": 10}},
])
def test_full_yunxia_message_is_final_output_and_evaluable_directly(monkeypatch, response):
    peer = _Transport("yunxia")
    original = peer.request_bytes

    def respond(method, url, **kwargs):
        wire = original(method, url, **kwargs)
        _, _, rest = wire.partition(b"\n\n")
        return f"event: message\ndata: {json.dumps(response)}\n\n".encode() + rest

    monkeypatch.setattr(peer, "request_bytes", respond)
    adapter = _adapter("yunxia", peer, _TraceServer())
    request = _request("yunxia")
    expected_fields = [(None, response), ("message", response["message"])]
    expected_fields.extend(
        (f"business.{key}", value) for key, value in response.get("business", {}).items()
    )
    if "output" in response:
        expected_fields.append(("output", response["output"]))
    turns = [turn.model_copy(update={"expectations": tuple(CaseTurn(input={"txt": "q"}, expectations=[
        {"kind": "output", "path": path, "condition": {"kind": "equals", "expected": expected}}
        for path, expected in expected_fields
    ]).expectations)}) for turn in request.case.turns]
    request = replace(request, case=request.case.model_copy(update={"turns": tuple(turns)}))
    try:
        trace = adapter.wait(adapter.start(request), 30).inline_trace
    finally:
        adapter.close()
    spec = pending_run().manifest.evaluator_specs
    result, = evaluate(request.case, trace, spec)
    assert result.outcome is Outcome.PASS
    assert len(result.checks) == len(expected_fields) * len(turns)
    assert trace.final_output == response
    assert all(trace.for_turn(turn.id).final_output == response for turn in turns)
    assert all("inbank.response.message_data" not in span.attributes for span in trace.spans)
    if "output" not in response:
        missing_output_case = request.case.model_copy(update={"turns": tuple(
            turn.model_copy(update={"expectations": CaseTurn(input={"txt": "q"}, expectations=[
                {"kind": "output", "path": "output",
                 "condition": {"kind": "equals", "expected": response["message"]}},
            ]).expectations}) for turn in turns
        )})
        missing_result, = evaluate(missing_output_case, trace, spec)
        assert missing_result.outcome is Outcome.FAIL
        assert all(check.actual_missing for check in missing_result.checks)


@pytest.mark.parametrize("mode", ["base", "workflow", "yunxia"])
def test_lifecycle_timeouts_are_configurable_and_reach_customer_requests(monkeypatch, mode):
    settings = _trace_wait_settings(mode, trace_environment={
        "AGENTGATE_INBANK_CREATE_TIMEOUT_SECONDS": "17",
        "AGENTGATE_INBANK_INIT_TIMEOUT_SECONDS": "3",
        "AGENTGATE_INBANK_DELETE_TIMEOUT_SECONDS": "7",
        "AGENTGATE_INBANK_HEALTH_REQUEST_TIMEOUT_SECONDS": "2",
        "AGENTGATE_INBANK_REQUEST_TIMEOUT_SECONDS": "4",
        "AGENTGATE_INBANK_TOTAL_TIMEOUT_SECONDS": "0",
    })
    peer = _Transport(mode)
    original_json, original_bytes = peer.request_json, peer.request_bytes
    timeouts = []

    def request_json(method, url, **kwargs):
        timeouts.append((url, kwargs["timeout"]))
        return original_json(method, url, **kwargs)

    def request_bytes(method, url, **kwargs):
        timeouts.append((url, kwargs["timeout"]))
        return original_bytes(method, url, **kwargs)

    monkeypatch.setattr(peer, "request_json", request_json)
    monkeypatch.setattr(peer, "request_bytes", request_bytes)
    adapter = _adapter(mode, peer, _TraceServer())
    adapter.settings = settings
    try:
        result = adapter.wait(adapter.start(_request(mode)), 30)
    finally:
        adapter.close()
    assert result.error is None
    for url, timeout in timeouts:
        if "createAgent" in url:
            assert timeout == 17
        elif "health" in url:
            assert timeout == 2
        elif "init_session" in url:
            assert timeout == 3
        elif "delete" in url:
            assert timeout == 7
        else:
            assert timeout == 4


@pytest.mark.parametrize("mode", ["base", "workflow", "yunxia"])
def test_pod_readiness_uses_its_own_budget_not_the_chat_timeout(monkeypatch, mode):
    peer = _Transport(mode)
    original = peer.request_json
    health_checks = 0

    def respond(method, url, **kwargs):
        nonlocal health_checks
        if "health" in url:
            health_checks += 1
            if health_checks < 5:
                return {"status": "starting"}
        return original(method, url, **kwargs)

    monkeypatch.setattr(peer, "request_json", respond)
    adapter = _adapter(mode, peer, _TraceServer())
    try:
        result = adapter.wait(adapter.start(replace(_request(mode), timeout_seconds=1)), 30)
    finally:
        adapter.close()
    assert result.error is None and health_checks == 5


@pytest.mark.parametrize("mode", ["base", "workflow", "yunxia"])
@pytest.mark.parametrize("configured", [None, "0", "12.5"])
def test_total_timeout_defaults_to_customer_unlimited_and_can_be_configured(mode, configured):
    values = {} if configured is None else {"AGENTGATE_INBANK_TOTAL_TIMEOUT_SECONDS": configured}
    assert _trace_wait_settings(mode, trace_environment=values).total_timeout_seconds == (
        float(configured) if configured else 0
    )


@pytest.mark.parametrize("configured,expected", [(None, 15.0), ("0", 0.0), ("2.5", 2.5)])
def test_yunxia_post_health_sleep_defaults_and_environment_override(configured, expected):
    values = {
        "AGENTGATE_INBANK_CREATE_BASE_URL": "https://bank.test",
        "AGENTGATE_INBANK_YUNXIA_HEALTH_BASE_URL": "https://bank.test",
        "AGENTGATE_INBANK_YUNXIA_POD_API_BASE_URL": "https://bank.test",
        "AGENTGATE_INBANK_YUNXIA_DELETE_BASE_URL": "https://bank.test",
        "AGENTGATE_INBANK_YUNXIA_AGENT_NAMESPACE": "chatabc",
    }
    if configured is not None:
        values["AGENTGATE_INBANK_YUNXIA_POST_HEALTH_SLEEP_SECONDS"] = configured
    assert yunxia.load_yunxia_settings(values).post_health_sleep_seconds == expected


def test_yunxia_settings_default_post_health_sleep_is_fifteen_seconds():
    settings = yunxia.YunxiaSettings(
        create_base_url="https://bank.test",
        health_base_url="https://bank.test",
        pod_api_base_url="https://bank.test",
        delete_base_url="https://bank.test",
        agent_namespace="chatabc",
    )
    assert settings.post_health_sleep_seconds == 15


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("source", ["settings", "environment"])
def test_yunxia_post_health_sleep_rejects_invalid_duration(value, source):
    with pytest.raises(ValueError):
        if source == "environment":
            _trace_wait_settings("yunxia", trace_environment={
                "AGENTGATE_INBANK_YUNXIA_POST_HEALTH_SLEEP_SECONDS": str(value),
            })
        else:
            replace(_trace_wait_settings("yunxia"), post_health_sleep_seconds=value)


@pytest.mark.parametrize("mode,delay", [
    ("base", 15.0), ("workflow", 15.0),
    ("yunxia", 15.0), ("yunxia", 0.0), ("yunxia", 2.5),
])
def test_only_yunxia_sleeps_once_after_health_before_multicase_messages(monkeypatch, mode, delay):
    peer = _Transport(mode)
    original_json, original_bytes = peer.request_json, peer.request_bytes
    clock = _Clock()
    events = []

    def request_json(method, url, **kwargs):
        result = original_json(method, url, **kwargs)
        if "/createAgent" in url:
            events.append("create")
        elif "/health" in url:
            events.append("healthy")
        return result

    def request_bytes(method, url, **kwargs):
        events.append("message")
        return original_bytes(method, url, **kwargs)

    def sleep(seconds):
        events.append(("sleep", seconds))
        clock.sleep(seconds)

    monkeypatch.setattr(peer, "request_json", request_json)
    monkeypatch.setattr(peer, "request_bytes", request_bytes)
    server = _TraceServer()
    monkeypatch.setattr(server, "fetch_events", Mock(side_effect=TargetExecutionError(
        "rejected", "no trace",
    )))
    adapter = _adapter(mode, peer, server, sleep=sleep, monotonic=clock.monotonic)
    if mode == "yunxia":
        adapter.settings = replace(adapter.settings, post_health_sleep_seconds=delay)
    request = _request(mode)
    second = replace(request, execution_id="execution-2", case=request.case.model_copy(update={
        "id": "case-2",
    }))
    try:
        results = [adapter.wait(adapter.start(item), 30) for item in (request, second)]
    finally:
        adapter.close()

    expected_sleep = [("sleep", delay)] if mode == "yunxia" and delay else []
    assert events == ["create", "healthy", *expected_sleep, *(["message"] * 4)]
    assert all(result.error is None for result in results)
    assert all(len(result.inline_trace.turn_outcomes) == 2 for result in results)
    assert peer.chat_count == 4 and peer.deleted


def test_yunxia_post_health_sleep_preserves_independent_execution_budgets(monkeypatch):
    peer = _Transport("yunxia")
    original = peer.request_bytes
    clock = _Clock()
    message_times, request_timeouts = [], []

    def respond(method, url, **kwargs):
        message_times.append(clock.now)
        request_timeouts.append(kwargs["timeout"])
        clock.now += 1
        return original(method, url, **kwargs)

    monkeypatch.setattr(peer, "request_bytes", respond)
    server = _TraceServer()
    monkeypatch.setattr(server, "fetch_events", Mock(side_effect=TargetExecutionError(
        "rejected", "no trace",
    )))
    adapter = _adapter("yunxia", peer, server, sleep=clock.sleep, monotonic=clock.monotonic)
    adapter.settings = replace(
        adapter.settings, post_health_sleep_seconds=15, total_timeout_seconds=5,
    )
    request = replace(_request("yunxia"), timeout_seconds=1)
    try:
        result = adapter.wait(adapter.start(request), 30)
    finally:
        adapter.close()

    assert result.error is None
    assert message_times == [15, 16]
    assert request_timeouts == [1, 1]
    assert clock.sleeps == [15]
    assert peer.chat_count == 2 and peer.deleted
