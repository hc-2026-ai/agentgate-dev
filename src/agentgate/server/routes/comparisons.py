"""Evaluation Run comparison endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from agentgate.domain import (
    ComparisonGateDecision,
    ComparisonGateSpec,
    EvaluatorRef,
    RunStatus,
)
from agentgate.result import EvaluationComparison, RunComparisonAnalysis
from agentgate.server.dependencies import ServerDependencies, get_dependencies
from agentgate.server.errors import (
    raise_conflict,
    raise_not_found,
    raise_service_unavailable,
    raise_unprocessable,
)


router = APIRouter(prefix="/api", tags=["comparisons"])
Dependencies = Annotated[ServerDependencies, Depends(get_dependencies)]


class EvaluatorVersionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class RunComparisonLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_version: str = Field(min_length=1)
    candidate_version: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_version: int = Field(ge=1)
    evaluators: list[EvaluatorVersionSelection] | None = None


class RunComparisonVariant(BaseModel):
    run_id: str
    status: RunStatus


class RunComparisonSubmission(BaseModel):
    baseline: RunComparisonVariant
    candidate: RunComparisonVariant


class RunComparisonGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_run_id: str = Field(min_length=1)
    candidate_run_id: str = Field(min_length=1)
    gate_spec: ComparisonGateSpec


@router.post(
    "/run-comparisons",
    status_code=202,
    response_model=RunComparisonSubmission,
)
def launch_run_comparison(
    request: RunComparisonLaunchRequest,
    dependencies: Dependencies,
) -> RunComparisonSubmission:
    try:
        pair = dependencies.submit_ab_runs(
            request.baseline_version,
            request.candidate_version,
            dataset_id=request.dataset_id,
            dataset_version=request.dataset_version,
            evaluator_refs=(
                [
                    EvaluatorRef(
                        evaluator_id=item.id,
                        evaluator_version=item.version,
                    )
                    for item in request.evaluators
                ]
                if request.evaluators is not None
                else None
            ),
        )
    except RuntimeError as error:
        raise_service_unavailable(error)
    except (LookupError, ValueError) as error:
        raise_unprocessable(error)
    return RunComparisonSubmission(
        baseline=RunComparisonVariant(
            run_id=pair.baseline_run.id,
            status=pair.baseline_run.status,
        ),
        candidate=RunComparisonVariant(
            run_id=pair.candidate_run.id,
            status=pair.candidate_run.status,
        ),
    )


@router.get("/run-comparisons")
def compare_runs(
    baseline_run_id: str,
    candidate_run_id: str,
    dependencies: Dependencies,
) -> EvaluationComparison:
    try:
        return dependencies.results.compare_runs(
            baseline_run_id,
            candidate_run_id,
        )
    except LookupError as error:
        raise_not_found(error)
    except ValueError as error:
        raise_conflict(error)


@router.get(
    "/run-comparisons/analysis",
    response_model=RunComparisonAnalysis,
)
def analyze_runs(
    baseline_run_id: str,
    candidate_run_id: str,
    dependencies: Dependencies,
) -> RunComparisonAnalysis:
    try:
        return dependencies.results.analyze_runs(
            baseline_run_id,
            candidate_run_id,
        )
    except LookupError as error:
        raise_not_found(error)
    except ValueError as error:
        raise_conflict(error)


@router.get(
    "/run-comparisons/gate-defaults",
    response_model=ComparisonGateSpec,
)
def get_comparison_gate_defaults(
    dependencies: Dependencies,
) -> ComparisonGateSpec:
    return dependencies.results.get_comparison_gate_defaults()


@router.post(
    "/run-comparisons/gate",
    response_model=ComparisonGateDecision,
)
def evaluate_comparison_gate(
    request: RunComparisonGateRequest,
    dependencies: Dependencies,
) -> ComparisonGateDecision:
    try:
        return dependencies.results.evaluate_comparison_gate(
            request.baseline_run_id,
            request.candidate_run_id,
            request.gate_spec,
        )
    except LookupError as error:
        raise_not_found(error)
    except ValueError as error:
        raise_conflict(error)
