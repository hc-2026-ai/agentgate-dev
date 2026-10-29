"""Evaluation Run lifecycle and immutable execution manifest."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import Field, ValidationInfo, field_validator, model_validator

from .base import (
    DomainModel,
    content_sha256,
    normalize_utc,
    require_non_blank,
    require_sha256,
    utcnow,
)
from .case import Case
from .dataset import DatasetVersion, DatasetVersionStatus
from .evaluator import EvaluatorSpec
from .gate import ReleaseGateSpec
from .metric import MetricPlan
from .target import TargetSnapshot


class RunStatus(StrEnum):
    """Lifecycle state of an Evaluation Run."""

    SCHEDULED = "scheduled"
    PENDING = "pending"
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunManifest(DomainModel):
    """Exact immutable inputs and effective configuration for one Evaluation Run."""

    dataset: DatasetVersion
    selected_case_ids: tuple[str, ...] | None = None
    target: TargetSnapshot
    evaluator_specs: tuple[EvaluatorSpec, ...] = Field(min_length=1)
    primary_evaluator_ids: tuple[str, ...] = Field(min_length=1)
    metric_plan: MetricPlan
    gate_spec: ReleaseGateSpec
    timeout_seconds: float = Field(default=300, gt=0)
    max_retries: int = Field(default=0, ge=0)
    max_parallel_cases: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utcnow)
    manifest_sha256: str = ""

    @field_validator("primary_evaluator_ids")
    @classmethod
    def validate_primary_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("primary_evaluator_ids must not contain blank values")
        if len(set(value)) != len(value):
            raise ValueError("primary_evaluator_ids must be unique")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return normalize_utc(value, "RunManifest created_at")

    @field_validator("manifest_sha256")
    @classmethod
    def validate_manifest_hash(cls, value: str) -> str:
        return require_sha256(value, "manifest_sha256") if value else value

    @model_validator(mode="after")
    def validate_manifest(self) -> "RunManifest":
        if self.dataset.status != DatasetVersionStatus.PUBLISHED:
            raise ValueError("RunManifest requires a published DatasetVersion")

        if self.selected_case_ids is not None:
            if not self.selected_case_ids:
                raise ValueError("selected_case_ids must contain at least one Case id")
            if any(not case_id.strip() for case_id in self.selected_case_ids):
                raise ValueError("selected_case_ids must not contain blank values")
            if len(set(self.selected_case_ids)) != len(self.selected_case_ids):
                raise ValueError("selected_case_ids must not contain duplicates")
            known_case_ids = {case.id for case in self.dataset.cases}
            unknown = set(self.selected_case_ids).difference(known_case_ids)
            if unknown:
                raise ValueError(
                    "selected_case_ids reference unknown Cases: "
                    + ", ".join(sorted(unknown))
                )

        evaluator_ids = tuple(item.id for item in self.evaluator_specs)
        if len(set(evaluator_ids)) != len(evaluator_ids):
            raise ValueError("Evaluator ids must be unique within a RunManifest")
        unknown_primary = set(self.primary_evaluator_ids).difference(evaluator_ids)
        if unknown_primary:
            raise ValueError(
                "primary_evaluator_ids reference unknown Evaluators: "
                + ", ".join(sorted(unknown_primary))
            )

        expected = content_sha256({
            "dataset": {
                "id": self.dataset.id,
                "dataset_id": self.dataset.dataset_id,
                "version": self.dataset.version,
                "content_sha256": self.dataset.content_sha256,
            },
            "selected_case_ids": self.selected_case_ids,
            "target": {
                "ref": self.target.ref.model_dump(mode="json"),
                "content_sha256": self.target.content_sha256,
            },
            "evaluators": [
                {
                    "id": item.id,
                    "version": item.version,
                    "content_sha256": item.content_sha256,
                }
                for item in self.evaluator_specs
            ],
            "primary_evaluator_ids": self.primary_evaluator_ids,
            "metric_plan": self.metric_plan.model_dump(mode="json"),
            "gate_spec": self.gate_spec.model_dump(mode="json"),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_parallel_cases": self.max_parallel_cases,
        })
        if self.manifest_sha256 and self.manifest_sha256 != expected:
            raise ValueError("RunManifest content hash mismatch")
        if not self.manifest_sha256:
            object.__setattr__(self, "manifest_sha256", expected)
        return self

    @property
    def execution_cases(self) -> tuple[Case, ...]:
        """Return the ordered Cases selected for this Run."""

        if self.selected_case_ids is None:
            return self.dataset.cases
        cases_by_id = {case.id: case for case in self.dataset.cases}
        return tuple(cases_by_id[case_id] for case_id in self.selected_case_ids)


class RunTiming(DomainModel):
    """Execution timestamps independent of the immutable manifest."""

    pending_started_at: datetime | None = None

    @field_validator("pending_started_at")
    @classmethod
    def normalize_pending_time(cls, value: datetime | None) -> datetime | None:
        return normalize_utc(value, "pending_started_at") if value is not None else None


class EvaluationRun(DomainModel):
    """Lifecycle record for one complete Dataset evaluation."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    manifest: RunManifest
    status: RunStatus = RunStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    scheduled_for: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    timing: RunTiming = Field(default_factory=RunTiming)
    user_team_id: str = ""  # 团队 ID（权限隔离，按团队过滤数据可见性）
    user_id: str = ""  # 用户 ID（创建者标识）
    user_name: str = ""  # 用户姓名（创建者显示名）
    api_key: str | None = None
    dispatch_attempts: int = Field(default=0, ge=0)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return require_non_blank(value, "EvaluationRun id")

    @field_validator("created_at", "scheduled_for", "started_at", "completed_at")
    @classmethod
    def normalize_timestamps(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return (
            normalize_utc(value, f"EvaluationRun {info.field_name}")
            if value is not None
            else None
        )

    @field_validator("error")
    @classmethod
    def validate_error(cls, value: str | None) -> str | None:
        if value is not None:
            return require_non_blank(value, "EvaluationRun error")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "EvaluationRun":
        if self.scheduled_for is not None and self.scheduled_for <= self.created_at:
            raise ValueError("scheduled_for must be later than created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at must not precede created_at")
        if self.completed_at is not None:
            earliest = self.started_at or self.created_at
            if self.completed_at < earliest:
                raise ValueError("completed_at must not precede Run activity")

        if self.status == RunStatus.SCHEDULED:
            if self.scheduled_for is None:
                raise ValueError("scheduled EvaluationRun requires scheduled_for")
            if self.started_at is not None or self.completed_at is not None or self.error:
                raise ValueError("scheduled EvaluationRun cannot contain execution outcome")
        elif self.status == RunStatus.PENDING:
            if self.started_at is not None or self.completed_at is not None or self.error:
                raise ValueError("pending EvaluationRun cannot contain execution outcome")
        elif self.status == RunStatus.WAITING:
            if self.started_at is not None or self.completed_at is not None or self.error:
                raise ValueError("waiting EvaluationRun cannot contain execution outcome")
        elif self.status == RunStatus.RUNNING:
            if self.started_at is None or self.completed_at is not None or self.error:
                raise ValueError("running EvaluationRun requires only started_at")
        elif self.status == RunStatus.COMPLETED:
            if self.started_at is None or self.completed_at is None or self.error:
                raise ValueError(
                    "completed EvaluationRun requires timestamps and no error"
                )
        elif self.status == RunStatus.FAILED:
            if self.completed_at is None or self.error is None:
                raise ValueError("failed EvaluationRun requires completed_at and error")
        elif self.status == RunStatus.CANCELLED:
            if self.completed_at is None or self.error is not None:
                raise ValueError("cancelled EvaluationRun requires completed_at and no error")
        return self


_ALLOWED_TRANSITIONS = {
    RunStatus.SCHEDULED: {RunStatus.PENDING, RunStatus.CANCELLED},
    RunStatus.PENDING: {RunStatus.WAITING, RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.WAITING: {RunStatus.PENDING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.RUNNING: {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED},
}


def transition_run(
    run: EvaluationRun,
    new_status: RunStatus,
    occurred_at: datetime | None = None,
    error: str | None = None,
) -> EvaluationRun:
    """Return a new EvaluationRun after one legal lifecycle transition."""

    if new_status not in _ALLOWED_TRANSITIONS.get(run.status, set()):
        raise ValueError(f"illegal Run transition: {run.status} -> {new_status}")
    timestamp = normalize_utc(occurred_at or utcnow(), "transition occurred_at")
    if timestamp < (run.started_at or run.created_at):
        raise ValueError("transition occurred_at must not precede Run activity")

    updates: dict[str, object] = {"status": new_status, "error": None}
    if new_status == RunStatus.WAITING:
        pass
    elif new_status == RunStatus.PENDING:
        updates["timing"] = RunTiming(pending_started_at=timestamp)
        if run.status == RunStatus.SCHEDULED and (
            run.scheduled_for is None or timestamp < run.scheduled_for
        ):
            raise ValueError("scheduled EvaluationRun cannot be released before scheduled_for")
    elif new_status == RunStatus.RUNNING:
        if error is not None:
            raise ValueError("running transition cannot contain an error")
        updates["started_at"] = timestamp
    else:
        updates["completed_at"] = timestamp
        if new_status == RunStatus.FAILED:
            if error is None or not error.strip():
                raise ValueError("failed transition requires an error")
            updates["error"] = error
        elif error is not None:
            raise ValueError("only failed transition may contain an error")

    return EvaluationRun.model_validate(
        {**run.model_dump(mode="json"), **updates}
    )
