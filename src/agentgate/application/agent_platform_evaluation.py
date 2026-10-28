"""Create exact-target platform tasks using existing persistence and dispatch."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Literal
from uuid import uuid4

from agentgate.application.credential_management import ApiKeyManagement
from agentgate.application.evaluator_management import EvaluatorManagement
from agentgate.application.run_management import RunManagement
from agentgate.domain import (
    EvaluationRun,
    EvaluatorRef,
    TargetDescriptor,
    TargetRef,
    TargetSnapshot,
    utcnow,
)
from agentgate.domain.evaluation_task import EvaluationTask
from agentgate.integrations.job_dispatchers import JobDispatcher
from agentgate.integrations.targets.agent_platform import PlatformClient, resolve_platform_target
from agentgate.storage.repository import AgentGateRepository

LOGGER = logging.getLogger(__name__)


def _log_inbank_run_saved(task: EvaluationTask, run: EvaluationRun, side: str) -> None:
    target = run.manifest.target
    LOGGER.info(
        "inbank_run_saved task_id=%r task_kind=%s side=%s run_id=%r "
        "agent_id=%r agent_version=%r invocation_agent_version=%r "
        "adapter_type=%r adapter_version=%r credential_ref=%r",
        task.id,
        task.kind,
        side,
        run.id,
        target.ref.external_target_id,
        target.ref.external_version_id,
        target.invocation_config.get("agent_version"),
        target.adapter_type,
        target.adapter_version,
        target.credential_ref,
    )


def _build_inbank_descriptor(
    *,
    team_id: str,
    agent_id: str,
    type_group: Literal["base/workflow", "abcclaw"],
    agent_version: str,
    arrange_type: Literal["base", "workflow"] | None,
    branch_id: str | None,
) -> TargetDescriptor:
    identity = {
        "team_id": team_id,
        "agent_id": agent_id,
        "type_group": type_group,
        "branch_id": branch_id,
    }
    source_id = (
        "inbank-platform-"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )
    metadata = {
        **identity,
        "arrange_type": arrange_type,
        "runtime_type": "abcclaw" if type_group == "abcclaw" else arrange_type,
        "simulated": False,
    }
    return TargetDescriptor(
        ref=TargetRef(
            source_id=source_id,
            target_type="agent",
            external_target_id=agent_id,
            external_version_id=agent_version,
        ),
        display_name=agent_id,
        metadata=metadata,
        input_schema=(
            {"type": "object", "minProperties": 1}
            if type_group == "base/workflow" and arrange_type in {"base", "workflow"}
            else {
                "type": "object",
                "required": ["txt"],
                "properties": {
                    "txt": {"type": "string"},
                    "appHistory": {},
                    "custID": {"type": ["string", "null"]},
                    "sessionId": {"type": ["string", "null"]},
                    "config_variables": {"type": ["array", "null"]},
                    "files": {"type": "array", "maxItems": 0},
                },
                "additionalProperties": False,
            }
        ),
    )


def _platform_target_snapshot(
    *,
    platform_mode: str,
    client: PlatformClient | None,
    token: str,
    team_id: str | None,
    agent_id: str,
    type_group: Literal["base/workflow", "abcclaw"],
    agent_version: str,
    arrange_type: Literal["base", "workflow"] | None,
    branch_id: str | None,
    max_retries: int,
) -> tuple[TargetDescriptor, str, dict]:
    if platform_mode == "mock":
        descriptor = resolve_platform_target(
            client,
            token,
            team_id=team_id,
            agent_id=agent_id,
            type_group=type_group,
            agent_version=agent_version,
            branch_id=branch_id,
        )
        return descriptor, "agent_platform_mock", dict(descriptor.metadata)
    if platform_mode == "inbank":
        if max_retries != 0:
            raise ValueError("inbank execution requires retries 0")
        if type_group == "abcclaw":
            if branch_id is None or arrange_type is not None:
                raise ValueError("invalid inbank abcclaw target")
            adapter_type = "inbank_yunxia"
            invocation_config = {
                "branch_id": branch_id,
                "agent_version": agent_version,
            }
        else:
            if arrange_type not in {"base", "workflow"} or branch_id is not None:
                raise ValueError("invalid inbank ChatABC target")
            adapter_type = "inbank_chatabc"
            invocation_config = {
                "arrange_type": arrange_type,
                "agent_version": agent_version,
            }
        descriptor = _build_inbank_descriptor(
            team_id=team_id,
            agent_id=agent_id,
            type_group=type_group,
            agent_version=agent_version,
            arrange_type=arrange_type,
            branch_id=branch_id,
        )
        return descriptor, adapter_type, invocation_config
    raise ConnectionError("agent platform mode must be mock or inbank")


def submit_platform_evaluation(
    *,
    repository: AgentGateRepository,
    evaluators: EvaluatorManagement,
    credentials: ApiKeyManagement | None,
    dispatcher: JobDispatcher,
    team_id: str | None,
    agent_id: str,
    type_group: Literal["base/workflow", "abcclaw"],
    agent_version: str,
    arrange_type: Literal["base", "workflow"] | None,
    branch_id: str | None,
    dataset_id: str,
    dataset_version: int,
    case_ids: tuple[str, ...] | None,
    evaluator_ids: tuple[str, ...] | None,
    max_parallel_cases: int,
    timeout_seconds: int,
    max_retries: int,
    repetitions: int,
    scheduled_for: datetime | None,
    token: str,
    user_team_id: str,
    user_id: str,
    user_name: str,
    name: str | None = None,
    evaluator_refs: tuple[EvaluatorRef, ...] | None = None,
) -> EvaluationTask:
    if credentials is None:
        raise ConnectionError("platform credential encryption is not configured")
    if scheduled_for is not None and (scheduled_for <= utcnow() or repetitions != 1):
        raise ValueError("invalid reservation")
    if not 1 <= repetitions <= 20:
        raise ValueError("invalid repetitions")
    if repository.get_dataset(dataset_id, user_team_id=user_team_id) is None:
        raise LookupError("dataset unavailable to caller")
    platform_mode = os.getenv("AGENTGATE_AGENT_PLATFORM_MODE", "").strip().lower()
    if platform_mode not in {"mock", "inbank"}:
        raise ConnectionError("agent platform mode must be mock or inbank")
    client = PlatformClient.from_environment() if platform_mode == "mock" else None
    descriptor, adapter_type, invocation_config = _platform_target_snapshot(
        platform_mode=platform_mode,
        client=client,
        token=token,
        team_id=team_id,
        agent_id=agent_id,
        type_group=type_group,
        agent_version=agent_version,
        arrange_type=arrange_type,
        branch_id=branch_id,
        max_retries=max_retries,
    )
    repository.save_target_descriptor(descriptor)
    task_id = str(uuid4())
    metadata = credentials.create_api_key(
        name="Platform task " + task_id,
        provider_id="agent-platform-" + platform_mode,
        scope="private",
        plaintext=token,
    )
    try:
        snapshot = TargetSnapshot(
            ref=descriptor.ref,
            display_name=descriptor.display_name,
            descriptor_sha256=descriptor.content_sha256,
            adapter_type=adapter_type,
            adapter_version="1",
            invocation_config=invocation_config,
            credential_ref=metadata.id,
        )
        management = RunManagement(repository, evaluators)
        run = management.create_run(
            snapshot,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            case_ids=case_ids,
            evaluator_ids=evaluator_ids,
            evaluator_refs=evaluator_refs,
            max_parallel_cases=max_parallel_cases,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            scheduled_for=scheduled_for,
            persist=False,
        )
        if platform_mode == "mock":
            for case in run.manifest.execution_cases:
                if case.initial_state:
                    raise ValueError("platform mock has no mutable initial business state")
                for turn in case.turns:
                    if (
                        set(turn.input) != {"txt"}
                        or not isinstance(turn.input["txt"], str)
                        or not turn.input["txt"].strip()
                    ):
                        raise ValueError(
                            "platform case input requires nonblank txt only"
                        )
        runs = [
            EvaluationRun(
                id=task_id if index == 0 else str(uuid4()),
                manifest=run.manifest,
                timing=run.timing,
                status=run.status,
                scheduled_for=run.scheduled_for,
                user_team_id=user_team_id,
                user_id=user_id,
                user_name=user_name,
            )
            for index in range(repetitions)
        ]
        task = EvaluationTask(
            id=task_id,
            name=name,
            kind="single" if repetitions == 1 else "stability",
            run_ids=tuple(r.id for r in runs),
            credential_id=metadata.id,
        )
    except Exception:
        credentials.delete_api_key(metadata.id)
        raise
    try:
        repository.save_task_runs(task, runs)
    except Exception:  # noqa: BLE001 -- Persistence outcome must never become a 4xx rejection.
        # A commit error may be ambiguous: retain the encrypted credential for reconciliation.
        raise RuntimeError("task persistence outcome is uncertain") from None
    if platform_mode == "inbank":
        # Stability repetitions share the single-target side.
        for item in runs:
            _log_inbank_run_saved(task, item, "single")
    for item in runs:
        if item.status == "scheduled":
            continue
        try:
            management.dispatch_run(item.id, dispatcher)
        except RuntimeError:
            # Existing RunManagement records dispatch failure. The created task still exists.
            continue
        except Exception:  # noqa: BLE001 -- Dispatch after commit has an uncertain outcome.
            raise RuntimeError("task exists but dispatch outcome is uncertain") from None
    return task


def submit_platform_comparison(
    *,
    repository: AgentGateRepository,
    evaluators: EvaluatorManagement,
    credentials: ApiKeyManagement | None,
    dispatcher: JobDispatcher,
    team_id: str | None,
    agent_id: str,
    type_group: Literal["base/workflow", "abcclaw"],
    baseline_version: str,
    candidate_version: str,
    arrange_type: Literal["base", "workflow"] | None,
    branch_id: str | None,
    dataset_id: str,
    dataset_version: int,
    case_ids: tuple[str, ...] | None,
    evaluator_ids: tuple[str, ...] | None,
    timeout_seconds: int,
    token: str,
    user_team_id: str,
    user_id: str,
    user_name: str,
    name: str | None = None,
    evaluator_refs: tuple[EvaluatorRef, ...] | None = None,
    max_parallel_cases: int = 1,
) -> tuple[EvaluationRun, EvaluationRun]:
    """Create and dispatch one controlled A/B pair of platform Runs."""
    if evaluator_ids is not None and evaluator_refs is not None:
        raise ValueError("use evaluator_ids or evaluator_refs, not both")
    if credentials is None:
        raise ConnectionError("platform credential encryption is not configured")
    if baseline_version == candidate_version:
        raise ValueError("A/B requires two different agent versions")
    if repository.get_dataset(dataset_id, user_team_id=user_team_id) is None:
        raise LookupError("dataset unavailable to caller")
    platform_mode = os.getenv("AGENTGATE_AGENT_PLATFORM_MODE", "").strip().lower()
    if platform_mode not in {"mock", "inbank"}:
        raise ConnectionError("agent platform mode must be mock or inbank")
    if platform_mode == "mock":
        max_parallel_cases = 1
    client = PlatformClient.from_environment() if platform_mode == "mock" else None

    task_id = str(uuid4())
    metadata = credentials.create_api_key(
        name="Platform comparison " + task_id,
        provider_id="agent-platform-" + platform_mode,
        scope="private",
        plaintext=token,
    )
    try:
        management = RunManagement(repository, evaluators)
        prepared: list[EvaluationRun] = []
        selected_specs = (
            evaluators.select_versions(evaluator_refs)
            if evaluator_refs is not None
            else evaluators.select(evaluator_ids)
        )
        if evaluator_refs is not None:
            primary_ids = tuple(ref.evaluator_id for ref in evaluator_refs)
        elif evaluator_ids is not None:
            primary_ids = evaluator_ids
        else:
            primary_ids = tuple(spec.id for spec in selected_specs)
        versions = {spec.id: spec.version for spec in selected_specs}
        selected_refs = tuple(
            EvaluatorRef(evaluator_id=evaluator_id, evaluator_version=versions[evaluator_id])
            for evaluator_id in primary_ids
        )
        for version in (baseline_version, candidate_version):
            descriptor, adapter_type, invocation_config = _platform_target_snapshot(
                platform_mode=platform_mode,
                client=client,
                token=token,
                team_id=team_id,
                agent_id=agent_id,
                type_group=type_group,
                agent_version=version,
                arrange_type=arrange_type,
                branch_id=branch_id,
                max_retries=0,
            )
            repository.save_target_descriptor(descriptor)
            snapshot = TargetSnapshot(
                ref=descriptor.ref,
                display_name=descriptor.display_name,
                descriptor_sha256=descriptor.content_sha256,
                adapter_type=adapter_type,
                adapter_version="1",
                invocation_config=invocation_config,
                credential_ref=metadata.id,
            )
            run = management.create_run(
                snapshot,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                case_ids=case_ids,
                evaluator_refs=selected_refs,
                max_parallel_cases=max_parallel_cases,
                timeout_seconds=timeout_seconds,
                max_retries=0,
                scheduled_for=None,
                persist=False,
            )
            if platform_mode == "mock":
                for case in run.manifest.execution_cases:
                    if case.initial_state:
                        raise ValueError("platform mock has no mutable initial business state")
                    for turn in case.turns:
                        if (
                            set(turn.input) != {"txt"}
                            or not isinstance(turn.input["txt"], str)
                            or not turn.input["txt"].strip()
                        ):
                            raise ValueError(
                                "platform case input requires nonblank txt only"
                            )
            prepared.append(
                EvaluationRun(
                    id=(task_id if not prepared else str(uuid4())),
                    manifest=run.manifest,
                timing=run.timing,
                    status=run.status,
                    scheduled_for=run.scheduled_for,
                    user_team_id=user_team_id,
                    user_id=user_id,
                    user_name=user_name,
                )
            )
        task = EvaluationTask(
            id=task_id,
            name=name,
            kind="ab",
            run_ids=tuple(item.id for item in prepared),
            credential_id=metadata.id,
        )
    except Exception:
        credentials.delete_api_key(metadata.id)
        raise
    try:
        repository.save_task_runs(task, tuple(prepared))
    except Exception:  # noqa: BLE001 -- Persistence outcome must never become a 4xx rejection.
        raise RuntimeError("task persistence outcome is uncertain") from None
    if platform_mode == "inbank":
        for side, item in zip(("baseline", "candidate"), prepared):
            _log_inbank_run_saved(task, item, side)
    for item in prepared:
        try:
            management.dispatch_run(item.id, dispatcher)
        except RuntimeError:
            continue
        except Exception:  # noqa: BLE001 -- Dispatch after commit has an uncertain outcome.
            raise RuntimeError("task exists but dispatch outcome is uncertain") from None
    return prepared[0], prepared[1]
