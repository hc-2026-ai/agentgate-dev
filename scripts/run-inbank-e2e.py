#!/usr/bin/env python3
"""Run one real in-bank Agent evaluation from Pod creation through final report."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentgate.application import ResultReader, RunManagement, TargetCatalog
from agentgate.application.evaluator_management import build_default_evaluator_management
from agentgate.domain import (
    Case,
    CaseTurn,
    Dataset,
    DatasetVersion,
    DatasetVersionStatus,
    MatchesPattern,
    OutputExpectation,
    TargetDescriptor,
    TargetRef,
    TargetSnapshot,
    TargetType,
    utcnow,
)
from agentgate.domain.evaluation_task import EvaluationTask, EvaluationTaskKind
from agentgate.integrations.job_dispatchers.execution import execute_persisted_run
from agentgate.storage.configuration import create_repository, load_database_config

DEFAULT_QUESTION = "请简要介绍你能够提供的服务，并返回一段非空文本。"
LOGGER = logging.getLogger("agentgate.inbank.e2e")
JUDGE_ENV_NAMES = (
    "AGENTGATE_JUDGE_PROVIDER_ID",
    "AGENTGATE_JUDGE_BASE_URL",
    "AGENTGATE_JUDGE_API_KEY",
    "AGENTGATE_JUDGE_MODEL_ID",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute one real in-bank Run and write its final AgentGate report."
        )
    )
    parser.add_argument("--base-url", required=True, help="客户环境服务根地址")
    parser.add_argument("--agent-id", required=True, help="客户 Agent ID")
    parser.add_argument("--agent-version", required=True, help="客户 Agent 版本")
    parser.add_argument(
        "--branch-id",
        help="云虾分支 ID；--agent-type yunxia 时必填",
    )
    parser.add_argument(
        "--agent-type",
        required=True,
        choices=("base", "workflow", "yunxia"),
        help="被测智能体类型：基础编排、工作流或云虾",
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="唯一测试问题")
    parser.add_argument(
        "--agent-namespace", default="chatabc", help="删除 Pod 使用的命名空间"
    )
    parser.add_argument(
        "--test-customer-prefix",
        default="agentgate",
        help="云虾测试 customer_id 的隔离前缀",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("output/inbank-e2e.db"),
        help="独立 SQLite 数据库路径",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/inbank-e2e"),
        help="日志和报告输出目录",
    )
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--health-wait-seconds", type=float, default=600.0)
    parser.add_argument("--health-poll-interval-seconds", type=float, default=10.0)
    parser.add_argument(
        "--debug-sse",
        action="store_true",
        help="解析失败时将客户原始 SSE 响应预览写入本地日志（仅限受控测试）",
    )
    return parser.parse_args()


def configure_environment(args: argparse.Namespace) -> None:
    base_url = args.base_url.rstrip("/")
    args.database.parent.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "AGENTGATE_DB_TYPE": "sqlite",
            "AGENTGATE_DB": str(args.database.resolve()),
            "AGENTGATE_INBANK_CREATE_BASE_URL": base_url,
            "AGENTGATE_INBANK_CHATABC_HEALTH_BASE_URL": base_url,
            "AGENTGATE_INBANK_CHATABC_POD_API_BASE_URL": base_url,
            "AGENTGATE_INBANK_CHATABC_DELETE_BASE_URL": base_url,
            "AGENTGATE_INBANK_CHATABC_AGENT_NAMESPACE": args.agent_namespace,
            "AGENTGATE_INBANK_YUNXIA_HEALTH_BASE_URL": base_url,
            "AGENTGATE_INBANK_YUNXIA_POD_API_BASE_URL": base_url,
            "AGENTGATE_INBANK_YUNXIA_DELETE_BASE_URL": base_url,
            "AGENTGATE_INBANK_YUNXIA_AGENT_NAMESPACE": args.agent_namespace,
            "AGENTGATE_INBANK_YUNXIA_TEST_CUSTOMER_PREFIX": (
                args.test_customer_prefix
            ),
            "AGENTGATE_INBANK_REQUEST_TIMEOUT_SECONDS": str(
                args.request_timeout_seconds
            ),
            "AGENTGATE_INBANK_HEALTH_WAIT_SECONDS": str(args.health_wait_seconds),
            "AGENTGATE_INBANK_HEALTH_POLL_INTERVAL_SECONDS": str(
                args.health_poll_interval_seconds
            ),
            "AGENTGATE_INBANK_DEBUG_SSE_FAILURES": (
                "1" if args.debug_sse else "0"
            ),
        }
    )
    for name in JUDGE_ENV_NAMES:
        os.environ.pop(name, None)


def configure_logging(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = output_dir / f"inbank-e2e-{timestamp}.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=(logging.StreamHandler(), logging.FileHandler(path, encoding="utf-8")),
        force=True,
    )
    return path


def build_run(args: argparse.Namespace) -> tuple[str, str]:
    now = utcnow()
    suffix = uuid4().hex[:8]
    dataset = Dataset(
        id=f"inbank-e2e-{suffix}",
        name="行内 Agent 端到端最小验收集",
        description="一个 Case、一个 Turn，校验客户 Agent 返回非空文本。",
        created_at=now,
        updated_at=now,
    )
    initial_state = (
        {"prompt_variables": [], "tool_variables": []}
        if args.agent_type == "base"
        else {"config_variables": []}
    )
    case = Case(
        id="e2e-nonempty-output",
        name="客户 Agent 返回非空结果",
        initial_state=initial_state,
        turns=(
            CaseTurn(
                id="turn-1",
                input={"txt": args.question},
                expectations=(
                    OutputExpectation(
                        id="output-is-nonempty",
                        name="最终输出为非空文本",
                        path="output",
                        condition=MatchesPattern(pattern=r"(?s)\S"),
                    ),
                ),
            ),
        ),
    )
    version = DatasetVersion(
        id=f"inbank-e2e-{suffix}-v1",
        dataset_id=dataset.id,
        dataset_name=dataset.name,
        dataset_description=dataset.description,
        version=1,
        status=DatasetVersionStatus.PUBLISHED,
        cases=(case,),
        created_at=now,
        updated_at=now,
        published_at=now,
    )
    adapter_type = (
        "inbank_yunxia" if args.agent_type == "yunxia" else "inbank_chatabc"
    )
    ref = TargetRef(
        source_id=(
            "inbank-yunxia" if args.agent_type == "yunxia" else "inbank-chatabc"
        ),
        target_type=TargetType.AGENT,
        external_target_id=args.agent_id,
        external_version_id=args.agent_version,
    )
    descriptor = TargetDescriptor(
        ref=ref,
        display_name=f"行内 Agent {args.agent_id}",
        description="客户环境端到端验收目标",
    )
    target = TargetSnapshot(
        ref=ref,
        display_name=descriptor.display_name,
        adapter_type=adapter_type,
        adapter_version="1",
        descriptor_sha256=descriptor.content_sha256,
        invocation_config=(
            {
                "agent_version": args.agent_version,
                "branch_id": args.branch_id,
                "input_field": "txt",
            }
            if args.agent_type == "yunxia"
            else {
                "agent_version": args.agent_version,
                "arrange_type": args.agent_type,
                "input_field": "txt",
            }
        ),
    )

    with closing(create_repository(load_database_config())) as repository:
        repository.save_dataset_with_version(dataset, version)
        TargetCatalog(repository).register_descriptor(descriptor)
        runs = RunManagement(repository, build_default_evaluator_management(repository))
        run = runs.create_run(
            target,
            dataset_id=dataset.id,
            dataset_version=1,
            evaluator_ids=("final-output",),
            timeout_seconds=args.timeout_seconds,
            max_parallel_cases=1,
            max_retries=0,
            persist=False,
        )
        task = EvaluationTask(kind=EvaluationTaskKind.SINGLE, run_ids=(run.id,))
        repository.save_task_runs(task, (run,))
    return task.id, run.id


def flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def lifecycle_from_log(path: Path) -> dict[str, bool]:
    flush_logs()
    content = path.read_text(encoding="utf-8")
    return {
        phase: f"phase={phase} status=succeeded" in content
        for phase in ("create", "health", "delete")
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def failure_evidence(run_id: str, error: Exception) -> dict[str, Any]:
    with closing(create_repository(load_database_config())) as repository:
        run = repository.get_run(run_id)
        return {
            "run": run.model_dump(mode="json") if run is not None else None,
            "results": [
                item.model_dump(mode="json")
                for item in repository.list_results(run_id)
            ],
            "traces": [
                item.model_dump(mode="json") for item in repository.list_traces(run_id)
            ],
            "error_type": type(error).__name__,
            "error": str(error)[:500],
        }


def main() -> int:
    args = parse_args()
    if args.agent_type == "yunxia" and (
        not isinstance(args.branch_id, str)
        or not args.branch_id.strip()
        or args.branch_id.strip().casefold() in {"null", "none"}
    ):
        raise SystemExit("云虾测试必须提供非空 --branch-id")
    if args.agent_type != "yunxia" and args.branch_id is not None:
        raise SystemExit("--branch-id 仅用于云虾测试")
    if any(
        value <= 0
        for value in (
            args.timeout_seconds,
            args.request_timeout_seconds,
            args.health_wait_seconds,
            args.health_poll_interval_seconds,
        )
    ):
        raise SystemExit("超时和轮询参数必须大于 0")

    configure_environment(args)
    log_path = configure_logging(args.output_dir)
    task_id, run_id = build_run(args)
    customer_task_id = run_id[-8:]
    LOGGER.info(
        "inbank_e2e_start agent_type=%s task_id=%r run_id=%r customer_task_id=%r",
        args.agent_type,
        task_id,
        run_id,
        customer_task_id,
    )

    try:
        status = execute_persisted_run(run_id)
        with closing(create_repository(load_database_config())) as repository:
            report = ResultReader(repository).get_report(run_id)
        lifecycle = lifecycle_from_log(log_path)
        report_path = args.output_dir / f"report-{run_id}.json"
        payload = {
            "agent_type": args.agent_type,
            "task_id": task_id,
            "run_id": run_id,
            "customer_task_id": customer_task_id,
            "status": status,
            "pod_lifecycle": lifecycle,
            "report": report.model_dump(mode="json"),
        }
        write_json(report_path, payload)
        passed = (
            status == "completed"
            and all(lifecycle.values())
            and report.release_gate.outcome.value == "pass"
        )
        print(
            json.dumps(
                {
                    "passed": passed,
                    "agent_type": args.agent_type,
                    "task_id": task_id,
                    "run_id": run_id,
                    "customer_task_id": customer_task_id,
                    "status": status,
                    "pod_lifecycle": lifecycle,
                    "report_file": str(report_path),
                    "log_file": str(log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if passed else 2
    except Exception as error:  # noqa: BLE001 -- Preserve process-boundary evidence.
        lifecycle = lifecycle_from_log(log_path)
        diagnostic_path = args.output_dir / f"failure-{run_id}.json"
        evidence = {
            "agent_type": args.agent_type,
            "task_id": task_id,
            "run_id": run_id,
            "customer_task_id": customer_task_id,
            "pod_lifecycle": lifecycle,
            **failure_evidence(run_id, error),
        }
        write_json(diagnostic_path, evidence)
        print(
            json.dumps(
                {
                    "passed": False,
                    "agent_type": args.agent_type,
                    "task_id": task_id,
                    "run_id": run_id,
                    "customer_task_id": customer_task_id,
                    "pod_lifecycle": lifecycle,
                    "error_type": type(error).__name__,
                    "diagnostic_file": str(diagnostic_path),
                    "log_file": str(log_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
