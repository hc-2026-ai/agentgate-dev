import logging
from unittest.mock import Mock

import pytest

from agentgate.domain import (
    Case,
    CaseTurn,
    CombinationPolicy,
    EvaluatorKind,
    EvaluatorRef,
    EvaluatorSpec,
    FailureStage,
    JudgeRecord,
    Outcome,
    OutputExpectation,
    SkillRouteExpectation,
    Trace,
    TraceSpan,
)
from agentgate.evaluator.executor import execute_evaluators
from agentgate.evaluator.models import CheckDraft, Evaluation, FailureCandidate
from agentgate.evaluator.rule.output import FinalOutputEvaluator
from agentgate.evaluator.rule.routing import SkillRoutingEvaluator


def evaluator_spec(
    evaluator_id: str,
    implementation_id: str,
    *,
    kind: EvaluatorKind = EvaluatorKind.RULE,
    implementation_version: str = "1",
    children: tuple[EvaluatorRef, ...] = (),
) -> EvaluatorSpec:
    config = (
        {"model": {"provider_id": "provider", "model_id": "model"}}
        if kind == EvaluatorKind.LLM_JUDGE
        else {}
    )
    return EvaluatorSpec(
        id=evaluator_id,
        name=evaluator_id,
        kind=kind,
        dimension="quality",
        metric=evaluator_id,
        implementation_id=implementation_id,
        implementation_version=implementation_version,
        config=config,
        children=children,
        combination=CombinationPolicy.ALL if kind == EvaluatorKind.HYBRID else None,
    )


def case(case_id: str = "case") -> Case:
    return Case(
        id=case_id,
        name=case_id,
        turns=(CaseTurn(id="turn", input={"message": "hello"}),),
    )


def trace(case_id: str = "case") -> Trace:
    return Trace(
        trace_id="0" * 32,
        run_id="run",
        case_id=case_id,
        spans=(),
    )


def passing_evaluation(name: str = "check") -> Evaluation:
    return Evaluation(
        checks=(
            CheckDraft(
                name=name,
                outcome=Outcome.PASS,
                score=1.0,
                reason="passed",
            ),
        )
    )


def output_only_trace(*, reported_intent: str | None = None) -> Trace:
    spans = [TraceSpan(
        trace_id="0" * 32, span_id="1" * 16, name="inbank.turn",
        operation_type="turn", sequence=0, status="ok",
        attributes={"agentgate.turn.id": "turn", "inbank.evidence_mode": "output_only"},
    )]
    if reported_intent is not None:
        spans.append(TraceSpan(
            trace_id="0" * 32, span_id="2" * 16, parent_span_id="1" * 16,
            name="inbank.yunxia.skill_selection", operation_type="routing", sequence=1,
            attributes={
                "agentgate.turn.id": "turn", "selected_skill": reported_intent,
                "inbank.skill_selection": {
                    "derived": True, "basis": "message_intent_code",
                    "source_span_id": "1" * 16, "source_path": "message.intent_code",
                },
            },
        ))
    return Trace(
        trace_id="0" * 32, run_id="run", case_id="case",
        spans=tuple(spans),
        final_output={"output": "hello"},
        turn_outcomes={"turn": {"output": {"output": "hello"}, "state": {}}},
    )


@pytest.mark.parametrize("implementation_id", [
    "required_tool", "forbidden_tool", "tool_arguments",
    "execution_path", "policy_compliance", "final_state",
])
def test_output_only_skips_trace_rules_instead_of_claiming_pass_or_fail(implementation_id, caplog):
    implementation = Mock(
        kind=EvaluatorKind.RULE, implementation_id=implementation_id, implementation_version="1",
    )
    implementation.applies_to.return_value = True
    result, = execute_evaluators(
        case(), output_only_trace(), (evaluator_spec("check", implementation_id),),
        {(implementation_id, "1"): implementation},
    )
    implementation.evaluate.assert_not_called()
    assert result.outcome == Outcome.NOT_APPLICABLE and result.score is None
    assert result.checks[0].actual_missing is True
    assert "Trace 证据不足" in result.checks[0].reason
    assert "inbank_evaluation_skipped" in caplog.text


