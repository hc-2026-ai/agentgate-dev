"""Semantic answer-quality evaluation through a configured Judge model."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from time import monotonic
from types import MappingProxyType
from typing import Any

from agentgate.domain import (
    Case,
    EvaluatorKind,
    EvaluatorSpec,
    FailureStage,
    JudgeRecord,
    MethodRef,
    Outcome,
    Trace,
    content_sha256,
)
from agentgate.trace.redaction import redact_value

from ..models import CheckDraft, Evaluation, FailureCandidate, ResultResolver
from .contract import JudgeContractError, ParsedVerdict, parse_verdict
from .model_protocol import (
    JudgeModelClient,
    JudgeModelInvalidResponse,
    JudgeResponse,
    request_fingerprint,
)
from .prompt import JudgeInputSelection, build_judge_request

LOGGER = logging.getLogger(__name__)


_CONFIG_FIELDS = frozenset(
    {
        "input_selection",
        "instruction",
        "max_input_chars",
        "max_output_tokens",
        "min_confidence",
        "model",
        "pass_threshold",
        "rubric",
        "seed",
        "temperature",
        "timeout_seconds",
    }
)
_MODEL_FIELDS = frozenset({"credential_ref", "model_id", "provider_id"})
_INPUT_SELECTIONS = frozenset(
    {"final_output", "output_and_tools", "full_trajectory"}
)


def _reject_unknown_fields(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    subject: str,
) -> None:
    unknown = set(value).difference(allowed)
    if unknown:
        raise ValueError(f"{subject} has unknown fields: {', '.join(sorted(unknown))}")


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string")
    return value.strip()


def _bounded_number(
    value: Any,
    field_name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(f"{field_name} must be between {minimum:g} and {maximum:g}")
    return float(value)


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _positive_number(value: Any, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be positive")
    return float(value)


@dataclass(frozen=True, slots=True)
class _AnswerQualityConfig:
    provider_id: str
    model_id: str
    instruction: str
    rubric: Mapping[str, Any]
    input_selection: JudgeInputSelection
    pass_threshold: float
    min_confidence: float
    temperature: float
    seed: int | None
    max_output_tokens: int
    timeout_seconds: float
    max_input_chars: int

    @classmethod
    def from_spec(cls, spec: EvaluatorSpec) -> "_AnswerQualityConfig":
        config = spec.config
        _reject_unknown_fields(config, _CONFIG_FIELDS, "answer-quality config")

        model = config.get("model")
        if not isinstance(model, Mapping):
            raise ValueError("answer-quality config model must be an object")
        _reject_unknown_fields(model, _MODEL_FIELDS, "answer-quality model config")
        provider_id = _required_text(
            model.get("provider_id"),
            "answer-quality config model.provider_id",
        )
        model_id = _required_text(
            model.get("model_id"),
            "answer-quality config model.model_id",
        )
        credential_ref = model.get("credential_ref")
        if credential_ref is not None:
            _required_text(
                credential_ref,
                "answer-quality config model.credential_ref",
            )

        instruction = _required_text(
            config.get("instruction"),
            "answer-quality config instruction",
        )
        rubric = config.get("rubric")
        if not isinstance(rubric, Mapping) or not rubric:
            raise ValueError("answer-quality config rubric must be a non-empty object")

        input_selection = config.get("input_selection", "final_output")
        if input_selection not in _INPUT_SELECTIONS:
            raise ValueError("answer-quality config input_selection is invalid")
        pass_threshold = _bounded_number(
            config.get("pass_threshold", 0.8),
            "answer-quality config pass_threshold",
            minimum=0,
            maximum=1,
        )
        min_confidence = _bounded_number(
            config.get("min_confidence", 0.0),
            "answer-quality config min_confidence",
            minimum=0,
            maximum=1,
        )
        temperature = _bounded_number(
            config.get("temperature", 0.0),
            "answer-quality config temperature",
            minimum=0,
            maximum=2,
        )
        seed = config.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValueError("answer-quality config seed must be an integer or null")

        return cls(
            provider_id=provider_id,
            model_id=model_id,
            instruction=instruction,
            rubric=rubric,
            input_selection=input_selection,
            pass_threshold=pass_threshold,
            min_confidence=min_confidence,
            temperature=temperature,
            seed=seed,
            max_output_tokens=_positive_int(
                config.get("max_output_tokens", 6000),
                "answer-quality config max_output_tokens",
            ),
            timeout_seconds=_positive_number(
                config.get("timeout_seconds", 60),
                "answer-quality config timeout_seconds",
            ),
            max_input_chars=_positive_int(
                config.get("max_input_chars", 12_000),
                "answer-quality config max_input_chars",
            ),
        )


@dataclass(frozen=True, slots=True)
class AnswerQualityJudge:
    """Judge semantic answer quality once for a complete Case execution."""

    model_clients: Mapping[str, JudgeModelClient]

    kind = EvaluatorKind.LLM_JUDGE
    implementation_id = "answer_quality"
    implementation_version = "1"

    def __post_init__(self) -> None:
        clients = dict(self.model_clients)
        for provider_id, client in clients.items():
            _required_text(provider_id, "Judge model client provider key")
            if not isinstance(client, JudgeModelClient):
                raise TypeError(
                    f"Judge model client for {provider_id!r} does not implement "
                    "JudgeModelClient"
                )
            if client.provider_id != provider_id:
                raise ValueError(
                    f"Judge model client key {provider_id!r} does not match "
                    f"provider_id {client.provider_id!r}"
                )
        object.__setattr__(self, "model_clients", MappingProxyType(clients))

    def validate_spec(self, spec: EvaluatorSpec) -> None:
        """Validate a spec without invoking the configured Judge model."""
        self._validate_and_get_config(spec)

    def _validate_and_get_config(
        self,
        spec: EvaluatorSpec,
    ) -> _AnswerQualityConfig:
        if spec.kind != self.kind:
            raise ValueError(
                f"answer_quality requires kind {self.kind.value!r}, "
                f"got {spec.kind.value!r}"
            )
        if spec.implementation_id != self.implementation_id:
            raise ValueError(
                "answer_quality requires implementation_id "
                f"{self.implementation_id!r}, got {spec.implementation_id!r}"
            )
        if spec.implementation_version != self.implementation_version:
            raise ValueError(
                "answer_quality requires implementation_version "
                f"{self.implementation_version!r}, "
                f"got {spec.implementation_version!r}"
            )

        config = _AnswerQualityConfig.from_spec(spec)
        if config.provider_id not in self.model_clients:
            raise ValueError(
                f"no Judge model client configured for provider {config.provider_id!r}"
            )
        return config

    def evaluate_case(
        self,
        spec: EvaluatorSpec,
        case: Case,
        trace: Trace,
        resolve: ResultResolver,
    ) -> Evaluation:
        del resolve
        config = self._validate_and_get_config(spec)
        method = MethodRef(
            implementation_id=self.implementation_id,
            implementation_version=self.implementation_version,
        )
        if not trace.final_output:
            return Evaluation(
                checks=(
                    CheckDraft(
                        name=spec.name,
                        outcome=Outcome.NOT_APPLICABLE,
                        score=None,
                        reason="The execution has no final output to judge",
                        methods=(method,),
                    ),
                )
            )

        client = self.model_clients[config.provider_id]
        request = build_judge_request(
            model_id=config.model_id,
            instruction=config.instruction,
            rubric=config.rubric,
            case=case,
            trace=trace,
            input_selection=config.input_selection,
            pass_threshold=config.pass_threshold,
            temperature=config.temperature,
            seed=config.seed,
            max_output_tokens=config.max_output_tokens,
            timeout_seconds=config.timeout_seconds,
            max_input_chars=config.max_input_chars,
            redact=redact_value,
        )
        deadline = monotonic() + config.timeout_seconds
        attempts: list[JudgeRecord] = []
        # One correction at most, within the original deadline. Never normalize
        # invalid scores or turn a protocol failure into a passing verdict.
        for attempt in range(2):
            try:
                response = client.complete(request)
                LOGGER.info(
                    "Judge model response: provider_id=%s, model=%s, attempt=%d, "
                    "chars=%d, finish_reason=%s, truncated=%s, text=%.6000s",
                    client.provider_id,
                    response.resolved_model_id,
                    attempt,
                    len(response.text),
                    response.finish_reason,
                    response.truncated,
                    response.text,
                )
            except Exception as exc:
                if attempts:
                    exc.judge_record = attempts[-1]
                raise
            record = _judge_record(config, request_fingerprint(request), response).model_copy(
                update={"previous_attempts": tuple(attempts)}
            )
            try:
                if response.truncated:
                    raise JudgeModelInvalidResponse("Judge model response was truncated")
                verdict = parse_verdict(response.text, config.pass_threshold)
                break
            except (JudgeContractError, JudgeModelInvalidResponse) as exc:
                remaining = deadline - monotonic()
                if attempt or remaining <= 0 or response.truncated:
                    error = JudgeModelInvalidResponse(str(exc))
                    error.judge_record = record
                    raise error from exc
                attempts.append(record)
                request = replace(request, timeout_seconds=remaining,
                    system_prompt=(request.system_prompt or "") + "\n"
                    "Your preceding response failed schema validation. Re-evaluate the same "
                    "evidence and return ONLY a JSON object with exactly these fields: "
                    '{"verdict":"pass|fail|review","score":0.0,"confidence":0.0,'
                    '"reason":"short explanation","violations":[]}. '
                    "score and confidence MUST be fractions from 0.0 to 1.0 inclusive, "
                    "NEVER percentages or ten-point scores. The example is a schema, "
                    "confidence means nonnegative certainty, not sentiment: a confident "
                    "failure has positive confidence (e.g. 0.9), NEVER -0.9. "
                    "not a verdict to copy. Keep the original rubric and pass threshold. "
                    "Do not invent evidence or change a failure into a pass.")
        outcome, reason = _outcome_and_reason(verdict, config.min_confidence)

        return Evaluation(
            checks=(
                CheckDraft(
                    name=spec.name,
                    outcome=outcome,
                    score=verdict.score,
                    reason=reason,
                    expected={"rubric_sha256": content_sha256(config.rubric)},
                    actual={
                        "confidence": verdict.confidence,
                        "verdict": verdict.verdict,
                        "violations": tuple(
                            {
                                "criterion": violation.criterion,
                                "detail": violation.detail,
                            }
                            for violation in verdict.violations
                        ),
                    },
                    methods=(method,),
                    failure=(
                        FailureCandidate(
                            stage=FailureStage.FINAL_OUTPUT,
                            at_trace_completion=True,
                        )
                        if outcome == Outcome.FAIL
                        else None
                    ),
                ),
            ),
            judge_record=record,
        )


def _outcome_and_reason(
    verdict: ParsedVerdict,
    min_confidence: float,
) -> tuple[Outcome, str]:
    if verdict.confidence < min_confidence:
        return (
            Outcome.REVIEW,
            f"Judge confidence {verdict.confidence:g} is below "
            f"min_confidence {min_confidence:g}",
        )
    return (
        {
            "pass": Outcome.PASS,
            "fail": Outcome.FAIL,
            "review": Outcome.REVIEW,
        }[verdict.verdict],
        verdict.reason,
    )


def _judge_record(
    config: _AnswerQualityConfig,
    request_sha256: str,
    response: JudgeResponse,
) -> JudgeRecord:
    return JudgeRecord(
        provider_id=config.provider_id,
        requested_model=config.model_id,
        resolved_model=response.resolved_model_id,
        request_sha256=request_sha256,
        raw_response=response.text,
        request_id=response.request_id,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        latency_ms=response.latency_ms,
    )


__all__ = ["AnswerQualityJudge"]
