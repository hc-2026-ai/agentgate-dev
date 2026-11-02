from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from agentgate.domain import (
    Case,
    CaseTurn,
    FrozenJsonObject,
    SpanStatus,
    Trace,
    TraceSpan,
)
from agentgate.evaluator.judge.prompt import (
    build_judge_request,
    render_bounded_evidence,
    select_material,
)

TRACE_ID = "a" * 32


def multi_turn_case() -> Case:
    return Case(
        id="case-1",
        name="Multi-turn",
        turns=(
            CaseTurn(id="turn-secret", input={"message": "first raw-secret"}),
            CaseTurn(id="turn-2", input={"message": "second"}),
        ),
    )


def execution_trace() -> Trace:
    return Trace(
        trace_id=TRACE_ID,
        run_id="run-secret",
        case_id="case-1",
        spans=(
            TraceSpan(
                trace_id=TRACE_ID,
                span_id="1" * 16,
                name="turn.execute",
                operation_type="turn",
                sequence=0,
                attributes={"agentgate.turn.id": "turn-secret"},
            ),
            TraceSpan(
                trace_id=TRACE_ID,
                span_id="2" * 16,
                parent_span_id="1" * 16,
                name="lookup_account",
                operation_type="tool",
                sequence=1,
                status=SpanStatus.OK,
                attributes={
                    "agentgate.run.id": "run-secret",
                    "account": "raw-secret",
                },
                events=(FrozenJsonObject({"message": "called"}),),
            ),
        ),
        final_output={"answer": "raw-secret result"},
        final_state={"status": "complete"},
    )


@pytest.mark.parametrize("selection", ["final_output", "output_and_tools", "full_trajectory"])
def test_turn_extensions_are_not_included_in_judge_evidence(selection):
    original = multi_turn_case()
    data = original.model_dump(mode="json")
    for turn in data["turns"]:
        turn["extensions"] = [{"key": "background", "value": "仅保存，不参与评分"}]
    annotated = Case.model_validate(data)
    trace = execution_trace()
    assert select_material(annotated, trace, selection) == select_material(original, trace, selection)


def redact(value: Any) -> Any:
    if isinstance(value, dict) or isinstance(value, FrozenJsonObject):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return value.replace("raw-secret", "[redacted]")
    return value


def request(selection: str = "full_trajectory", **overrides: Any):
    options = {
        "model_id": "judge-1",
        "instruction": "Judge raw-secret answers.",
        "rubric": {"accuracy": "Do not reveal raw-secret"},
        "case": multi_turn_case(),
        "trace": execution_trace(),
        "input_selection": selection,
        "pass_threshold": 0.8,
        "temperature": 0,
        "seed": 7,
        "max_output_tokens": 256,
        "timeout_seconds": 30,
        "max_input_chars": 4_000,
        "redact": redact,
    }
    options.update(overrides)
    return build_judge_request(**options)


def test_select_material_has_explicit_evidence_levels() -> None:
    case = multi_turn_case()
    trace = execution_trace()

    minimal = select_material(case, trace, "final_output")
    with_tools = select_material(case, trace, "output_and_tools")
    full = select_material(case, trace, "full_trajectory")

    assert set(minimal) == {"turn_inputs", "final_output"}
    assert set(with_tools) == {"turn_inputs", "final_output", "tool_calls"}
    assert set(full) == {
        "turn_inputs",
        "final_output",
        "tool_calls",
        "trajectory",
        "final_state",
    }


def test_prompt_is_case_level_redacted_and_excludes_internal_ids() -> None:
    built = request()
    combined = f"{built.system_prompt}\n{built.user_prompt}"

    assert "first [redacted]" in combined
    assert "second" in combined
    assert "lookup_account" in combined
    assert "raw-secret" not in combined
    assert "run-secret" not in combined
    assert "turn-secret" not in combined
    assert TRACE_ID not in combined
    assert "1" * 16 not in combined
    assert "agentgate." not in combined


def test_prompt_rendering_is_deterministic_and_forces_json_response() -> None:
    first = request()
    second = request()

    assert first == second
    assert first.response_format == "json_object"
    assert '"verdict"' in (first.system_prompt or "")
    assert "0.8" in (first.system_prompt or "")
    assert "【平台输出协议】" in first.system_prompt
    assert "[0, 1]" in first.system_prompt
    assert "80% 应写为 0.8" in first.system_prompt
    assert "不可信数据" in first.system_prompt