@pytest.mark.parametrize("expected, outcome", [("balance", Outcome.PASS), ("transfer", Outcome.FAIL)])
@pytest.mark.parametrize("evidence_mode", ["trace_server", "output_only"])
def test_reported_yunxia_intent_is_evaluated_in_both_evidence_modes(expected, outcome, evidence_mode):
    sample = Case(id="case", name="case", turns=(CaseTurn(
        id="turn", input={"txt": "hello"}, expectations=(SkillRouteExpectation(
            condition={"kind": "equals", "expected": expected},
        ),),
    ),))
    evidence = output_only_trace(reported_intent="balance")
    root = evidence.spans[0].model_copy(update={"attributes": {
        **evidence.spans[0].attributes, "inbank.evidence_mode": evidence_mode,
    }})
    evidence = evidence.model_copy(update={"spans": (root, *evidence.spans[1:])})
    result, = execute_evaluators(
        sample, evidence,
        (evaluator_spec("routing", "skill_routing"),),
        {("skill_routing", "1"): SkillRoutingEvaluator()},
    )
    assert result.outcome == outcome
    assert result.checks[0].turn_id == "turn"
    assert result.checks[0].actual == "balance"
    assert result.checks[0].span_ids == ("2" * 16,)


@pytest.mark.parametrize("evidence_mode", ["trace_server", "output_only"])
def test_missing_yunxia_intent_is_missing_routing_in_both_evidence_modes(evidence_mode, caplog):
    sample = Case(id="case", name="case", turns=(CaseTurn(
        id="turn", input={"txt": "hello"}, expectations=(SkillRouteExpectation(
            condition={"kind": "equals", "expected": "balance"},
        ),),
    ),))
    evidence = output_only_trace()
    root = evidence.spans[0].model_copy(update={"attributes": {
        **evidence.spans[0].attributes, "inbank.evidence_mode": evidence_mode,
    }})
    evidence = evidence.model_copy(update={"spans": (root,)})
    result, = execute_evaluators(
        sample, evidence, (evaluator_spec("routing", "skill_routing"),),
        {("skill_routing", "1"): SkillRoutingEvaluator()},
    )
    assert result.outcome == Outcome.FAIL
    assert result.score == 0
    assert result.checks[0].actual_missing
    assert result.checks[0].reason == "未产生路由决策"
    assert "inbank_evaluation_skipped" not in caplog.text


@pytest.mark.parametrize("implementation_id", [
    "required_tool", "forbidden_tool", "tool_arguments", "execution_path",
    "policy_compliance", "final_state",
])
def test_reported_intent_does_not_supply_missing_execution_evidence(implementation_id):
    implementation = Mock(
        kind=EvaluatorKind.RULE, implementation_id=implementation_id, implementation_version="1",
    )
    implementation.applies_to.return_value = True
    result, = execute_evaluators(
        case(), output_only_trace(reported_intent="balance"),
        (evaluator_spec("check", implementation_id),), {(implementation_id, "1"): implementation},
    )
    implementation.evaluate.assert_not_called()
    assert result.outcome == Outcome.NOT_APPLICABLE and result.score is None
    assert result.checks[0].actual_missing


@pytest.mark.parametrize("expected, outcome", [("hello", Outcome.PASS), ("wrong", Outcome.FAIL)])
@pytest.mark.parametrize("reported_intent", [None, "balance"])
def test_output_only_still_evaluates_actual_final_answer(expected, outcome, reported_intent):
    sample = Case(id="case", name="case", turns=(CaseTurn(
        id="turn", input={"txt": "hi"}, expectations=(OutputExpectation(
            id="answer", path="output", condition={"kind": "equals", "expected": expected},
        ),),
    ),))
    result, = execute_evaluators(
        sample, output_only_trace(reported_intent=reported_intent), (evaluator_spec("output", "final_output"),),
        {("final_output", "1"): FinalOutputEvaluator()},
    )
    assert result.outcome == outcome
    assert result.checks[0].actual == "hello"


