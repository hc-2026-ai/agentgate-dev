"""Controlled A/B creation through ordinary Evaluation Runs."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from agentgate.domain import (
    EvaluationRun,
    EvaluatorRef,
    MetricPlan,
    ReleaseGateSpec,
    RunStatus,
    TargetSnapshot,
    TargetType,
)
from agentgate.integrations.job_dispatchers import JobDispatcher

from .run_management import RunManagement


class ABRunPair(BaseModel):
    """Two controlled Runs representing baseline A and candidate B."""

    model_config = ConfigDict(frozen=True)

    baseline_run: EvaluationRun
    candidate_run: EvaluationRun

    @model_validator(mode="after")
    def validate_pair(self) -> "ABRunPair":
        if self.baseline_run.id == self.candidate_run.id:
            raise ValueError("A/B Runs must have distinct identities")
        _validate_variants(
            self.baseline_run.manifest.target,
            self.candidate_run.manifest.target,
        )
        if _control_signature(self.baseline_run) != _control_signature(
            self.candidate_run
        ):
            raise ValueError("A/B Runs must use identical controlled inputs")
        return self


def _validate_variants(
    baseline: TargetSnapshot,
    candidate: TargetSnapshot,
) -> None:
    baseline_ref = baseline.ref
    candidate_ref = candidate.ref
    if (
        baseline_ref.target_type is not TargetType.AGENT
        or candidate_ref.target_type is not TargetType.AGENT
    ):
        raise ValueError("A/B variants must be Agent Targets")
    baseline_identity = (
        baseline_ref.source_id,
        baseline_ref.external_target_id,
    )
    candidate_identity = (
        candidate_ref.source_id,
        candidate_ref.external_target_id,
    )
    if baseline_identity != candidate_identity:
        raise ValueError("A/B variants must reference the same logical Agent")
    if baseline_ref.external_version_id == candidate_ref.external_version_id:
        raise ValueError("A/B variants must use different Agent versions")
    if baseline.content_sha256 == candidate.content_sha256:
        raise ValueError("A/B variants must use different immutable Target snapshots")


def _control_signature(run: EvaluationRun) -> tuple[object, ...]:
    manifest = run.manifest
    dataset = manifest.dataset
    return (
        dataset.dataset_id,
        dataset.version,
        dataset.content_sha256,
        manifest.selected_case_ids,
        tuple(
            (spec.id, spec.version, spec.content_sha256)
            for spec in manifest.evaluator_specs
        ),
        manifest.primary_evaluator_ids,
        manifest.metric_plan.model_dump(mode="json"),
        manifest.gate_spec.model_dump(mode="json"),
        manifest.timeout_seconds,
        manifest.max_retries,
        manifest.max_parallel_cases,
    )


def _dispatch_and_reload(
    run_management: RunManagement,
    dispatcher: JobDispatcher,
    run: EvaluationRun,
) -> EvaluationRun:
    try:
        run_management.dispatch_run(run.id, dispatcher)
    except RuntimeError:
        current = run_management.repository.get_run(run.id)
        if current is None or current.status is not RunStatus.FAILED:
            raise
        return current
    current = run_management.repository.get_run(run.id)
    if current is None:
        raise RuntimeError(f"dispatched EvaluationRun disappeared: {run.id}")
    return current


def create_ab_runs(
    run_management: RunManagement,
    dispatcher: JobDispatcher,
    baseline_target: TargetSnapshot,
    candidate_target: TargetSnapshot,
    *,
    dataset_id: str,
    dataset_version: int | None = None,
    evaluator_refs: Sequence[EvaluatorRef] | None = None,
    metric_plan: MetricPlan | None = None,
    gate_spec: ReleaseGateSpec | None = None,
    timeout_seconds: float = 300,
) -> ABRunPair:
    """Create and independently dispatch one controlled pair of Runs."""

    _validate_variants(baseline_target, candidate_target)
    for target in (baseline_target, candidate_target):
        run_management.target_catalog.resolve_descriptor(
            target.ref,
            target.descriptor_sha256,
        )

    run_arguments = {
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "evaluator_refs": evaluator_refs,
        "metric_plan": metric_plan,
        "gate_spec": gate_spec,
        "timeout_seconds": timeout_seconds,
    }
    baseline_run = run_management.create_run(baseline_target, persist=False, **run_arguments)
    candidate_run = EvaluationRun(timing=baseline_run.timing, manifest=type(baseline_run.manifest).model_validate({
        **baseline_run.manifest.model_dump(exclude={"manifest_sha256"}), "target": candidate_target,
    }))
    from agentgate.domain.evaluation_task import EvaluationTask
    run_management.repository.save_task_runs(
        EvaluationTask(id=baseline_run.id, kind="ab", run_ids=(baseline_run.id, candidate_run.id)),
        [baseline_run, candidate_run],
    )

    dispatched_baseline = _dispatch_and_reload(
        run_management,
        dispatcher,
        baseline_run,
    )
    dispatched_candidate = _dispatch_and_reload(
        run_management,
        dispatcher,
        candidate_run,
    )
    return ABRunPair(
        baseline_run=dispatched_baseline,
        candidate_run=dispatched_candidate,
    )


__all__ = ["ABRunPair", "create_ab_runs"]