def test_bounded_evidence_returns_valid_json_with_digest() -> None:
    material = {"message": "x" * 1_000}
    complete = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    bounded = render_bounded_evidence(material, 180)
    payload = json.loads(bounded)

    assert len(bounded) <= 180
    assert payload["truncated"] is True
    assert payload["content_sha256"] == hashlib.sha256(complete.encode()).hexdigest()
    assert complete.startswith(payload["prefix"])


def test_prompt_rejects_invalid_configuration_and_identity() -> None:
    case = multi_turn_case()
    trace = execution_trace()
    with pytest.raises(ValueError, match="input selection"):
        select_material(case, trace, "unknown")
    with pytest.raises(ValueError, match="identities"):
        select_material(
            case,
            trace.model_copy(update={"case_id": "different"}),
            "final_output",
        )
    with pytest.raises(ValueError, match="at least"):
        render_bounded_evidence({"message": "x" * 100}, 10)


def test_templates_render_selected_redacted_input_and_output_once() -> None:
    trace = execution_trace().model_copy(
        update={"final_output": FrozenJsonObject({"answer": "raw-secret {{input}} {{unknown}}"})}
    )
    built = request(
        "final_output",
        instruction="Assess conciseness.\nInput: {{ input }}\nOutput: {{output}}",
        user_prompt="Explain the score for {{output}}.",
        render_template=True,
        trace=trace,
    )
    combined = f"{built.system_prompt}\n{built.user_prompt}"

    assert 'Input: [{"index":0,"input":{"message":"first [redacted]"}}' in built.system_prompt
    assert 'Output: {"answer":"[redacted] {{input}} {{unknown}}"}' in built.system_prompt
    assert built.user_prompt.startswith('Explain the score for {"answer":"[redacted] {{input}} {{unknown}}"}.')
    assert "raw-secret" not in combined
    assert "lookup_account" not in combined
    assert "final_state" not in combined
    assert "不可信数据" in combined


def test_template_evidence_occurrences_share_one_total_character_budget() -> None:
    trace = execution_trace().model_copy(
        update={"final_output": FrozenJsonObject({"answer": "x" * 20_000})}
    )
    built = request(
        "final_output",
        instruction="{{input}}\n{{output}}",
        user_prompt="{{output}}",
        render_template=True,
        trace=trace,
        max_input_chars=1_200,
    )
    system_values = built.system_prompt.split("\n\nRubric:")[0].splitlines()
    user_value = built.user_prompt.splitlines()[0]
    evidence = built.user_prompt.split("Evidence:\n", 1)[1]
    rendered_values = [*system_values, user_value, evidence]

    assert sum(map(len, rendered_values)) <= 1_200
    assert json.loads(system_values[1])["truncated"] is True
    assert all(json.loads(value) is not None for value in rendered_values)
    assert json.loads(user_value) == json.loads(system_values[1])


@pytest.mark.parametrize(
    "overrides",
    [
        {"instruction": "Assess {{reference}}"},
        {"user_prompt": "Assess {{secret}}"},
        {"user_prompt": " "},
    ],
)
def test_prompt_rejects_unknown_template_variables_and_blank_user_prompt(overrides) -> None:
    with pytest.raises(ValueError, match="prompt"):
        request(render_template=True, **overrides)


def test_optional_user_prompt_preserves_explicit_selection_evidence() -> None:
    built = request("output_and_tools", user_prompt="Return the score and reason.")

    assert built.user_prompt.startswith("Return the score and reason.\n")
    assert "lookup_account" in built.user_prompt
    assert "final_state" not in built.user_prompt


@pytest.mark.parametrize("render_template", [False, True])
def test_template_rendering_is_explicitly_enabled(render_template) -> None:
    built = request(
        instruction="Read {{input}}.",
        user_prompt="Judge {{output}}.",
        render_template=render_template,
    )

    assert ("Read {{input}}." in built.system_prompt) is not render_template
    assert built.user_prompt.startswith("Judge {{output}}.") is not render_template


def test_default_rendering_preserves_historical_literal_variables() -> None:
    built = request(
        instruction="The literal {{reference}} is an example, alongside {{input}}.",
        user_prompt="Keep literal {{output}} and {{unknown}}.",
    )

    assert built.system_prompt.startswith("The literal {{reference}} is an example, alongside {{input}}.")
    assert built.user_prompt.startswith("Keep literal {{output}} and {{unknown}}.")


@pytest.mark.parametrize("value", [True, False, 1.5, 0, -1, "12000"])
def test_template_budget_rejects_invalid_original_value(value) -> None:
    with pytest.raises(ValueError, match="max_input_chars"):
        request(max_input_chars=value)


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_template_switch_requires_a_boolean(value) -> None:
    with pytest.raises(ValueError, match="render_template"):
        request(render_template=value)