@pytest.mark.parametrize("selection", [None, "final_output", "output_and_tools", "full_trajectory"])
@pytest.mark.parametrize("reported_intent", [None, "balance"])
def test_output_only_judge_requires_requested_evidence(selection, reported_intent):
    spec = evaluator_spec("judge", "answer_quality", kind=EvaluatorKind.LLM_JUDGE)
    if selection:
        spec = EvaluatorSpec(**{
            **spec.model_dump(exclude={"content_sha256"}),
            "config": {**spec.config.to_dict(), "input_selection": selection},
        })
    implementation = Mock(
        kind=EvaluatorKind.LLM_JUDGE, implementation_id="answer_quality", implementation_version="1",
    )
    implementation.evaluate_case.return_value = passing_evaluation().model_copy(update={
        "judge_record": JudgeRecord(
            provider_id="provider", requested_model="model", request_sha256="b" * 64,
            raw_response="{}",
        ),
    })
    result, = execute_evaluators(
        case(), output_only_trace(reported_intent=reported_intent), (spec,),
        {("answer_quality", "1"): implementation},
    )
    if selection in {None, "final_output"}:
        implementation.evaluate_case.assert_called_once()
        assert result.outcome == Outcome.PASS
    else:
        implementation.evaluate_case.assert_not_called()
        assert result.outcome == Outcome.NOT_APPLICABLE and result.score is None


def test_mixed_evidence_skips_only_missing_turn():
    first = output_only_trace()
    sample = Case(id="case", name="case", turns=(
        CaseTurn(id="turn", input={"txt": "first"}), CaseTurn(id="second", input={"txt": "next"}),
    ))
    evidence = Trace(
        **{**first.model_dump(), "spans": [*first.spans, TraceSpan(
            trace_id=first.trace_id, span_id="2" * 16, name="trace_sdk.turn", operation_type="turn",
            sequence=1, status="ok",
            attributes={"agentgate.turn.id": "second", "inbank.evidence_mode": "trace_server"},
        )], "turn_outcomes": {**first.turn_outcomes.to_dict(), "second": {"output": {"output": "next"}, "state": {}}}},
    )
    implementation = Mock(
        kind=EvaluatorKind.RULE, implementation_id="required_tool", implementation_version="1",
    )
    implementation.applies_to.return_value = True
    implementation.evaluate.return_value = passing_evaluation()
    result, = execute_evaluators(
        sample, evidence, (evaluator_spec("tool", "required_tool"),), {("required_tool", "1"): implementation},
    )
    implementation.evaluate.assert_called_once()
    assert implementation.evaluate.call_args.args[1].id == "second"
    assert [c.outcome for c in result.checks] == [Outcome.NOT_APPLICABLE, Outcome.PASS]
    assert result.score == 1.0


class PassingEvaluator:
    kind = EvaluatorKind.RULE
    implementation_id = "passing"
    implementation_version = "1"

    def applies_to(self, spec, turn):
        return True

    def evaluate(self, spec, turn, trace, resolve):
        return passing_evaluation()


class SkippedEvaluator:
    kind = EvaluatorKind.RULE
    implementation_id = "skipped"
    implementation_version = "1"

    def applies_to(self, spec, turn):
        return False

    def evaluate(self, spec, turn, trace, resolve):
        raise AssertionError("a skipped evaluator must not execute")


class CrashingEvaluator:
    kind = EvaluatorKind.RULE
    implementation_id = "crashing"
    implementation_version = "1"

    def applies_to(self, spec, turn):
        return True

    def evaluate(self, spec, turn, trace, resolve):
        raise RuntimeError("authorization=secret-value")


class TimeoutEvaluator:
    kind = EvaluatorKind.RULE
    implementation_id = "timeout"
    implementation_version = "1"

    def applies_to(self, spec, turn):
        return True

    def evaluate(self, spec, turn, trace, resolve):
        raise TimeoutError("token=secret-value")


