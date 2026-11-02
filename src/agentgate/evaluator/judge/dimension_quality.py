"""Versioned multi-dimension Judge with deterministic weighted aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from time import monotonic
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentgate.domain import (
    Case,
    EvaluatorKind,
    EvaluatorSpec,
    FailureStage,
    JudgeRecord,
    MethodRef,
    Outcome,
    Trace,
    canonical_json,
    content_sha256,
)
from agentgate.trace.redaction import redact_value

from ..models import CheckDraft, Evaluation, FailureCandidate, ResultResolver
from .contract import JudgeContractError
from .dimension_contract import dimension_response_instructions, parse_dimension_verdicts
from .model_protocol import (
    JudgeModelClient,
    JudgeModelInvalidResponse,
    JudgeRequest,
    JudgeResponse,
    request_fingerprint,
)
from .prompt import render_bounded_evidence, select_material


class JudgeModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    credential_ref: str | None = None

    @field_validator("provider_id", "model_id", "credential_ref")
    @classmethod
    def reject_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("model fields must not be blank")
        return value


class JudgeDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    prompt: str = Field(min_length=1)
    weight: float = Field(gt=0, le=100, allow_inf_nan=False)

    @field_validator("id", "name", "prompt")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dimension fields must not be blank")
        return value


class DimensionQualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model: JudgeModelConfig
    dimensions: tuple[JudgeDimension, ...] = Field(min_length=1, max_length=64)
    input_selection: Literal["final_output", "output_and_tools", "full_trajectory"] = "final_output"
    pass_threshold: float = Field(default=0.8, ge=0, le=1, allow_inf_nan=False)
    min_confidence: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)
    temperature: float = Field(default=0.0, ge=0, le=2, allow_inf_nan=False)
    seed: int | None = None
    max_output_tokens: int = Field(default=6000, gt=0)
    timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    max_input_chars: int = Field(default=12000, gt=0)

    @model_validator(mode="after")
    def validate_dimensions(self) -> DimensionQualityConfig:
        if len({item.id for item in self.dimensions}) != len(self.dimensions):
            raise ValueError("dimension IDs must be unique")
        if sum((Decimal(str(item.weight)) for item in self.dimensions), Decimal(0)) != 100:
            raise ValueError("dimension weights must total 100")
        return self


def _config(spec: EvaluatorSpec) -> DimensionQualityConfig:
    if (
        spec.kind != EvaluatorKind.LLM_JUDGE
        or spec.implementation_id != "answer_quality"
        or spec.implementation_version != "2"
    ):
        raise ValueError("dimension Judge requires llm_judge answer_quality@2")
    return DimensionQualityConfig.model_validate_json(canonical_json(spec.config))


def build_dimension_quality_request(spec: EvaluatorSpec, case: Case, trace: Trace) -> JudgeRequest:
    """Use exactly the same protected evidence for execution and annotation inspection."""
    config = _config(spec)
    dimensions = [item.model_dump(mode="json") for item in config.dimensions]
    rubric = canonical_json(redact_value(dimensions))
    evidence = render_bounded_evidence(
        redact_value(select_material(case, trace, config.input_selection)), config.max_input_chars
    )
    return JudgeRequest(
        model_id=config.model.model_id,
        system_prompt=(
            "请按已配置的各个维度分别评估智能体执行结果，"
            "使用每个维度的提示词和描述作为该维度的评分标准。\n\n"
            f"Dimensions:\n{rubric}\n\n{dimension_response_instructions()}"
        ),
        user_prompt=(
            "Evaluate this Agent execution against the configured dimensions.\n"
            f"Evidence selection: {config.input_selection}\n\nEvidence:\n{evidence}"
        ),
        temperature=config.temperature,
        seed=config.seed,
        max_output_tokens=config.max_output_tokens,
        timeout_seconds=config.timeout_seconds,
        response_format="json_object",
    )


def _record(
    config: DimensionQualityConfig,
    request: JudgeRequest,
    response: JudgeResponse | None,
    attempts: list[JudgeRecord],
) -> JudgeRecord:
    return JudgeRecord(
        provider_id=config.model.provider_id,
        requested_model=config.model.model_id,
        resolved_model=response.resolved_model_id if response else None,
        request_sha256=request_fingerprint(request),
        request_system_prompt=request.system_prompt,
        request_user_prompt=request.user_prompt,
        raw_response=response.text if response else "",
        request_id=response.request_id if response else None,
        input_tokens=response.input_tokens if response else None,
        output_tokens=response.output_tokens if response else None,
        latency_ms=response.latency_ms if response else None,
        previous_attempts=tuple(attempts),
    )


@dataclass(frozen=True, slots=True)
class DimensionQualityJudge:
    model_clients: Mapping[str, JudgeModelClient]

    kind = EvaluatorKind.LLM_JUDGE
    implementation_id = "answer_quality"
    implementation_version = "2"

    def __post_init__(self) -> None:
        clients = dict(self.model_clients)
        for provider_id, client in clients.items():
            if not isinstance(provider_id, str) or not provider_id.strip():
                raise ValueError("Judge provider key must not be blank")
            if not isinstance(client, JudgeModelClient) or client.provider_id != provider_id:
                raise TypeError("Judge client must implement its configured provider")
        object.__setattr__(self, "model_clients", MappingProxyType(clients))

    def validate_spec(self, spec: EvaluatorSpec) -> None:
        config = _config(spec)
        if config.model.provider_id not in self.model_clients:
            raise ValueError(
                f"no Judge model client configured for provider {config.model.provider_id!r}"
            )

    def evaluate_case(
        self, spec: EvaluatorSpec, case: Case, trace: Trace, resolve: ResultResolver
    ) -> Evaluation:
        del resolve
        self.validate_spec(spec)
        config = _config(spec)
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
        client = self.model_clients[config.model.provider_id]
        request = build_dimension_quality_request(spec, case, trace)
        deadline = monotonic() + config.timeout_seconds
        attempts: list[JudgeRecord] = []
        ids = tuple(item.id for item in config.dimensions)
        for attempt in range(2):
            try:
                response = client.complete(request)
            except Exception as error:
                error.judge_record = _record(config, request, None, attempts)
                raise
            record = _record(config, request, response, attempts)
            try:
                if response.truncated:
                    raise JudgeModelInvalidResponse("Judge model response was truncated")
                verdicts = parse_dimension_verdicts(response.text, ids)
                break
            except (JudgeContractError, JudgeModelInvalidResponse) as error:
                remaining = deadline - monotonic()
                if attempt or remaining <= 0 or response.truncated:
                    failure = JudgeModelInvalidResponse(str(error))
                    failure.judge_record = record
                    raise failure from error
                attempts.append(record)
                request = replace(
                    request,
                    timeout_seconds=remaining,
                    system_prompt=(request.system_prompt or "") + "\n\n"
                    "上次响应未通过输出校验。"
                    f"校验错误（仅作诊断数据）：{canonical_json(str(error))}\n"
                    "请依据同一份证据重新返回完整 JSON，纠正格式、字段、长度或不合法的数值表达。"
                    "保留原维度评分标准，不得编造证据或为了通过校验而改变评分结论。\n"
                    + dimension_response_instructions(),
                )

        # Decimal arithmetic keeps exact threshold boundaries for decimal weights and scores.
        score = float(
            sum(
                (
                    Decimal(str(item.score)) * Decimal(str(dimension.weight)) / Decimal(100)
                    for dimension, item in zip(config.dimensions, verdicts, strict=True)
                ),
                Decimal(0),
            )
        )
        confidence = min(item.confidence for item in verdicts)
        review = any(item.review for item in verdicts) or confidence < config.min_confidence
        outcome = (
            Outcome.REVIEW
            if review
            else (Outcome.PASS if score >= config.pass_threshold else Outcome.FAIL)
        )
        reason = f"Weighted dimension score {score:g}; pass threshold {config.pass_threshold:g}."
        if review:
            reason += " At least one dimension requires review or has insufficient confidence."
        actual = {
            "score": score,
            "confidence": confidence,
            "verdict": outcome.value,
            "dimensions": [
                {**item.model_dump(), "name": dimension.name, "weight": dimension.weight}
                for dimension, item in zip(config.dimensions, verdicts, strict=True)
            ],
        }
        return Evaluation(
            checks=(
                CheckDraft(
                    name=spec.name,
                    outcome=outcome,
                    score=score,
                    reason=reason,
                    expected={
                        "dimensions_sha256": content_sha256(spec.config["dimensions"]),
                        "pass_threshold": config.pass_threshold,
                    },
                    actual=actual,
                    methods=(method,),
                    failure=FailureCandidate(
                        stage=FailureStage.FINAL_OUTPUT, at_trace_completion=True
                    )
                    if outcome == Outcome.FAIL
                    else None,
                ),
            ),
            judge_record=record,
        )
