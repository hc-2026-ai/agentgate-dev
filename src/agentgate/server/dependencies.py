"""Process-lifetime dependencies used by AgentGate HTTP routes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import Request
from agentgate.application.evaluation_queries import EvaluationQueries

from agentgate.application import (
    ABRunPair,
    DatasetManagement,
    LineageQueries,
    OptimizationAnalysis,
    ResultReader,
    RunManagement,
    SkillAnalysis,
    TargetCatalog,
    create_ab_runs,
)
from agentgate.application.credential_management import ApiKeyManagement
from agentgate.application.evaluator_management import (
    EvaluatorManagement,
    build_default_evaluator_management,
)
from agentgate.application.result_case_writeback import ResultCaseWriteback
from agentgate.demo.bootstrap import (
    ensure_demo_dataset,
    ensure_loan_core_datasets,
    ensure_demo_target_descriptors,
)
from agentgate.demo.loan import LOAN_DATASET, LoanAgent
from agentgate.demo.targets import (
    build_demo_target_snapshot,
    get_demo_target_descriptor,
)
from agentgate.domain import (
    EvaluationRun,
    EvaluatorRef,
    RunStatus,
    SkillAnalysisReport,
    TargetSnapshot,
)
from agentgate.evaluator.judge import JudgeModelClient
from agentgate.integrations.credentials.encryption import ApiKeyEncryptor
from agentgate.integrations.credentials.environment import (
    API_KEY_ENCRYPTION_KEY_ENV,
    load_api_key_encryptor,
)
from agentgate.integrations.job_dispatchers import JobDispatcher
from agentgate.integrations.job_dispatchers.configuration import create_dispatcher
from agentgate.integrations.model_providers.environment import (
    load_judge_model_from_environment,
)
from agentgate.integrations.observability import (
    InMemoryTraceCapture,
    ingest_otlp_http_json,
)
from agentgate.integrations.targets import DemoLoanTargetAdapter
from agentgate.skill_analysis import analyze_skill_relationships
from agentgate.storage.configuration import SQLiteConfig, create_repository, load_database_config
from agentgate.storage.repository import AgentGateRepository


@dataclass(slots=True)
class ServerDependencies:
    """Explicit application and infrastructure dependencies for one FastAPI app."""

    repository: AgentGateRepository
    datasets: DatasetManagement
    evaluators: EvaluatorManagement
    targets: TargetCatalog
    runs: RunManagement
    results: ResultReader
    result_case_writeback: ResultCaseWriteback
    lineage: LineageQueries
    skill_analysis: SkillAnalysis
    optimization: OptimizationAnalysis
    api_keys: ApiKeyManagement | None
    dispatcher: JobDispatcher
    demo_state: dict[str, dict]
    _judge_client: JudgeModelClient | None = field(
        default=None,
        repr=False,
    )

    @property
    def evaluation_queries(self) -> EvaluationQueries:
        return EvaluationQueries(self.repository)

    def close(self) -> None:
        """Release process-lifetime resources owned by these dependencies."""

        client = self._judge_client
        self._judge_client = None
        try:
            if client is not None:
                client.close()
        finally:
            self.repository.close()

    def ingest_otlp_json(self, payload: dict[str, Any]) -> int:
        """Normalize and persist one external OTLP/HTTP JSON payload."""

        return ingest_otlp_http_json(payload, self.repository)

    def analyze_demo_target(self, version: str) -> SkillAnalysisReport:
        """Analyze the exact demo Target selected during Run configuration."""

        target = self._resolve_demo_target(version)
        return self.skill_analysis.analyze_target(target.descriptor_sha256)

    def submit_demo_run(
        self,
        version: str,
        *,
        dataset_id: str = LOAN_DATASET.id,
        dataset_version: int | None = None,
        case_ids: list[str] | None = None,
        evaluator_ids: list[str] | None = None,
        evaluator_refs: list[EvaluatorRef] | None = None,
        timeout_seconds: float = 300,
        max_parallel_cases: int = 1,
        max_retries: int = 0,
        scheduled_for: datetime | None = None,
        api_key: str | None = None,
    ) -> EvaluationRun:
        """Create one POC Loan Agent Run and dispatch it when eligible."""

        from agentgate.domain.evaluation_task import EvaluationTask
        run = self._create_demo_run(
            version,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            case_ids=case_ids,
            evaluator_ids=evaluator_ids,
            evaluator_refs=evaluator_refs,
            timeout_seconds=timeout_seconds,
            max_parallel_cases=max_parallel_cases,
            max_retries=max_retries,
            scheduled_for=scheduled_for,
            persist=False,
            api_key=api_key,
        )
        self.repository.save_task_runs(EvaluationTask(id=run.id, kind="single", run_ids=(run.id,)), [run])
        if run.status is RunStatus.SCHEDULED:
            return run
        return self.runs.dispatch_run(run.id, self.dispatcher)

    def submit_ab_runs(
        self,
        baseline_version: str,
        candidate_version: str,
        *,
        dataset_id: str = LOAN_DATASET.id,
        dataset_version: int | None = None,
        evaluator_refs: list[EvaluatorRef] | None = None,
    ) -> ABRunPair:
        """Create and dispatch one controlled pair of POC Loan Agent Runs."""

        return create_ab_runs(
            self.runs,
            self.dispatcher,
            self._resolve_demo_target(baseline_version),
            self._resolve_demo_target(candidate_version),
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            evaluator_refs=evaluator_refs,
        )

    def execute_demo_run(
        self,
        version: str,
        *,
        dataset_id: str = LOAN_DATASET.id,
        dataset_version: int | None = None,
        case_ids: list[str] | None = None,
        evaluator_ids: list[str] | None = None,
        evaluator_refs: list[EvaluatorRef] | None = None,
    ) -> EvaluationRun:
        """Create and synchronously execute one POC Loan Agent evaluation."""

        run = self._create_demo_run(
            version,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            case_ids=case_ids,
            evaluator_ids=evaluator_ids,
            evaluator_refs=evaluator_refs,
            timeout_seconds=300,
            max_parallel_cases=1,
            max_retries=0,
        )
        capture = InMemoryTraceCapture()
        try:
            adapter = DemoLoanTargetAdapter(
                capture, state_store=self.demo_state
            )
            return self.runs.execute_run(run.id, adapter, capture.resolve)
        finally:
            capture.shutdown()

    def submit_stability_runs(self, version: str, repetitions: int, **settings):
        from agentgate.domain.evaluation_task import EvaluationTask
        if isinstance(repetitions, bool) or not 2 <= repetitions <= 20:
            raise ValueError("repetitions must be 2 to 20")
        template = self._create_demo_run(version, persist=False, **settings)
        if template.status != RunStatus.PENDING:
            raise ValueError("stability does not support scheduling")
        runs = [template, *(EvaluationRun(timing=template.timing, manifest=template.manifest,
            user_team_id=template.user_team_id, user_id=template.user_id,
            user_name=template.user_name, api_key=template.api_key)
            for _ in range(repetitions - 1))]
        task = EvaluationTask(id=template.id, kind="stability", run_ids=tuple(r.id for r in runs))
        self.repository.save_task_runs(task, runs)
        for run in runs:
            try:
                self.runs.dispatch_run(run.id, self.dispatcher)
            except RuntimeError:
                # Dispatch failures are persisted by RunManagement; retain the group for inspection.
                continue
        return task

    def _create_demo_run(
        self,
        version: str,
        *,
        dataset_id: str,
        dataset_version: int | None,
        case_ids: list[str] | None,
        evaluator_ids: list[str] | None,
        timeout_seconds: float,
        max_parallel_cases: int,
        max_retries: int,
        scheduled_for: datetime | None = None,
        persist: bool = True,
        api_key: str | None = None,
        evaluator_refs: list[EvaluatorRef] | None = None,
    ) -> EvaluationRun:
        target = self._resolve_demo_target(version)
        return self.runs.create_run(
            target,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            case_ids=case_ids,
            evaluator_ids=evaluator_ids,
            evaluator_refs=evaluator_refs,
            timeout_seconds=timeout_seconds,
            max_parallel_cases=max_parallel_cases,
            max_retries=max_retries,
            scheduled_for=scheduled_for,
            persist=persist,
            api_key=api_key,
        )

    def _resolve_demo_target(self, version: str) -> TargetSnapshot:
        if version not in LoanAgent.versions:
            raise ValueError(f"unknown demo Target version: {version}")
        descriptor = get_demo_target_descriptor(version)
        resolved = self.targets.resolve_descriptor(
            descriptor.ref,
            descriptor.content_sha256,
        )
        return build_demo_target_snapshot(resolved)


def get_dependencies(request: Request) -> ServerDependencies:
    """Return the typed dependency container owned by the FastAPI app."""

    dependencies = getattr(request.app.state, "dependencies", None)
    if not isinstance(dependencies, ServerDependencies):
        raise RuntimeError("AgentGate server dependencies are not configured")
    return dependencies


def build_dependencies(
    database_path: str | Path | None = None,
    dispatcher: JobDispatcher | None = None,
    api_key_encryptor: ApiKeyEncryptor | None = None,
) -> ServerDependencies:
    """Build isolated dependencies for one AgentGate FastAPI application."""

    database_config = load_database_config(database_path=database_path)
    repository = create_repository(database_config)
    configured_judge = None
    try:
        dataset_management = DatasetManagement(repository)
        target_catalog = TargetCatalog(repository)
        if isinstance(database_config, SQLiteConfig):
            ensure_demo_target_descriptors(target_catalog)
            ensure_demo_dataset(repository)
            ensure_loan_core_datasets(repository)
        configured_api_key_encryptor = api_key_encryptor
        if (
            configured_api_key_encryptor is None
            and API_KEY_ENCRYPTION_KEY_ENV in os.environ
        ):
            configured_api_key_encryptor = load_api_key_encryptor()
        configured_judge = load_judge_model_from_environment()
        evaluator_management = (
            build_default_evaluator_management(repository)
            if configured_judge is None
            else build_default_evaluator_management(
                repository,
                judge_client=configured_judge.client,
                judge_model_id=configured_judge.model_id,
                judge_credential_ref=configured_judge.credential_ref,
            )
        )
        skill_analyzer = (
            None
            if configured_judge is None
            else partial(
                analyze_skill_relationships,
                model_client=configured_judge.client,
                model_id=configured_judge.model_id,
            )
        )
        return ServerDependencies(
            repository=repository,
            datasets=dataset_management,
            evaluators=evaluator_management,
            targets=target_catalog,
            runs=RunManagement(repository, evaluator_management),
            results=ResultReader(repository),
            result_case_writeback=ResultCaseWriteback(
                repository,
                dataset_management,
            ),
            lineage=LineageQueries(repository),
            skill_analysis=SkillAnalysis(repository, skill_analyzer),
            optimization=OptimizationAnalysis(
                repository,
                root_cause_model_client=(
                    configured_judge.client
                    if configured_judge is not None
                    else None
                ),
                root_cause_model_id=(
                    configured_judge.model_id
                    if configured_judge is not None
                    else None
                ),
            ),
            api_keys=(
                ApiKeyManagement(repository, configured_api_key_encryptor)
                if configured_api_key_encryptor is not None
                else None
            ),
            dispatcher=dispatcher or create_dispatcher(),
            demo_state={},
            _judge_client=(
                configured_judge.client if configured_judge is not None else None
            ),
        )
    except Exception:
        try:
            if configured_judge is not None:
                configured_judge.client.close()
        finally:
            repository.close()
        raise