class TraceOrderedFailureEvaluator:
    kind = EvaluatorKind.RULE
    implementation_id = "trace-ordered"
    implementation_version = "1"

    def applies_to(self, spec, turn):
        return True

    def evaluate(self, spec, turn, trace, resolve):
        return Evaluation(
            checks=(
                CheckDraft(
                    name="routing",
                    outcome=Outcome.FAIL,
                    score=0.0,
                    reason="routing",
                    failure=FailureCandidate(
                        stage=FailureStage.ROUTING,
                        span_id="1" * 16,
                    ),
                ),
                CheckDraft(
                    name="state",
                    outcome=Outcome.FAIL,
                    score=0.0,
                    reason="state",
                    failure=FailureCandidate(
                        stage=FailureStage.FINAL_STATE,
                        span_id="2" * 16,
                    ),
                ),
            )
        )


def test_executes_structural_implementations_in_specification_order() -> None:
    specs = (
        evaluator_spec("passing-result", "passing"),
        evaluator_spec("skipped-result", "skipped"),
    )

    results = execute_evaluators(
        case(),
        trace(),
        specs,
        {
            ("passing", "1"): PassingEvaluator(),
            ("skipped", "1"): SkippedEvaluator(),
        },
    )

    assert isinstance(results, tuple)
    assert [result.evaluator_id for result in results] == [
        "passing-result",
        "skipped-result",
    ]
    assert results[0].outcome == Outcome.PASS
    assert results[0].checks[0].turn_id == "turn"
    assert results[1].outcome == Outcome.NOT_APPLICABLE


def test_rejects_case_trace_and_implementation_identity_drift() -> None:
    spec = evaluator_spec("passing-result", "passing")

    with pytest.raises(ValueError, match="does not match Case id"):
        execute_evaluators(case(), trace("different"), (spec,), {})

    with pytest.raises(Exception, match="unknown evaluator implementation"):
        execute_evaluators(case(), trace(), (spec,), {})

    implementation = PassingEvaluator()
    implementation.implementation_version = "2"
    with pytest.raises(Exception, match="requires version 1, not 2"):
        execute_evaluators(
            case(),
            trace(),
            (spec,),
            {("passing", "1"): implementation},
        )


def test_sanitizes_errors_and_continues_independent_evaluators(caplog) -> None:
    specs = (
        evaluator_spec("crashing-result", "crashing"),
        evaluator_spec("passing-result", "passing"),
    )

    with caplog.at_level(logging.ERROR):
        results = execute_evaluators(
            case(),
            trace(),
            specs,
            {
                ("crashing", "1"): CrashingEvaluator(),
                ("passing", "1"): PassingEvaluator(),
            },
        )

    assert results[0].outcome == Outcome.ERROR
    assert results[0].error_detail.category == "crash"
    assert results[1].outcome == Outcome.PASS
    assert "secret-value" not in results[0].error_detail.message
    assert "secret-value" not in caplog.text


def test_classifies_timeout_as_retryable_and_sanitizes_it(caplog) -> None:
    spec = evaluator_spec("timeout-result", "timeout")

    with caplog.at_level(logging.ERROR):
        result = execute_evaluators(
            case(),
            trace(),
            (spec,),
            {("timeout", "1"): TimeoutEvaluator()},
        )[0]

    assert result.outcome == Outcome.ERROR
    assert result.score is None
    assert result.error_detail.category == "timeout"
    assert result.error_detail.exception_type == "TimeoutError"
    assert result.error_detail.retryable is True
    assert "secret-value" not in result.error_detail.message
    assert "secret-value" not in caplog.text


def test_primary_failure_follows_trace_sequence_not_check_order() -> None:
    spec = evaluator_spec("ordered-result", "trace-ordered")
    ordered_trace = Trace(
        trace_id="0" * 32,
        run_id="run",
        case_id="case",
        spans=(
            TraceSpan(
                span_id="1" * 16,
                trace_id="0" * 32,
                name="route",
                operation_type="routing",
                sequence=5,
            ),
            TraceSpan(
                span_id="2" * 16,
                trace_id="0" * 32,
                name="state",
                operation_type="state",
                sequence=1,
            ),
        ),
    )

    result = execute_evaluators(
        case(),
        ordered_trace,
        (spec,),
        {("trace-ordered", "1"): TraceOrderedFailureEvaluator()},
    )[0]

    assert [check.failure_sequence for check in result.checks] == [5, 1]
    assert result.primary_failure_stage == FailureStage.FINAL_STATE


