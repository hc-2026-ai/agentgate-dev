"""In-bank evidence binding and adaptation to the existing evaluation contract."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_target_execution_factory import target_snapshot

from agentgate.domain import (
    Case,
    CaseTurn,
    Equals,
    ExecutionPathExpectation,
    FrozenJsonObject,
    SkillRouteExpectation,
    ToolArgumentExpectation,
    ToolCallExpectation,
    Trace,
)
from agentgate.integrations.observability.trace_server import (
    detail_to_events,
    prepare_inbank_trace_events,
)
from agentgate.integrations.targets.bank_protocol import BankChatResult
from agentgate.integrations.targets.inbank.evidence import (
    assemble_evidence,
    configured_trace_source,
    fetch_turn_evidence,
    normalize_turn_evidence,
    output_only_turn_span,
    project_yunxia_intent_selection,
)
from agentgate.run.target_protocol import CaseExecutionRequest, TargetExecutionError


def trace_detail(source, session, text):
    start = "2026-10-08T00:00:00Z"
    root = {"id": source, "projectId": "inbank-tests", "sessionId": session,
            "name": "agent", "agentName": "test-pod", "input": {"txt": text},
            "output": "answer", "status": "success", "spanCount": 4,
            "durationMs": 5, "startedAt": start}
    spans = []
    for sid, parent, kind, name in [("root", None, "agent", "agent"),
                                   ("skill", "root", "chain", "skill.review"),
                                   ("llm", "skill", "llm", "model"),
                                   ("tool", "skill", "tool", "credit_inquiry")]:
        spans.append({"id": sid, "traceId": source, "eventId": sid,
                      "parentSpanId": parent, "spanType": kind, "name": name,
                      "toolName": name if kind == "tool" else None,
                      "status": "success", "durationMs": 1, "startedAt": start,
                      "input": {"txt": text}, "output": "answer"})
    return {"trace": root, "spans": spans, "observations": []}


def chat(reference=None):
    return BankChatResult(
        "answer",
        "request",
        None,
        ({"project_id": "inbank-tests", "trace_id": "source"} if reference is None else reference,),
        {},
        (),
    )


def fetch(client, result=None):
    return fetch_turn_evidence(
        client,
        "inbank-tests",
        result or chat(),
        session_id="session",
        request_id="request",
        input_field="txt",
        turn_input={"txt": "hello"},
        agent_name="test-pod",
        timeout=2,
    )


@pytest.fixture
def source():
    return Mock(
        fetch_events=Mock(
            return_value=detail_to_events(trace_detail("source", "session", "hello"), [])
        )
    )


@pytest.mark.parametrize(
    "reference",
    [
        None,
        {},
        {"project_id": "other", "trace_id": "source"},
        {"project_id": "inbank-tests", "trace_id": "../source"},
        {"project_id": "inbank-tests", "trace_id": "source", "simulated": True},
        {"project_id": "inbank-tests", "trace_id": "source", "request_id": "different"},
        {"project_id": "inbank-tests", "trace_id": "source", "session_id": "different"},
    ],
)
def test_reference_mismatch_never_queries_server(source, reference):
    result = (
        chat(reference)
        if reference is not None
        else BankChatResult("answer", "request", None, (), {}, ())
    )
    with pytest.raises(TargetExecutionError):
        fetch(source, result)
    source.fetch_events.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "foreign"),
        ("trace_id", "foreign"),
        ("session_id", "foreign"),
        ("agent_name", "foreign"),
        ("input", "wrong"),
        ("output", "wrong"),
        ("status", "running"),
        ("span_count", None),
        ("span_count", 5),
    ],
)
def test_remote_root_must_match_turn(source, field, value):
    source.fetch_events.return_value[0][field] = value
    with pytest.raises(TargetExecutionError):
        fetch(source)


def execution_request(two_turns=False):
    turns = [CaseTurn(id="first", input={"txt": "hello"})]
    if two_turns:
        turns.append(CaseTurn(id="second", input={"txt": "hello"}))
    return CaseExecutionRequest(
        "execution",
        "run",
        Case(id="case", name="case", turns=tuple(turns)),
        target_snapshot("inbank_chatabc"),
        30,
        "00-" + "1" * 32 + "-" + "2" * 16 + "-01",
    )


def test_normalized_evidence_preserves_tree_and_protocol_output(source):
    evidence = fetch(source)
    output = {"output": "answer", "intent_code": "review", "slots": {"amount": 42}}
    trace = assemble_evidence(
        execution_request(),
        "inbank-tests",
        {"first": evidence},
        {"first": {"input": {"txt": "hello"}, "output": output, "state": {}}},
    )
    assert trace.final_output.to_dict() == output
    skill = next(s for s in trace.spans if s.name == "skill.review")
    tool = next(s for s in trace.spans if s.name == "credit_inquiry")
    assert tool.parent_span_id == skill.span_id
    assert tool.operation_type == "tool"
    assert all(s.attributes["trace_sdk.replay"] is False for s in trace.spans)


def test_case_input_alias_is_compared_to_actual_wire_txt(source):
    result = fetch_turn_evidence(
        source,
        "inbank-tests",
        chat(),
        session_id="session",
        request_id="request",
        input_field="question",
        turn_input={"question": "hello"},
        agent_name="test-pod",
        timeout=2,
    )
    assert result.source_trace_id == "source"


@pytest.mark.parametrize("damage", ["duplicate", "orphan", "cycle", "missing_turn"])
def test_partial_or_reused_evidence_is_rejected(source, damage):
    events = source.fetch_events.return_value
    if damage == "orphan":
        events[1]["parent_span_id"] = "missing"
    if damage == "cycle":
        events[1]["parent_span_id"] = events[1]["span_id"]
    item = fetch(source)
    evidence = {"first": item}
    if damage == "duplicate":
        evidence["second"] = item
    with pytest.raises(TargetExecutionError, match="incomplete or invalid"):
        assemble_evidence(
            execution_request(damage in {"duplicate", "missing_turn"}), "inbank-tests", evidence, {}
        )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://user:secret@trace.example",
        "https://trace.example/arbitrary",
        "ftp://trace.example",
    ],
)
def test_explicit_trace_origin_required(monkeypatch, url):
    monkeypatch.setenv("AGENTGATE_TRACE_SERVER_URL", url)
    monkeypatch.setenv("AGENTGATE_INBANK_TRACE_PROJECT_ID", "inbank-tests")
    with pytest.raises(TargetExecutionError) as error:
        configured_trace_source()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "required,expected", [("credit_inquiry", "pass"), ("approve_loan", "fail")]
)
def test_tool_checks_use_retrieved_execution_evidence(source, required, expected):
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.domain import ToolCallExpectation
    from agentgate.evaluator.rule.tool_use import RequiredToolEvaluator

    request = execution_request()
    trace = assemble_evidence(
        request,
        "inbank-tests",
        {"first": fetch(source)},
        {"first": {"input": {"txt": "hello"}, "output": {"output": "answer"}, "state": {}}},
    )
    turn = CaseTurn(
        id="first", input={"txt": "hello"}, expectations=(ToolCallExpectation(tool=required),)
    )
    evaluation = RequiredToolEvaluator().evaluate(
        evaluator_spec("required_tool"), turn, trace, resolve_unexpected
    )
    assert evaluation.checks[0].outcome == expected
    if expected == "pass":
        tool = next(s for s in trace.spans if s.name == required)
        assert tool.span_id in evaluation.checks[0].span_ids


def skill_detail(source="source", skills=("hello",)):
    """Synthetic customer wire shape; no customer prompts or credentials."""
    detail = trace_detail(source, "session", "hello")
    template = detail["spans"][0]
    paths = [f"/mnt/oneagent-skills/bundle/skills/{name}/SKILL.md" for name in skills]
    calls = [
        {"id": f"call-{i}", "name": "read_file", "type": "tool_call",
         "args": {"file_path": path, "limit": 1000}}
        for i, path in enumerate(paths)
    ]
    detail["spans"] = [
        {**template, "id": "model", "eventId": "model", "spanType": "chain", "name": "model",
         "input": {"skills_metadata": [{"name": name, "path": path} for name, path in zip(skills, paths)],
                   "messages": [{"type": "human", "content": "hello"}]},
         "output": {"messages": [{"type": "ai", "tool_calls": deepcopy(calls)}]}},
        {**template, "id": "llm", "eventId": "llm", "spanType": "llm",
         "name": "RouterChatOpenAI", "parentSpanId": "model",
         "startedAt": "2026-10-08T00:00:00.001Z",
         "input": [{"role": "system", "content": "Synthetic prompt"}],
         "output": {"role": "assistant", "content": "", "tool_calls": calls}},
        *[
            {**template, "id": f"tool-{i}", "eventId": f"tool-{i}", "spanType": "tool",
             "name": "read_file", "toolName": "read_file", "parentSpanId": "model",
             "startedAt": f"2026-10-08T00:00:00.{i + 3:03d}Z",
             "input": repr(call["args"]),
             "output": {"type": "tool", "name": "read_file", "tool_call_id": call["id"],
                        "status": "success", "content": "Synthetic tool result"}}
            for i, call in enumerate(calls)
        ],
    ]
    detail["trace"].update(input=None, output=None, spanCount=len(detail["spans"]), durationMs=50)
    return detail


def adapt_detail(detail, request=None, turn=None):
    request = request or execution_request()
    turn = turn or request.case.turns[0]
    source = detail["trace"]["id"]
    output = {"output": "dialogue answer"}
    events = prepare_inbank_trace_events(
        detail_to_events(detail, []), project_id="inbank-tests", customer_task_id="pod-task",
        trace_id=source, final_output=output,
    )
    spans = normalize_turn_evidence(
        request, turn, project_id="inbank-tests", request_id=source, events=events,
    )
    state = json.loads(spans[0].attributes["agentgate.turn.state"])
    return Trace(
        trace_id=request.traceparent.split("-")[1], run_id=request.run_id, case_id=request.case.id,
        spans=spans, turn_outcomes={turn.id: {"input": turn.input, "output": output, "state": state}},
        final_output=output, final_state=state,
    )


def reported_intent(trace, intent="hello"):
    return trace.model_copy(update={"spans": project_yunxia_intent_selection(
        trace.spans, {"intent_code": intent},
    )})


@pytest.mark.parametrize("tool_status", ["success", "error"])
def test_turn_execution_state_comes_from_trace_root_not_output_or_tool(tool_status):
    detail = skill_detail()
    detail["trace"]["output"] = {"execution_status": "error"}
    detail["spans"][2]["status"] = tool_status
    original = deepcopy(detail)

    trace = adapt_detail(detail)

    assert detail == original
    root = next(span for span in trace.spans if span.operation_type == "turn")
    assert json.loads(root.attributes["agentgate.turn.state"]) == {
        "execution_status": detail["trace"]["status"],
    }
    assert trace.for_turn("first").final_state.to_dict() == {"execution_status": "success"}
    assert all("agentgate.turn.state" not in span.attributes
               for span in trace.spans if span.operation_type != "turn")


@pytest.mark.parametrize("status", ["missing", None, "", "running", "error"])
def test_turn_execution_state_does_not_relax_existing_trace_status_validation(status):
    detail = skill_detail()
    if status == "missing":
        detail["trace"].pop("status")
    else:
        detail["trace"]["status"] = status
    with pytest.raises((ValueError, TargetExecutionError)):
        adapt_detail(detail)


def test_output_only_turn_does_not_claim_execution_state():
    from datetime import UTC, datetime

    from agentgate.integrations.targets.inbank.evidence import output_only_turn_span

    request = execution_request()
    now = datetime(2026, 10, 13, tzinfo=UTC)
    span = output_only_turn_span(
        request, request.case.turns[0], adapter="yunxia", project_id="inbank-tests",
        request_id="request", started_at=now, ended_at=now,
        error=TargetExecutionError("unavailable", "trace not found"),
    )
    assert span.attributes["inbank.evidence_mode"] == "output_only"
    assert "agentgate.turn.state" not in span.attributes


@pytest.mark.parametrize("expected,outcome", [("success", "pass"), ("error", "fail")])
def test_existing_final_state_rule_can_consume_projected_execution_status(expected, outcome):
    from test_rule_state import evaluator_spec, resolve_unexpected

    from agentgate.domain import StateExpectation
    from agentgate.evaluator.rule.state import FinalStateEvaluator

    trace = adapt_detail(skill_detail())
    turn = CaseTurn(id="first", input={"txt": "hello"}, expectations=(
        StateExpectation(path="execution_status", condition=Equals(expected=expected)),
    ))
    check = FinalStateEvaluator().evaluate(
        evaluator_spec(), turn, trace.for_turn(turn.id), resolve_unexpected,
    ).checks[0]
    assert check.actual == "success"
    assert not check.actual_missing
    assert check.outcome == outcome


def test_customer_semantics_preserve_raw_tree_without_inferring_skill():
    detail = skill_detail()
    original = deepcopy(detail)
    trace = adapt_detail(detail)
    assert detail == original
    assert trace == adapt_detail(detail)
    assert len(trace.spans) == len(detail["spans"]) + 1  # Turn wrapper only.
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    chain = next(s for s in trace.spans if s.operation_type == "chain")
    assert tool.name == "read_file"
    assert tool.parent_span_id == chain.span_id
    assert tool.attributes["trace_sdk.input"] == detail["spans"][2]["input"]
    assert tool.attributes["trace_sdk.output"] == detail["spans"][2]["output"]
    assert tool.attributes["arguments"] == detail["spans"][1]["output"]["tool_calls"][0]["args"]
    assert chain.attributes["trace_sdk.input"]["skills_metadata"][0]["name"] == "hello"
    assert not any(s.operation_type in {"skill", "workflow", "state"} for s in trace.spans)
    assert not any("agentgate.evidence" in s.attributes for s in trace.spans)
    assert trace.final_state == {"execution_status": "success"}
    assert not any(s.operation_type == "routing" for s in trace.spans)


@pytest.mark.parametrize("raw", [
    {"nested": {"active": True, "items": [None, 2, 3.5]}},
    '{"nested":{"active":true,"items":[null,2,3.5]}}',
    "{'nested': {'active': True, 'items': [None, 2, 3.5]}}",
    {},
    "{}",
])
def test_tool_arguments_accept_only_data_objects(raw):
    detail = skill_detail()
    detail["spans"][1]["output"]["tool_calls"] = []
    detail["spans"][2]["input"] = raw
    trace = adapt_detail(detail)
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    expected = {} if raw in ({}, "{}") else {"nested": {"active": True, "items": [None, 2, 3.5]}}
    assert tool.attributes["arguments"].to_dict() == expected


@pytest.mark.parametrize("raw", [
    None, "", "not-an-object", "[]", "{'x': (1, 2)}", "{'x': {1, 2}}",
    "{1: 'x'}", "{'x': b'bytes'}", '{"x":1,"x":2}', "{'x':1, 'x':2}",
    "{'x': float('nan')}", "__import__('os').system('echo NEVER')",
    "{'x':" * 40 + "{}" + "}" * 40, " " * 65537,
])
def test_unparseable_arguments_remain_raw_without_empty_object_or_execution(raw, caplog):
    detail = skill_detail()
    detail["spans"][1]["output"]["tool_calls"] = []
    detail["spans"][2]["input"] = raw
    trace = adapt_detail(detail)
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert "arguments" not in tool.attributes
    assert tool.attributes["trace_sdk.input"] == raw
    assert tool.attributes["inbank.argument_diagnostic"] == "unparseable_arguments"
    assert not any(s.operation_type == "routing" for s in trace.spans)
    assert "inbank_trace_argument_unmapped" in caplog.text
    assert "NEVER" not in caplog.text


def test_nonfinite_json_keeps_existing_sdk_rejection():
    detail = skill_detail()
    detail["spans"][2]["input"] = '{"x":NaN}'
    with pytest.raises(TargetExecutionError, match="evidence is invalid"):
        adapt_detail(detail)


def test_matching_call_id_fills_missing_fields_without_overwriting_execution():
    detail = skill_detail()
    detail["spans"][2]["input"] = "unparseable"
    trace = adapt_detail(detail)
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["arguments"]["limit"] == 1000
    assert tool.attributes["inbank.argument_sources"][0]["path"].endswith(".0.args")
    detail["spans"][2]["input"] = "{'limit': 2000}"
    trace = adapt_detail(detail)
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["arguments"]["limit"] == 2000
    assert tool.attributes["arguments"]["file_path"].endswith("/hello/SKILL.md")
    assert tool.attributes["inbank.argument_diagnostic"] == "conflicting_arguments"
    assert sum(s.operation_type == "routing" for s in trace.spans) == 0


def test_argument_conflicts_are_type_sensitive():
    detail = skill_detail()
    detail["spans"][2]["input"] = "{'x': True}"
    detail["spans"][1]["output"]["tool_calls"][0]["args"] = {"x": 1}
    tool = next(s for s in adapt_detail(detail).spans if s.operation_type == "tool")
    assert tool.attributes["inbank.argument_diagnostic"] == "conflicting_arguments"
    assert tool.attributes["arguments"]["x"] is True


@pytest.mark.parametrize("encoding", ["object", "json", "literal"])
@pytest.mark.parametrize("catalog", [True, False])
def test_execution_arguments_may_include_parameters_omitted_by_request(encoding, catalog):
    detail = skill_detail(skills=("hello-ces-skills",))
    call = detail["spans"][1]["output"]["tool_calls"][0]
    call["args"].pop("limit")
    detail["spans"][0]["output"]["messages"][0]["tool_calls"] = [deepcopy(call)]
    if not catalog:
        detail["spans"][0]["input"]["skills_metadata"] = []
    executed = {**call["args"], "limit": 500}
    detail["spans"][2]["input"] = (
        executed if encoding == "object" else json.dumps(executed) if encoding == "json" else repr(executed)
    )
    original = deepcopy(detail)

    trace = adapt_detail(detail)

    assert detail == original
    assert trace == adapt_detail(detail)
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["arguments"] == executed
    assert tool.attributes["trace_sdk.input"] == original["spans"][2]["input"]
    assert "inbank.argument_diagnostic" not in tool.attributes
    assert [item["path"] for item in tool.attributes["inbank.argument_sources"]] == [
        "trace_sdk.input", "trace_sdk.output.tool_calls.0.args",
    ]
    assert not any(s.operation_type == "routing" for s in trace.spans)
    assert "inbank.skill_selection_diagnostics" not in trace.spans[0].attributes
    assert sum(s.operation_type == "tool" for s in trace.spans) == 1


@pytest.mark.parametrize("requested,executed", [
    ({}, {"limit": 500}),
    ({"filter": {"enabled": True}}, {"filter": {"enabled": True}, "limit": 500}),
    ({"items": [1, None]}, {"items": [1, None], "limit": 500}),
])
def test_observed_execution_superset_is_not_specific_to_read_file(requested, executed):
    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    call.update(name="lookup", args=requested)
    detail["spans"][2].update(name="lookup", toolName="lookup", input=repr(executed))
    detail["spans"][2]["output"]["name"] = "lookup"
    tool = next(s for s in adapt_detail(detail).spans if s.operation_type == "tool")
    assert tool.attributes["arguments"].to_dict() == executed
    assert "inbank.argument_diagnostic" not in tool.attributes


@pytest.mark.parametrize("requested,executed", [
    ({"limit": 1000}, {"limit": 500}),
    ({"limit": None}, {"limit": 500}),
    ({"limit": True}, {"limit": 1}),
    ({"limit": 500}, {"limit": "500"}),
    ({"options": {"enabled": True}}, {"options": {"enabled": 1}}),
    ({"items": [True]}, {"items": [1]}),
])
def test_argument_conflicts_do_not_change_reported_intent(requested, executed):
    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    path = call["args"]["file_path"]
    call["args"] = {"file_path": path, **requested}
    detail["spans"][2]["input"] = repr({"file_path": path, **executed})

    trace = reported_intent(adapt_detail(detail))

    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["inbank.argument_diagnostic"] == "conflicting_arguments"
    assert tool.attributes["arguments"].to_dict() == {"file_path": path, **executed}
    routes = [s for s in trace.spans if s.operation_type == "routing"]
    assert len(routes) == 1
    assert routes[0].attributes["selected_skill"] == "hello"
    assert "inbank.skill_selection_diagnostics" not in trace.spans[0].attributes


@pytest.mark.parametrize("raw", ["unparseable", None])
def test_request_omission_does_not_conflict_with_explicit_value(raw):
    detail = skill_detail()
    calls = detail["spans"][1]["output"]["tool_calls"]
    duplicate = deepcopy(calls[0])
    duplicate["args"].pop("limit")
    calls.append(duplicate)
    detail["spans"][2]["input"] = raw
    trace = adapt_detail(detail)
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["arguments"] == calls[0]["args"]
    assert "inbank.argument_diagnostic" not in tool.attributes
    assert sum(s.operation_type == "routing" for s in trace.spans) == 0


@pytest.mark.parametrize("with_tool", [True, False])
def test_repeated_tool_requests_do_not_duplicate_reported_intent(with_tool):
    detail = skill_detail()
    calls = detail["spans"][1]["output"]["tool_calls"]
    duplicate = deepcopy(calls[0])
    duplicate["args"]["limit"] = 2000
    calls.append(duplicate)
    if not with_tool:
        detail["spans"].pop()
        detail["trace"]["spanCount"] -= 1
    trace = reported_intent(adapt_detail(detail))
    routes = [s for s in trace.spans if s.operation_type == "routing"]
    assert len(routes) == 1
    assert routes[0].attributes["selected_skill"] == "hello"
    if with_tool:
        tool = next(s for s in trace.spans if s.operation_type == "tool")
        assert tool.attributes["inbank.argument_diagnostic"] == "conflicting_arguments"
        assert tool.attributes["arguments"]["limit"] == 1000


@pytest.mark.parametrize("path", ["/mnt/oneagent-skills/bundle/skills/other/SKILL.md", None])
def test_disagreeing_file_paths_do_not_create_skill_diagnostics(path):
    detail = skill_detail()
    args = dict(detail["spans"][1]["output"]["tool_calls"][0]["args"])
    args["file_path"] = path
    detail["spans"][2]["input"] = repr(args)
    trace = adapt_detail(detail)
    assert not any(s.operation_type == "routing" for s in trace.spans)
    assert "inbank.skill_selection_diagnostics" not in trace.spans[0].attributes


@pytest.mark.parametrize("expected,outcome", [(500, "pass"), (1000, "fail")])
def test_existing_rules_check_observed_extra_parameter_without_false_pass(expected, outcome):
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.evaluator.rule.routing import SkillRoutingEvaluator
    from agentgate.evaluator.rule.tool_use import ToolArgumentsEvaluator

    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    call["args"].pop("limit")
    detail["spans"][2]["input"] = repr({**call["args"], "limit": 500})
    trace = reported_intent(adapt_detail(detail))
    turn = CaseTurn(id="first", input={"txt": "hello"}, expectations=(
        SkillRouteExpectation(condition=Equals(expected="hello")),
        ToolArgumentExpectation(tool="read_file", path="arguments.limit", condition=Equals(expected=expected)),
    ))
    route = SkillRoutingEvaluator().evaluate(
        evaluator_spec("skill_routing"), turn, trace.for_turn(turn.id), resolve_unexpected,
    ).checks[0]
    argument = ToolArgumentsEvaluator().evaluate(
        evaluator_spec("tool_arguments"), turn, trace.for_turn(turn.id), resolve_unexpected,
    ).checks[0]
    assert route.outcome == "pass" and route.actual == "hello"
    assert argument.outcome == outcome and argument.actual == 500
    assert not argument.actual_missing


@pytest.mark.parametrize("expected,outcome", [(500, "pass"), (1000, "fail")])
def test_argument_difference_uses_execution_value_independently_of_expectation(expected, outcome):
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.evaluator.rule.routing import SkillRoutingEvaluator
    from agentgate.evaluator.rule.tool_use import ToolArgumentsEvaluator

    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    detail["spans"][2]["input"] = repr({**call["args"], "limit": 500})
    trace = reported_intent(adapt_detail(detail))
    turn = CaseTurn(id="first", input={"txt": "hello"}, expectations=(
        SkillRouteExpectation(condition=Equals(expected="hello")),
        ToolArgumentExpectation(tool="read_file", path="arguments.file_path",
                                condition=Equals(expected=call["args"]["file_path"])),
        ToolArgumentExpectation(tool="read_file", path="arguments.limit", condition=Equals(expected=expected)),
    ))
    route = SkillRoutingEvaluator().evaluate(
        evaluator_spec("skill_routing"), turn, trace.for_turn(turn.id), resolve_unexpected,
    ).checks[0]
    arguments = ToolArgumentsEvaluator().evaluate(
        evaluator_spec("tool_arguments"), turn, trace.for_turn(turn.id), resolve_unexpected,
    ).checks
    assert route.outcome == "pass"
    assert arguments[0].outcome == "pass" and not arguments[0].actual_missing
    assert arguments[1].outcome == outcome and arguments[1].actual == 500
    assert not arguments[1].actual_missing
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["inbank.argument_diagnostic"] == "conflicting_arguments"


@pytest.mark.parametrize("executed", [{}, {"limit": 500}, {"limit": None}, {"limit": False}, {"limit": 0}])
def test_only_missing_execution_fields_are_filled_from_linked_request(executed):
    detail = skill_detail()
    requested = detail["spans"][1]["output"]["tool_calls"][0]["args"]
    detail["spans"][2]["input"] = repr(executed)
    original = deepcopy(detail)
    trace = adapt_detail(detail)
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["arguments"] == {**requested, **executed}
    assert tool.attributes["trace_sdk.input"] == original["spans"][2]["input"]
    assert detail == original
    assert len(tool.attributes["inbank.argument_sources"]) == 2
    assert sum(s.operation_type == "routing" for s in trace.spans) == 0
    assert ("inbank.argument_diagnostic" in tool.attributes) == ("limit" in executed)


@pytest.mark.parametrize("raw", ["unparseable", None, "{'encoding': 'utf-8'}"])
@pytest.mark.parametrize("reverse_requests", [True, False])
def test_unresolved_request_field_does_not_hide_other_fields(raw, reverse_requests):
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.evaluator.rule.routing import SkillRoutingEvaluator
    from agentgate.evaluator.rule.tool_use import ToolArgumentsEvaluator

    detail = skill_detail()
    calls = detail["spans"][1]["output"]["tool_calls"]
    path = calls[0]["args"]["file_path"]
    duplicate = deepcopy(calls[0])
    duplicate["args"]["limit"] = 2000
    calls.append(duplicate)
    if reverse_requests:
        calls.reverse()
    detail["spans"][2]["input"] = raw
    trace = reported_intent(adapt_detail(detail))
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["arguments"]["file_path"] == path
    assert "limit" not in tool.attributes["arguments"]
    if raw == "{'encoding': 'utf-8'}":
        assert tool.attributes["arguments"]["encoding"] == "utf-8"
    assert tool.attributes["inbank.argument_diagnostic"] == "conflicting_arguments"
    turn = CaseTurn(id="first", input={"txt": "hello"}, expectations=(
        SkillRouteExpectation(condition=Equals(expected="hello")),
        ToolArgumentExpectation(tool="read_file", path="arguments.file_path", condition=Equals(expected=path)),
        ToolArgumentExpectation(tool="read_file", path="arguments.limit", condition=Equals(expected=1000)),
    ))
    route = SkillRoutingEvaluator().evaluate(
        evaluator_spec("skill_routing"), turn, trace.for_turn(turn.id), resolve_unexpected,
    ).checks[0]
    checks = ToolArgumentsEvaluator().evaluate(
        evaluator_spec("tool_arguments"), turn, trace.for_turn(turn.id), resolve_unexpected,
    ).checks
    assert route.outcome == "pass"
    assert checks[0].outcome == "pass" and not checks[0].actual_missing
    assert checks[1].outcome == "fail" and checks[1].actual_missing


@pytest.mark.parametrize("executed,requests,expected,conflict", [
    ({"options": {"limit": 500}}, [{"options": {"limit": 1000, "encoding": "utf-8"}}],
     {"options": {"limit": 500, "encoding": "utf-8"}}, True),
    ({}, [{"options": {"limit": 500, "encoding": "utf-8"}}, {"options": {"limit": 1000}}],
     {"options": {"encoding": "utf-8"}}, True),
    ({"options": None}, [{"options": {"limit": 500}}], {"options": None}, True),
    ({"items": []}, [{"items": [1, 2]}], {"items": []}, True),
    ({}, [{"items": [1, 2]}, {"items": [1, 3]}], {}, True),
    ({}, [{"items": [True]}, {"items": [1]}], {}, True),
    ({}, [{"options": {"limit": 500}}, {"options": {"encoding": "utf-8"}}],
     {"options": {"limit": 500, "encoding": "utf-8"}}, False),
    ({"options": {"limit": 500}}, [{"options": "not-an-object"}], {"options": {"limit": 500}}, True),
    ({}, [{"options": None}, {"options": {"limit": 500}}], {}, True),
])
def test_parameter_evidence_is_resolved_per_field_including_nested_objects(executed, requests, expected, conflict):
    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    call["name"] = "lookup"
    detail["spans"][1]["output"]["tool_calls"] = [{**call, "args": args} for args in requests]
    detail["spans"][2].update(name="lookup", toolName="lookup", input=repr(executed))
    detail["spans"][2]["output"]["name"] = "lookup"
    original = deepcopy(detail)
    trace = adapt_detail(detail)
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["arguments"].to_dict() == expected
    assert (tool.attributes.get("inbank.argument_diagnostic") == "conflicting_arguments") == conflict
    assert detail == original
    assert trace == adapt_detail(detail)


@pytest.mark.parametrize("association", ["other_id", "missing_id", "other_name", "duplicate_tools"])
def test_missing_fields_are_not_filled_from_ambiguous_or_unrelated_calls(association):
    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    if association == "other_id":
        call["id"] = "unrelated-call"
    elif association == "missing_id":
        call.pop("id")
    elif association == "other_name":
        call["name"] = "other-tool"
    else:
        detail["spans"].append({**detail["spans"][2], "id": "tool-copy", "eventId": "tool-copy"})
        detail["trace"]["spanCount"] += 1
    detail["spans"][2]["input"] = "{'encoding': 'utf-8'}"
    trace = adapt_detail(detail)
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert tool.attributes["arguments"] == {"encoding": "utf-8"}
    assert "limit" not in tool.attributes.get("arguments", {})
    assert "file_path" not in tool.attributes.get("arguments", {})
    if association in {"other_name", "duplicate_tools"}:
        assert tool.attributes["inbank.argument_diagnostic"] == "ambiguous_tool_call"


@pytest.mark.parametrize("association", ["other_name", "duplicate_tools"])
@pytest.mark.parametrize("encoding", ["object", "json", "literal"])
def test_ambiguous_association_preserves_each_tools_own_arguments(association, encoding):
    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    if association == "other_name":
        call["name"] = "other-tool"
    else:
        detail["spans"].append({**deepcopy(detail["spans"][2]), "id": "tool-copy", "eventId": "tool-copy"})
        detail["trace"]["spanCount"] += 1
    expected = []
    for index, tool in enumerate(detail["spans"][2:]):
        arguments = {"limit": 500 + index, "options": {"enabled": False}, "cursor": None}
        expected.append(arguments)
        tool["input"] = (arguments if encoding == "object" else
                         json.dumps(arguments) if encoding == "json" else repr(arguments))
    original = deepcopy(detail)

    trace = adapt_detail(detail)

    tools = [span for span in trace.spans if span.operation_type == "tool"]
    assert [tool.attributes["arguments"].to_dict() for tool in tools] == expected
    for tool in tools:
        assert tool.attributes["inbank.argument_diagnostic"] == "ambiguous_tool_call"
        assert [source.to_dict() for source in tool.attributes["inbank.argument_sources"]] == [
            {"span_id": tool.span_id, "path": "trace_sdk.input"},
        ]
    assert detail == original
    assert trace == adapt_detail(detail)


@pytest.mark.parametrize("association", ["other_name", "duplicate_tools"])
@pytest.mark.parametrize("raw", [None, "invalid execution arguments"])
def test_ambiguous_association_with_unparseable_input_never_borrows_request_arguments(association, raw):
    detail = skill_detail()
    detail["spans"][2]["input"] = raw
    if association == "other_name":
        detail["spans"][1]["output"]["tool_calls"][0]["name"] = "other-tool"
    else:
        detail["spans"].append({**deepcopy(detail["spans"][2]), "id": "tool-copy", "eventId": "tool-copy"})
        detail["trace"]["spanCount"] += 1
    trace = adapt_detail(detail)
    for tool in (s for s in trace.spans if s.operation_type == "tool"):
        assert "arguments" not in tool.attributes
        assert "inbank.argument_sources" not in tool.attributes
        assert tool.attributes["inbank.argument_diagnostic"] == "ambiguous_tool_call"


@pytest.mark.parametrize("same_path", [True, False])
@pytest.mark.parametrize("with_request", [True, False])
def test_duplicate_call_ids_keep_each_observed_tools_input(same_path, with_request):
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.evaluator.rule.tool_use import (
        ForbiddenToolEvaluator,
        RequiredToolEvaluator,
        ToolArgumentsEvaluator,
    )

    detail = skill_detail()
    if not with_request:
        detail["spans"][1]["output"]["tool_calls"] = []
    duplicate = {**deepcopy(detail["spans"][2]), "id": "tool-copy", "eventId": "tool-copy"}
    duplicate_args = {
        "file_path": f"/mnt/oneagent-skills/bundle/skills/{'hello' if same_path else 'other'}/SKILL.md",
        "limit": 500,
    }
    duplicate["input"] = repr(duplicate_args)
    duplicate["startedAt"] = "2026-10-08T00:00:00.004Z"
    detail["spans"].append(duplicate)
    detail["trace"]["spanCount"] += 1
    original = deepcopy(detail)
    trace = adapt_detail(detail)
    tools = [s for s in trace.spans if s.operation_type == "tool"]
    assert len(tools) == 2
    assert tools[0].attributes["arguments"]["limit"] == 1000
    assert tools[1].attributes["arguments"] == duplicate_args
    routes = [s for s in trace.spans if s.operation_type == "routing"]
    assert routes == []
    turn = CaseTurn(id="first", input={"txt": "hello"}, expectations=(
        ToolCallExpectation(tool="read_file"),
        ToolCallExpectation(tool="read_file", mode="forbidden"),
        ToolCallExpectation(tool="delete", mode="forbidden"),
        ToolArgumentExpectation(tool="read_file", path="arguments.limit", occurrence="first",
                                condition=Equals(expected=1000)),
        ToolArgumentExpectation(tool="read_file", path="arguments.limit", occurrence="last",
                                condition=Equals(expected=500)),
    ))
    for evaluator, expected in [
        (RequiredToolEvaluator(), ["pass"]),
        (ForbiddenToolEvaluator(), ["fail", "pass"]),
        (ToolArgumentsEvaluator(), ["pass", "pass"]),
    ]:
        result = evaluator.evaluate(evaluator_spec(evaluator.implementation_id), turn, trace, resolve_unexpected)
        assert [check.outcome for check in result.checks] == expected
        assert not any(check.actual_missing for check in result.checks)
    assert len({s.span_id for s in trace.spans}) == len(trace.spans)
    assert detail == original
    assert trace == adapt_detail(detail)
    detail["spans"].reverse()
    reordered = adapt_detail(detail)
    assert routes == [s for s in reordered.spans if s.operation_type == "routing"]
    assert tools == [s for s in reordered.spans if s.operation_type == "tool"]


@pytest.mark.parametrize("raw", [None, "invalid arguments", {}, {"limit": 500}])
@pytest.mark.parametrize("with_request", [True, False])
def test_incomplete_duplicate_tools_preserve_observed_tool_count(raw, with_request):
    detail = skill_detail()
    if not with_request:
        detail["spans"][1]["output"]["tool_calls"] = []
    detail["spans"].append({
        **deepcopy(detail["spans"][2]), "id": "tool-copy", "eventId": "tool-copy", "input": raw,
    })
    detail["trace"]["spanCount"] += 1
    trace = adapt_detail(detail)
    routes = [s for s in trace.spans if s.operation_type == "routing"]
    assert routes == []
    assert "inbank.skill_selection_diagnostics" not in trace.spans[0].attributes
    assert sum(s.operation_type == "tool" for s in trace.spans) == 2


@pytest.mark.parametrize("conflict", ["tool_name", "null_path"])
@pytest.mark.parametrize("with_request", [True, False])
def test_duplicate_tool_identity_differences_do_not_generate_skill_diagnostics(conflict, with_request):
    detail = skill_detail()
    if not with_request:
        detail["spans"][1]["output"]["tool_calls"] = []
    duplicate = {**deepcopy(detail["spans"][2]), "id": "tool-copy", "eventId": "tool-copy"}
    if conflict == "tool_name":
        duplicate.update(name="another_tool", toolName="another_tool")
        duplicate["output"]["name"] = "another_tool"
    else:
        duplicate["input"] = {"file_path": None, "limit": 500}
    detail["spans"].append(duplicate)
    detail["trace"]["spanCount"] += 1
    trace = adapt_detail(detail)
    assert not any(s.operation_type == "routing" for s in trace.spans)
    assert "inbank.skill_selection_diagnostics" not in trace.spans[0].attributes
    tools = [s for s in trace.spans if s.operation_type == "tool"]
    assert len(tools) == 2 and all("arguments" in s.attributes for s in tools)


@pytest.mark.parametrize("with_request", [True, False])
def test_distinct_tool_calls_are_not_deduplicated(with_request):
    detail = skill_detail(skills=("hello", "hello"))
    if not with_request:
        detail["spans"][1]["output"]["tool_calls"] = []
    trace = adapt_detail(detail)
    routes = [s for s in trace.spans if s.operation_type == "routing"]
    assert routes == []
    assert [s.attributes["tool_call_id"] for s in trace.spans if s.operation_type == "tool"] == ["call-0", "call-1"]
    assert len({s.span_id for s in trace.spans}) == len(trace.spans)


@pytest.mark.parametrize("assembly", ["per_turn", "case"])
def test_repeated_call_ids_are_resolved_within_each_turn_only(assembly):
    request = execution_request(two_turns=True)
    evidence, outcomes = {}, {}
    turns = []
    for turn, skill in zip(request.case.turns, ("hello", "other")):
        source_id = f"source-{turn.id}"
        detail = skill_detail(source=source_id, skills=(skill,))
        detail["spans"].append({
            **deepcopy(detail["spans"][2]), "id": "tool-copy", "eventId": "tool-copy",
        })
        detail["trace"].update(input={"txt": "hello"}, output="answer", spanCount=len(detail["spans"]))
        if assembly == "per_turn":
            turns.append(adapt_detail(detail, request, turn))
        else:
            client = Mock(fetch_events=Mock(return_value=detail_to_events(detail, [])))
            evidence[turn.id] = fetch(client, chat({"project_id": "inbank-tests", "trace_id": source_id}))
            outcomes[turn.id] = {"input": turn.input, "output": {"output": "answer"}, "state": {}}
    if assembly == "case":
        trace = assemble_evidence(request, "inbank-tests", evidence, outcomes)
        assert len({s.span_id for s in trace.spans}) == len(trace.spans)
        turns = [trace.for_turn(turn.id) for turn in request.case.turns]
    tool_ids = []
    for trace, turn, skill in zip(turns, request.case.turns, ("hello", "other")):
        assert not any(s.operation_type == "routing" for s in trace.spans)
        tools = [s for s in trace.spans if s.operation_type == "tool"]
        assert len(tools) == 2
        assert trace.spans[0].attributes["agentgate.turn.id"] == turn.id
        assert all(s.attributes["arguments"]["file_path"].endswith(f"/{skill}/SKILL.md") for s in tools)
        tool_ids.extend(s.span_id for s in tools)
        assert "inbank.skill_selection_diagnostics" not in trace.spans[0].attributes
    assert len(tool_ids) == len(set(tool_ids))


@pytest.mark.parametrize("request_args", [None, "invalid request arguments", {}, {"limit": 1000}])
@pytest.mark.parametrize("encoding", ["object", "json", "literal"])
def test_execution_arguments_survive_incomplete_requests(request_args, encoding):
    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    executed = dict(call["args"])
    call["args"] = request_args
    detail["spans"][2]["input"] = (executed if encoding == "object" else
                                   json.dumps(executed) if encoding == "json" else repr(executed))
    original = deepcopy(detail)

    trace = adapt_detail(detail)

    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert not any(s.operation_type == "routing" for s in trace.spans)
    assert tool.attributes["arguments"] == executed
    assert "inbank.skill_selection_diagnostics" not in trace.spans[0].attributes
    assert detail == original
    assert trace == adapt_detail(detail)


@pytest.mark.parametrize("request_args", [None, "invalid request arguments", {}, {"limit": 2000}])
@pytest.mark.parametrize("incomplete_first", [True, False])
@pytest.mark.parametrize("with_tool", [True, False])
def test_incomplete_request_copies_do_not_infer_skill_from_file_path(request_args, incomplete_first, with_tool):
    detail = skill_detail()
    calls = detail["spans"][1]["output"]["tool_calls"]
    calls.append({**deepcopy(calls[0]), "args": request_args})
    if incomplete_first:
        calls.reverse()
    if not with_tool:
        detail["spans"].pop()
        detail["trace"]["spanCount"] -= 1
    trace = adapt_detail(detail)
    assert not any(s.operation_type == "routing" for s in trace.spans)
    assert "inbank.skill_selection_diagnostics" not in trace.spans[0].attributes
    assert sum(s.operation_type == "tool" for s in trace.spans) == int(with_tool)


@pytest.mark.parametrize("request_args", [None, "invalid request arguments", {}, {"limit": 1000}])
def test_incomplete_request_without_execution_path_does_not_invent_skill_or_tool(request_args):
    detail = skill_detail()
    detail["spans"][1]["output"]["tool_calls"][0]["args"] = request_args
    detail["spans"].pop()
    detail["trace"]["spanCount"] -= 1
    trace = adapt_detail(detail)
    assert not any(s.operation_type in {"tool", "routing"} for s in trace.spans)


def test_missing_intent_does_not_use_observed_skill_path():
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.evaluator.rule.routing import SkillRoutingEvaluator

    detail = skill_detail()
    detail["spans"][1]["output"]["tool_calls"][0]["args"] = None
    detail["spans"][2]["input"] = "{'file_path': '/mnt/oneagent-skills/bundle/skills/other/SKILL.md'}"
    trace = adapt_detail(detail)
    turn = CaseTurn(id="first", input={"txt": "hello"}, expectations=(
        SkillRouteExpectation(condition=Equals(expected="hello")),
    ))
    check = SkillRoutingEvaluator().evaluate(
        evaluator_spec("skill_routing"), turn, trace, resolve_unexpected,
    ).checks[0]
    assert check.outcome == "fail" and check.actual is None
    assert check.actual_missing


@pytest.mark.parametrize("path", [None, "", 123, "/mnt/oneagent-skills/bundle/skills/other/SKILL.md"])
@pytest.mark.parametrize("with_tool", [True, False])
def test_explicit_request_path_contradiction_is_not_treated_as_missing(path, with_tool):
    detail = skill_detail()
    calls = detail["spans"][1]["output"]["tool_calls"]
    copy = deepcopy(calls[0])
    copy["args"]["file_path"] = path
    calls.append(copy)
    if not with_tool:
        detail["spans"].pop()
        detail["trace"]["spanCount"] -= 1
    trace = adapt_detail(detail)
    assert not any(s.operation_type == "routing" for s in trace.spans)
    assert "inbank.skill_selection_diagnostics" not in trace.spans[0].attributes


@pytest.mark.parametrize("problem", ["argument_difference", "other_name", "duplicate_tools", "bad_request"])
@pytest.mark.parametrize("tool_status", ["success", "error"])
def test_tool_presence_forbidden_and_argument_checks_remain_independent(problem, tool_status):
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.evaluator.rule.tool_use import (
        ForbiddenToolEvaluator,
        RequiredToolEvaluator,
        ToolArgumentsEvaluator,
    )

    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    detail["spans"][2].update(input=repr({**call["args"], "limit": 500}), status=tool_status)
    if problem == "other_name":
        call["name"] = "another-tool"
    elif problem == "duplicate_tools":
        detail["spans"].append({**deepcopy(detail["spans"][2]), "id": "tool-copy", "eventId": "tool-copy"})
        detail["trace"]["spanCount"] += 1
    elif problem == "bad_request":
        call["args"] = "invalid request arguments"
    trace = adapt_detail(detail)
    turn = CaseTurn(id="first", input={"txt": "hello"}, expectations=(
        ToolCallExpectation(tool="read_file"),
        ToolCallExpectation(tool="read_file", mode="forbidden"),
        ToolCallExpectation(tool="delete", mode="forbidden"),
        ToolArgumentExpectation(tool="read_file", path="arguments.limit", occurrence="all",
                                condition=Equals(expected=500)),
        ToolArgumentExpectation(tool="read_file", path="arguments.limit", occurrence="all",
                                condition=Equals(expected=1000)),
    ))
    for evaluator, expected in [
        (RequiredToolEvaluator(), ["pass"]),
        (ForbiddenToolEvaluator(), ["fail", "pass"]),
        (ToolArgumentsEvaluator(), ["pass", "fail"]),
    ]:
        evaluation = evaluator.evaluate(evaluator_spec(evaluator.implementation_id), turn, trace, resolve_unexpected)
        assert [check.outcome for check in evaluation.checks] == expected
        assert not any(check.actual_missing for check in evaluation.checks)


@pytest.mark.parametrize("mode", ["catalog_only", "historical_input", "plain_file"])
def test_available_skills_and_historical_messages_are_not_selections(mode):
    detail = skill_detail()
    if mode == "plain_file":
        detail["spans"][1]["output"]["tool_calls"][0]["args"]["file_path"] = "/tmp/readme.txt"
        detail["spans"][2]["input"] = repr(detail["spans"][1]["output"]["tool_calls"][0]["args"])
    else:
        # The chain output still contains a copy, but does not create another call.
        detail["spans"][1]["input"] = deepcopy(detail["spans"][0]["output"]["messages"])
        detail["spans"][1]["output"]["tool_calls"] = []
        detail["spans"] = detail["spans"][:2]
        detail["trace"]["spanCount"] = 2
    trace = adapt_detail(detail)
    assert not any(s.operation_type == "routing" for s in trace.spans)
    if mode != "plain_file":
        assert not any(s.operation_type == "tool" for s in trace.spans)


@pytest.mark.parametrize("mode", ["request_only", "tool_only", "tool_error", "no_call_id"])
def test_reported_intent_does_not_claim_skill_execution(mode):
    detail = skill_detail()
    if mode == "request_only":
        detail["spans"] = detail["spans"][:2]
        detail["trace"]["spanCount"] = 2
    elif mode == "tool_only":
        detail["spans"][1]["output"]["tool_calls"] = []
    elif mode == "tool_error":
        detail["spans"][2]["status"] = "error"
    else:
        detail["spans"][2]["output"].pop("tool_call_id")
    trace = reported_intent(adapt_detail(detail))
    routes = [s for s in trace.spans if s.operation_type == "routing"]
    assert len(routes) == 1
    assert routes[0].attributes["selected_skill"] == "hello"
    assert routes[0].status == "unset"
    assert sum(s.operation_type == "tool" for s in trace.spans) == (mode != "request_only")
    assert not any(s.operation_type == "skill" for s in trace.spans)





@pytest.mark.parametrize("damage", ["catalog", "call_arguments", "call_name", "duplicate_tool_path", "path"])
def test_skill_metadata_conflicts_do_not_generate_skill_diagnostics(damage, caplog):
    detail = skill_detail()
    call = detail["spans"][1]["output"]["tool_calls"][0]
    if damage == "catalog":
        detail["spans"][0]["input"]["skills_metadata"].append({
            **detail["spans"][0]["input"]["skills_metadata"][0], "name": "other",
        })
    elif damage in {"call_arguments", "call_name"}:
        duplicate = deepcopy(call)
        if damage == "call_arguments":
            duplicate["args"]["file_path"] = "/mnt/oneagent-skills/bundle/skills/other/SKILL.md"
        else:
            duplicate["name"] = "another_tool"
        detail["spans"][1]["output"]["tool_calls"].append(duplicate)
    elif damage == "duplicate_tool_path":
        detail["spans"].append({
            **detail["spans"][2], "id": "tool-copy", "eventId": "tool-copy",
            "input": {"file_path": "/mnt/oneagent-skills/bundle/skills/other/SKILL.md"},
        })
        detail["trace"]["spanCount"] += 1
    else:
        call["args"]["file_path"] = "/mnt/oneagent-skills/bundle/skills/../SKILL.md"
        detail["spans"][2]["input"] = repr(call["args"])
    trace = adapt_detail(detail)
    assert not any(s.operation_type == "routing" for s in trace.spans)
    assert "inbank_trace_skill_unmapped" not in caplog.text


def test_multiple_skill_file_reads_remain_tools_without_routes():
    detail = skill_detail(skills=("text-score", "text-summary"))
    calls = detail["spans"][1]["output"]["tool_calls"]
    calls.append(deepcopy(calls[0]))
    trace = adapt_detail(detail)
    routes = [s for s in trace.spans if s.operation_type == "routing"]
    assert routes == []
    assert len({s.span_id for s in trace.spans}) == len(trace.spans)
    assert [s.sequence for s in trace.spans] == list(range(len(trace.spans)))
    assert sum(s.operation_type == "tool" for s in trace.spans) == 2


@pytest.mark.parametrize("intent", ["hello", "transfer-routing", "账户查询", " transfer-routing \n"])
def test_yunxia_message_intent_preserves_observed_trace_and_tools(intent):
    trace = adapt_detail(skill_detail(skills=("hello", "other")))
    original = trace.model_dump(mode="json")
    message = {"intent_code": intent, "output": "answer", "slots": {"amount": 42}}
    original_message = deepcopy(message)

    spans = project_yunxia_intent_selection(trace.spans, message)
    projected = Trace(**{**trace.model_dump(), "spans": spans})

    assert trace.model_dump(mode="json") == original
    assert message == original_message
    assert [s.model_dump(exclude={"sequence"}) for s in spans if s.operation_type != "routing"] == [
        s.model_dump(exclude={"sequence"}) for s in trace.spans if s.operation_type != "routing"
    ]
    routes = [s for s in spans if s.operation_type == "routing"]
    assert len(routes) == 1
    route = routes[0]
    root = spans[0]
    assert route.attributes["selected_skill"] == intent.strip()
    assert route.attributes["agentgate.turn.id"] == "first"
    assert route.attributes["inbank.skill_selection"]["basis"] == "message_intent_code"
    assert route.attributes["inbank.skill_selection"]["source_path"] == "message.intent_code"
    assert route.attributes["inbank.skill_selection"]["source_span_id"] == root.span_id
    assert route.parent_span_id == root.span_id
    assert route.started_at == route.ended_at == root.ended_at
    assert route.status == "unset"
    assert all(s.operation_type != "skill" for s in spans)
    assert [s.sequence for s in spans] == list(range(len(spans)))
    assert projected.for_turn("first").spans == spans
    assert project_yunxia_intent_selection(spans, message) == spans


@pytest.mark.parametrize("message", [
    {}, {"intentCode": "hello"}, {"intent_code": None}, {"intent_code": ""},
    {"intent_code": " \t\n"}, {"intent_code": 123}, {"intent_code": False},
    {"intent_code": []}, {"intent_code": {"name": "hello"}},
])
def test_yunxia_missing_intent_does_not_fall_back_to_skill_file_or_previous_projection(message):
    trace = adapt_detail(skill_detail())
    previous = project_yunxia_intent_selection(trace.spans, {"intent_code": "previous"})
    for source in (trace.spans, previous):
        spans = project_yunxia_intent_selection(source, message)
        assert not any(s.operation_type == "routing" for s in spans)
        assert sum(s.operation_type == "tool" for s in spans) == 1
        assert spans[0].attributes == trace.spans[0].attributes
        Trace(**{**trace.model_dump(), "spans": spans})


@pytest.mark.parametrize("trace_available", [True, False])
@pytest.mark.parametrize("expected,outcome", [("transfer-routing", "pass"), ("other", "fail")])
def test_yunxia_message_route_is_usable_without_a_skill_read(trace_available, expected, outcome):
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.evaluator.rule.routing import SkillRoutingEvaluator

    request = execution_request()
    turn = request.case.turns[0].model_copy(update={"expectations": (
        SkillRouteExpectation(condition=Equals(expected=expected)),
    )})
    trace = adapt_detail(skill_detail(skills=()), request, turn)
    turn_spans = trace.spans
    if not trace_available:
        root = trace.spans[0]
        turn_spans = (output_only_turn_span(
            request, turn, adapter="yunxia", project_id="inbank-tests", request_id="request",
            started_at=root.started_at, ended_at=root.ended_at,
            error=TargetExecutionError("unavailable", "trace not found"),
        ),)
    message = {"intent_code": "transfer-routing", "output": "answer"}
    spans = project_yunxia_intent_selection(turn_spans, message)
    projected = Trace(**{**trace.model_dump(), "spans": spans})

    assert spans[0] == turn_spans[0]
    assert spans[0].attributes["inbank.evidence_mode"] == (
        "trace_server" if trace_available else "output_only"
    )
    assert not any(s.operation_type in {"tool", "skill"} for s in spans)
    check = SkillRoutingEvaluator().evaluate(
        evaluator_spec("skill_routing"), turn, projected.for_turn(turn.id), resolve_unexpected,
    ).checks[0]
    assert check.actual == "transfer-routing"
    assert check.outcome == outcome
    assert check.span_ids == (spans[1].span_id,)


@pytest.mark.parametrize("second_intent", ["hello", "transfer-routing", None])
def test_yunxia_message_intent_is_scoped_to_each_turn(second_intent):
    request = execution_request(two_turns=True)
    spans, outcomes = [], {}
    for index, turn in enumerate(request.case.turns):
        detail = skill_detail(f"source-{index}", skills=("hello",) if index == 0 else ())
        trace = adapt_detail(detail, request, turn)
        message = {"intent_code": "hello" if index == 0 else second_intent, "output": "answer"}
        for span in project_yunxia_intent_selection(trace.spans, message):
            spans.append(span.model_copy(update={"sequence": len(spans)}))
        outcomes[turn.id] = {"input": turn.input, "output": message, "state": {}}
    combined = Trace(
        trace_id=trace.trace_id, run_id=request.run_id, case_id=request.case.id,
        spans=tuple(spans), turn_outcomes=outcomes,
        final_output=outcomes["second"]["output"],
    )
    for turn_id, expected in [("first", ["hello"]),
                              ("second", [] if second_intent is None else [second_intent])]:
        scoped = combined.for_turn(turn_id)
        routes = [s for s in scoped.spans if s.operation_type == "routing"]
        assert [s.attributes["selected_skill"] for s in routes] == expected
        assert all(s.attributes["agentgate.turn.id"] == turn_id for s in routes)


def test_yunxia_projection_preserves_customer_routing_spans():
    trace = adapt_detail(skill_detail())
    inferred = next(s for s in project_yunxia_intent_selection(
        trace.spans, {"intent_code": "initial"},
    ) if s.operation_type == "routing")
    observed = inferred.model_copy(update={
        "name": "customer.routing", "span_id": "f" * 16,
        "attributes": FrozenJsonObject({"selected_skill": "observed", "agentgate.turn.id": "first"}),
    })
    source = (*trace.spans, observed)
    spans = project_yunxia_intent_selection(source, {"intent_code": "reported"})
    preserved = next(s for s in spans if s.span_id == observed.span_id)
    assert preserved.model_dump(exclude={"sequence"}) == observed.model_dump(exclude={"sequence"})
    assert [s.attributes["selected_skill"] for s in spans if s.operation_type == "routing"] == [
        "reported", "observed",
    ]


def test_existing_rules_consume_adapted_fields_without_new_status_protocol():
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.evaluator.rule.execution import ExecutionPathEvaluator
    from agentgate.evaluator.rule.routing import SkillRoutingEvaluator
    from agentgate.evaluator.rule.tool_use import (
        ForbiddenToolEvaluator,
        RequiredToolEvaluator,
        ToolArgumentsEvaluator,
    )

    trace = reported_intent(adapt_detail(skill_detail()))
    expectations = (
        SkillRouteExpectation(condition=Equals(expected="hello")),
        ToolCallExpectation(tool="read_file"),
        ToolCallExpectation(tool="delete", mode="forbidden"),
        ToolCallExpectation(tool="read_file", mode="forbidden"),
        ToolArgumentExpectation(tool="read_file", path="arguments.limit", condition=Equals(expected=1000)),
        ExecutionPathExpectation(scope="tool", expected=("read_file",)),
    )
    turn = CaseTurn(id="first", input={"txt": "hello"}, expectations=expectations)
    for evaluator, expected in [
        (SkillRoutingEvaluator(), ["pass"]),
        (RequiredToolEvaluator(), ["pass"]),
        (ForbiddenToolEvaluator(), ["pass", "fail"]),
        (ToolArgumentsEvaluator(), ["pass"]),
        (ExecutionPathEvaluator(), ["pass"]),
    ]:
        result = evaluator.evaluate(evaluator_spec(evaluator.implementation_id), turn, trace, resolve_unexpected)
        assert [c.outcome for c in result.checks] == expected
        assert all(c.span_ids for c in result.checks if c.outcome == "fail")


@pytest.mark.parametrize("selection", ["final_output", "output_and_tools", "full_trajectory"])
def test_existing_judge_request_consumes_adapted_trace_with_redaction(selection):
    from test_judge_prompt import redact

    from agentgate.evaluator.judge.prompt import build_judge_request

    detail = skill_detail()
    detail["spans"][2]["output"]["content"] = "raw-secret tool result"
    trace = reported_intent(adapt_detail(detail))
    built = build_judge_request(
        model_id="mock-only", instruction="Evaluate the provided execution.",
        rubric={"accuracy": "Use the observed evidence."}, case=execution_request().case,
        trace=trace, input_selection=selection, pass_threshold=0.8, temperature=0, seed=1,
        max_output_tokens=256, timeout_seconds=1, max_input_chars=100000, redact=redact,
    )
    material = json.loads(built.user_prompt.split("Evidence:\n", 1)[1])
    assert material["final_output"]["output"] == "dialogue answer"
    assert "raw-secret" not in built.user_prompt
    if selection != "final_output":
        assert material["tool_calls"][0]["name"] == "read_file"
        assert material["tool_calls"][0]["arguments"]["arguments"]["limit"] == 1000
        assert material["tool_calls"][0]["arguments"]["trace_sdk.output"]["content"] == "[redacted] tool result"
    if selection == "full_trajectory":
        route = next(s for s in material["trajectory"] if s["operation_type"] == "routing")
        assert route["attributes"]["selected_skill"] == "hello"
        assert route["attributes"]["inbank.skill_selection"]["derived"] is True


@pytest.mark.parametrize("occurrence,expected", [
    ("first", "pass"), ("last", "fail"), ("any", "pass"), ("all", "fail"),
])
def test_adaptation_keeps_existing_tool_argument_occurrence_semantics(occurrence, expected):
    from test_rule_tool_use import evaluator_spec, resolve_unexpected

    from agentgate.evaluator.rule.tool_use import ToolArgumentsEvaluator

    detail = skill_detail(skills=("first-skill", "second-skill"))
    args = detail["spans"][1]["output"]["tool_calls"][1]["args"]
    args["limit"] = 2000
    detail["spans"][3]["input"] = repr(args)
    trace = adapt_detail(detail)
    turn = CaseTurn(id="first", input={"txt": "hello"}, expectations=(
        ToolArgumentExpectation(tool="read_file", path="arguments.limit",
                                occurrence=occurrence, condition=Equals(expected=1000)),
    ))
    check = ToolArgumentsEvaluator().evaluate(
        evaluator_spec("tool_arguments"), turn, trace, resolve_unexpected,
    ).checks[0]
    assert check.outcome == expected
    assert check.score == (1.0 if expected == "pass" else 0.0)


def test_strict_evidence_assembly_preserves_arguments_without_inferring_skill():
    detail = skill_detail()
    detail["trace"].update(input={"txt": "hello"}, output="answer")
    client = Mock(fetch_events=Mock(return_value=detail_to_events(detail, [])))
    trace = assemble_evidence(
        execution_request(), "inbank-tests", {"first": fetch(client)},
        {"first": {"input": {"txt": "hello"}, "output": {"output": "answer"}, "state": {}}},
    )
    tool = next(s for s in trace.spans if s.operation_type == "tool")
    assert not any(s.operation_type == "routing" for s in trace.spans)
    assert tool.attributes["arguments"]["limit"] == 1000
    assert trace.final_output["output"] == "answer"


def test_existing_dimension_judge_consumes_adapted_trace_with_mock_only():
    from test_dimension_quality_judge import RecordingModel, config, spec

    from agentgate.evaluator.executor import execute_evaluators
    from agentgate.evaluator.judge.dimension_quality import DimensionQualityJudge

    configuration = {**config(), "input_selection": "full_trajectory", "max_input_chars": 100000}
    model = RecordingModel()
    result = execute_evaluators(
        execution_request().case, reported_intent(adapt_detail(skill_detail())), (spec(configuration),),
        {("answer_quality", "2"): DimensionQualityJudge({"test": model})},
    )[0]
    assert result.outcome == "pass"
    assert result.judge_record.resolved_model == "resolved-judge"
    assert len(model.requests) == 1
    assert '"selected_skill":"hello"' in model.requests[0].user_prompt
    assert '"arguments":{"file_path":' in model.requests[0].user_prompt