class ChildRuleEvaluator:
    kind = EvaluatorKind.RULE
    implementation_id = "child-rule"
    implementation_version = "1"

    def __init__(self):
        self.calls = 0

    def applies_to(self, spec, turn):
        return True

    def evaluate(self, spec, turn, trace, resolve):
        self.calls += 1
        return passing_evaluation("rule")


class ChildJudgeEvaluator:
    kind = EvaluatorKind.LLM_JUDGE
    implementation_id = "child-judge"
    implementation_version = "1"

    def evaluate_case(self, spec, case, trace, resolve):
        return Evaluation(
            checks=passing_evaluation("judge").checks,
            judge_record=JudgeRecord(
                provider_id="provider",
                requested_model="model",
                request_sha256="a" * 64,
                raw_response="{}",
            ),
        )


class HybridEvaluator:
    kind = EvaluatorKind.HYBRID
    implementation_id = "hybrid"
    implementation_version = "1"

    def applies_to(self, spec, turn):
        return True

    def evaluate(self, spec, turn, trace, resolve):
        first = resolve("rule")
        second = resolve("rule")
        judge = resolve("judge")
        assert first is second
        assert first.outcome == judge.outcome == Outcome.PASS
        return passing_evaluation("hybrid")


def test_resolves_only_declared_dependencies_and_memoizes_results() -> None:
    rule = evaluator_spec("rule", "child-rule")
    judge = evaluator_spec("judge", "child-judge", kind=EvaluatorKind.LLM_JUDGE)
    hybrid = evaluator_spec(
        "combined",
        "hybrid",
        kind=EvaluatorKind.HYBRID,
        children=(
            EvaluatorRef(evaluator_id="rule", evaluator_version="1"),
            EvaluatorRef(evaluator_id="judge", evaluator_version="1"),
        ),
    )
    rule_implementation = ChildRuleEvaluator()

    results = execute_evaluators(
        case(),
        trace(),
        (hybrid, rule, judge),
        {
            ("hybrid", "1"): HybridEvaluator(),
            ("child-rule", "1"): rule_implementation,
            ("child-judge", "1"): ChildJudgeEvaluator(),
        },
    )

    assert [result.outcome for result in results] == [Outcome.PASS] * 3
    assert rule_implementation.calls == 1


class InvalidEvidenceEvaluator:
    kind = EvaluatorKind.RULE
    implementation_id = "invalid-evidence"
    implementation_version = "1"

    def applies_to(self, spec, turn):
        return True

    def evaluate(self, spec, turn, trace, resolve):
        return Evaluation(
            checks=(
                CheckDraft(
                    name="invalid",
                    turn_id="another-turn",
                    outcome=Outcome.FAIL,
                    score=0.0,
                    reason="failed",
                    failure=FailureCandidate(
                        stage=FailureStage.FINAL_OUTPUT,
                        at_trace_completion=True,
                    ),
                ),
            )
        )


def test_converts_invalid_evaluator_evidence_to_an_error_result() -> None:
    spec = evaluator_spec("invalid-result", "invalid-evidence")

    result = execute_evaluators(
        case(),
        trace(),
        (spec,),
        {("invalid-evidence", "1"): InvalidEvidenceEvaluator()},
    )[0]

    assert result.outcome == Outcome.ERROR
    assert result.error_detail.category == "invalid_output"
    assert result.error_detail.exception_type == "ValueError"


class RecordingCaseJudge:
    kind = EvaluatorKind.LLM_JUDGE
    implementation_id = "recording-judge"
    implementation_version = "1"

    def __init__(self):
        self.calls = []

    def evaluate_case(self, spec, case, trace, resolve):
        self.calls.append((case, trace))
        return Evaluation(
            checks=(
                CheckDraft(
                    name="case quality",
                    outcome=Outcome.PASS,
                    score=1.0,
                    reason="passed",
                    span_ids=tuple(span.span_id for span in trace.spans),
                ),
                CheckDraft(
                    name="second turn quality",
                    turn_id="turn-2",
                    outcome=Outcome.PASS,
                    score=1.0,
                    reason="passed",
                ),
            ),
            judge_record=JudgeRecord(
                provider_id="provider",
                requested_model="model",
                request_sha256="b" * 64,
                raw_response="{}",
            ),
        )


def test_executes_judge_once_with_complete_case_and_trace() -> None:
    complete_case = Case(
        id="case",
        name="case",
        turns=(
            CaseTurn(id="turn-1", input={"message": "first"}),
            CaseTurn(id="turn-2", input={"message": "second"}),
        ),
    )
    complete_trace = Trace(
        trace_id="0" * 32,
        run_id="run",
        case_id="case",
        spans=(
            TraceSpan(
                trace_id="0" * 32,
                span_id="3" * 16,
                name="first",
                operation_type="turn",
                sequence=1,
            ),
            TraceSpan(
                trace_id="0" * 32,
                span_id="4" * 16,
                name="second",
                operation_type="turn",
                sequence=2,
            ),
        ),
    )
    implementation = RecordingCaseJudge()
    spec = evaluator_spec(
        "judge-result",
        "recording-judge",
        kind=EvaluatorKind.LLM_JUDGE,
    )

    result = execute_evaluators(
        complete_case,
        complete_trace,
        (spec,),
        {("recording-judge", "1"): implementation},
    )[0]

    assert implementation.calls == [(complete_case, complete_trace)]
    assert result.outcome == Outcome.PASS
    assert result.checks[0].turn_id is None
    assert result.checks[0].span_ids == ("3" * 16, "4" * 16)
    assert result.checks[1].turn_id == "turn-2"


class InvalidCaseEvidenceJudge(ChildJudgeEvaluator):
    implementation_id = "invalid-case-evidence"

    def __init__(self, check):
        self.check = check

    def evaluate_case(self, spec, case, trace, resolve):
        return Evaluation(
            checks=(self.check,),
            judge_record=JudgeRecord(
                provider_id="provider",
                requested_model="model",
                request_sha256="c" * 64,
                raw_response="{}",
            ),
        )


@pytest.mark.parametrize(
    "check",
    [
        CheckDraft(
            name="unknown turn",
            turn_id="missing-turn",
            outcome=Outcome.PASS,
            score=1.0,
            reason="passed",
        ),
        CheckDraft(
            name="unknown span",
            outcome=Outcome.PASS,
            score=1.0,
            reason="passed",
            span_ids=("9" * 16,),
        ),
    ],
)
def test_converts_invalid_case_evidence_to_an_error_result(check) -> None:
    spec = evaluator_spec(
        "invalid-judge-result",
        "invalid-case-evidence",
        kind=EvaluatorKind.LLM_JUDGE,
    )

    result = execute_evaluators(
        case(),
        trace(),
        (spec,),
        {("invalid-case-evidence", "1"): InvalidCaseEvidenceJudge(check)},
    )[0]

    assert result.outcome == Outcome.ERROR
    assert result.error_detail.category == "invalid_output"
    assert result.error_detail.exception_type == "ValueError"


class WrongScopedJudge:
    kind = EvaluatorKind.LLM_JUDGE
    implementation_id = "wrong-scoped"
    implementation_version = "1"

    def applies_to(self, spec, turn):
        return True

    def evaluate(self, spec, turn, trace, resolve):
        return passing_evaluation()


def test_converts_wrong_execution_method_to_an_error_result() -> None:
    spec = evaluator_spec(
        "wrong-scoped-result",
        "wrong-scoped",
        kind=EvaluatorKind.LLM_JUDGE,
    )

    result = execute_evaluators(
        case(),
        trace(),
        (spec,),
        {("wrong-scoped", "1"): WrongScopedJudge()},
    )[0]

    assert result.outcome == Outcome.ERROR
    assert result.error_detail.category == "invalid_output"
    assert result.error_detail.exception_type == "TypeError"
