"""MySQL persistence with explicit transactions and exact identity verification."""

from __future__ import annotations

from . import evaluation_projection as projection

import hashlib
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy import Connection, Table, and_, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from agentgate.domain import (
    Dataset,
    DatasetVersion,
    DatasetVersionStatus,
    EvaluationResult,
    EvaluationRun,
    Evaluator,
    EvaluatorDraft,
    EvaluatorSource,
    EvaluatorSpec,
    OptimizationReport,
    RunStatus,
    SkillAnalysisReport,
    SkillAnalysisReview,
    TargetDescriptor,
    TargetRef,
    TargetType,
    Trace,
    canonical_json,
    content_sha256,
    normalize_utc,
    transition_run,
    utcnow,
)
from agentgate.domain.credential import ApiKeyMetadata
from agentgate.domain.evaluation_task import EvaluationTask, task_with_rerun
from agentgate.domain.run import RunTiming
from agentgate.evaluator.versioning import publish_evaluator_draft as build_publication
from agentgate.storage import mysql_schema as s
from agentgate.storage.configuration import TDSQLConfig
from agentgate.storage.evaluator_groups import (
    validate_draft_update,
    validate_group_draft,
    validate_group_publications,
    validate_identity_update,
)

M = TypeVar("M", bound=BaseModel)
_KEY_FIELDS = {
    "id": "id_key",
    "dataset_id": "dataset_key",
    "evaluator_id": "evaluator_key",
    "run_id": "run_key",
    "case_id": "case_key",
    "trace_id": "trace_key",
    "report_id": "report_key",
    "finding_id": "finding_key",
    "task_id": "task_key",
    "user_team_id": "user_team_key",
    "evidence_key": "evidence_key_digest",
}


def _digest(*parts: str) -> bytes:
    return hashlib.sha256(canonical_json(("agentgate.mysql.identity.v1", *parts)).encode()).digest()


def _scalar(value):
    if isinstance(value, datetime):
        return normalize_utc(value, "database timestamp").replace(tzinfo=None)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bool):
        return int(value)
    return value


def _values(table: Table, model: BaseModel) -> dict:
    data = model.model_dump()
    if table is s.traces:
        data["id"] = model.trace_id
    if table is s.evaluator_versions:
        data["evaluator_id"] = model.id
        data["version"] = _version(model.version)
    if table is s.target_descriptors:
        data.update(model.ref.model_dump())
        data["target_ref_key"] = _target_key(model.ref)
    if table is s.dataset_versions:
        data["draft_slot"] = 1 if model.status == DatasetVersionStatus.DRAFT else None
        data["sample_count"] = len(model.cases)
    if table is s.evaluation_tasks:
        data["first_run_key"] = _digest(model.run_ids[0])
    for original, key in _KEY_FIELDS.items():
        if key in table.c and original in data:
            data[key] = _digest(data[original])
    payload = "metadata_payload" if table is s.api_keys else "payload"
    data[payload] = canonical_json(model)
    return {column.name: _scalar(data[column.name]) for column in table.c if column.name in data}


def _target_key(ref: TargetRef) -> bytes:
    return _digest(
        ref.source_id, ref.target_type.value, ref.external_target_id, ref.external_version_id
    )


def _where(table: Table, filters: dict):
    conditions = []
    for name, value in filters.items():
        key = _KEY_FIELDS.get(name)
        if key and key in table.c:
            conditions.append(table.c[key] == _digest(value))
        else:
            conditions.append(table.c[name] == _scalar(value))
    return and_(*conditions) if conditions else True


def _rows(db: Connection, table: Table, *, lock: bool = False, **filters) -> list:
    query = select(table).where(_where(table, filters))
    if lock:
        query = query.with_for_update()
    rows = db.execute(query).mappings().all()
    exact = []
    for row in rows:
        for name, value in filters.items():
            if row[name] != _scalar(value):
                if _KEY_FIELDS.get(name) and _KEY_FIELDS[name] in table.c:
                    raise ValueError("database identity digest collision")
                break
        else:
            exact.append(row)
    return exact


def _models(db: Connection, table: Table, model_type: type[M], *, lock=False, **filters) -> list[M]:
    models = []
    for row in _rows(db, table, lock=lock, **filters):
        model = model_type.model_validate_json(row["payload"])
        indexed = {
            name: value
            for name, value in _values(table, model).items()
            if name not in ("payload", "metadata_payload")
            and not (table is s.dataset_versions and name == "sample_count")
            and not (table is s.evaluation_tasks and name in {"name", "kind", "first_run_key"})
        }
        if any(row[name] != value for name, value in indexed.items()):
            raise ValueError("stored indexed columns do not match payload")
        models.append(model)
    return models


def _one(db: Connection, table: Table, model_type: type[M], *, lock=False, **filters) -> M | None:
    models = _models(db, table, model_type, lock=lock, **filters)
    if len(models) > 1:
        raise ValueError("expected a unique stored identity")
    return models[0] if models else None


def _insert(db: Connection, table: Table, model: BaseModel) -> None:
    db.execute(table.insert().values(**_values(table, model)))


def _update(db: Connection, table: Table, model: BaseModel) -> None:
    values = _values(table, model)
    condition = and_(*(column == values[column.name] for column in table.primary_key.columns))
    db.execute(table.update().where(condition).values(**values))


def _version(value: str) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("Evaluator version must be a canonical positive integer")
    number = int(value)
    if number < 1 or str(number) != value or number > 2**63 - 1:
        raise ValueError("Evaluator version must be a canonical positive BIGINT")
    return number


def _next_evaluator_version(db: Connection, evaluator_id: str) -> int:
    items = _models(db, s.evaluator_versions, EvaluatorSpec, evaluator_id=evaluator_id)
    return max((_version(item.version) for item in items), default=0) + 1


def _cleanup_owned_evaluator_drafts(
    db: Connection,
    evaluator_id: str,
    user_team_id: str,
    *,
    keep: set[str] | None = None,
) -> None:
    candidates = _models(db, s.evaluators, Evaluator, user_team_id=user_team_id)
    for child in sorted(candidates, key=lambda item: item.id):
        if child.owner_evaluator_id != evaluator_id or child.id in (keep or set()):
            continue
        _one(db, s.evaluators, Evaluator, id=child.id, lock=True)
        db.execute(s.evaluator_drafts.delete().where(
            s.evaluator_drafts.c.evaluator_key == _digest(child.id),
        ))
        if _next_evaluator_version(db, child.id) == 1:
            db.execute(s.evaluators.delete().where(s.evaluators.c.id_key == _digest(child.id)))


def _ordered(items: list[M], field: str = "created_at", *, reverse=True) -> list[M]:
    # Python sorts complete IDs; MySQL LONGTEXT ordering can truncate comparisons.
    return sorted(
        sorted(items, key=lambda item: item.id),
        key=lambda item: getattr(item, field),
        reverse=reverse,
    )


def _limit(value: int | None) -> None:
    if value is not None and value < 1:
        raise ValueError("list limit must be at least 1")


class MySQLRepository:
    def __init__(self, config: TDSQLConfig) -> None:
        url = make_url(config.url).set(
            username=config.username, password=config.password.get_secret_value()
        )
        self._engine = create_engine(
            url,
            pool_size=5,
            max_overflow=5,
            pool_timeout=10,
            pool_pre_ping=True,
            pool_recycle=1800,
            hide_parameters=True,
            isolation_level="READ COMMITTED",
            connect_args={
                "charset": "utf8mb4",
                "connect_timeout": 5,
                "read_timeout": 30,
                "write_timeout": 30,
                "init_command": "SET time_zone = '+00:00'",
            },
        )
        self._pid = os.getpid()
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._engine.dispose()

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        if self._closed or os.getpid() != self._pid:
            raise RuntimeError("repository must be open and owned by the current process")
        try:
            with self._engine.begin() as connection:
                yield connection
        except IntegrityError:
            raise ValueError("database uniqueness or reference constraint conflict") from None
        except DBAPIError:
            # Do not replay an operation whose commit outcome may be unknown.
            raise RuntimeError(
                "MySQL operation failed; check connectivity, schema, and transaction conflicts"
            ) from None

    def save_dataset(self, dataset: Dataset) -> None:
        with self._transaction() as db:
            stored = _one(db, s.datasets, Dataset, id=dataset.id, lock=True)
            if stored:
                if dataset.created_at != stored.created_at:
                    raise ValueError("Dataset created_at is immutable")
                if dataset.deleted_version_high_watermark < stored.deleted_version_high_watermark:
                    raise ValueError("Dataset deleted version watermark cannot decrease")
                if dataset.updated_at < stored.updated_at:
                    raise ValueError("cannot save a stale Dataset")
                _update(db, s.datasets, dataset)
            else:
                _insert(db, s.datasets, dataset)

    def save_dataset_with_version(self, dataset: Dataset, version: DatasetVersion) -> None:
        if version.dataset_id != dataset.id:
            raise ValueError("DatasetVersion must belong to Dataset")
        with self._transaction() as db:
            _insert(db, s.datasets, dataset)
            _insert(db, s.dataset_versions, version)

    def get_dataset(self, dataset_id: str, *, user_team_id: str) -> Dataset | None:
        with self._transaction() as db:
            return _one(db, s.datasets, Dataset, id=dataset_id, user_team_id=user_team_id)

    def list_datasets(self, include_archived: bool = False, *, user_team_id: str) -> list[Dataset]:
        with self._transaction() as db:
            items = _models(db, s.datasets, Dataset, user_team_id=user_team_id)
        return _ordered(
            [item for item in items if include_archived or not item.archived], "updated_at"
        )

    def save_dataset_version(self, version: DatasetVersion) -> None:
        with self._transaction() as db:
            if not _one(db, s.datasets, Dataset, id=version.dataset_id, lock=True):
                raise ValueError("unknown Dataset")
            stored = _one(db, s.dataset_versions, DatasetVersion, id=version.id, lock=True)
            if stored:
                if stored.status == DatasetVersionStatus.PUBLISHED:
                    if stored != version:
                        raise ValueError("published DatasetVersion is immutable")
                    return
                if version.status != DatasetVersionStatus.DRAFT:
                    raise ValueError("draft DatasetVersion cannot be published through save")
                if (
                    version.dataset_id != stored.dataset_id
                    or version.created_at != stored.created_at
                ):
                    raise ValueError("DatasetVersion identity and created_at are immutable")
                if version.updated_at < stored.updated_at:
                    raise ValueError("cannot save a stale DatasetVersion draft")
                _update(db, s.dataset_versions, version)
            else:
                _insert(db, s.dataset_versions, version)

    def list_dataset_versions(
        self, dataset_id: str, include_draft: bool = True, *, user_team_id: str
    ) -> list[DatasetVersion]:
        with self._transaction() as db:
            items = _models(
                db,
                s.dataset_versions,
                DatasetVersion,
                dataset_id=dataset_id,
                user_team_id=user_team_id,
            )
        return sorted(
            [v for v in items if include_draft or v.status == DatasetVersionStatus.PUBLISHED],
            key=lambda v: (v.status != DatasetVersionStatus.DRAFT, -(v.version or 0)),
        )

    def get_dataset_draft(self, dataset_id: str, *, user_team_id: str) -> DatasetVersion | None:
        with self._transaction() as db:
            return _one(
                db,
                s.dataset_versions,
                DatasetVersion,
                dataset_id=dataset_id,
                status="draft",
                user_team_id=user_team_id,
            )

    def get_published_dataset_version(
        self, dataset_id: str, version: int, *, user_team_id: str
    ) -> DatasetVersion | None:
        with self._transaction() as db:
            return _one(
                db,
                s.dataset_versions,
                DatasetVersion,
                dataset_id=dataset_id,
                version=version,
                status="published",
                user_team_id=user_team_id,
            )

    def get_latest_published_dataset_version(
        self, dataset_id: str, *, user_team_id: str
    ) -> DatasetVersion | None:
        items = self.list_dataset_versions(dataset_id, False, user_team_id=user_team_id)
        return items[0] if items else None

    def delete_dataset_draft(
        self, dataset_id: str, expected_draft_id: str, *, user_team_id: str
    ) -> None:
        with self._transaction() as db:
            _one(db, s.datasets, Dataset, id=dataset_id, lock=True)
            draft = _one(
                db,
                s.dataset_versions,
                DatasetVersion,
                id=expected_draft_id,
                dataset_id=dataset_id,
                status="draft",
                user_team_id=user_team_id,
                lock=True,
            )
            if draft is None:
                raise ValueError("expected Dataset draft does not exist")
            db.execute(
                s.dataset_versions.delete().where(s.dataset_versions.c.id_key == _digest(draft.id))
            )
            if not _rows(db, s.dataset_versions, dataset_id=dataset_id, user_team_id=user_team_id):
                db.execute(
                    s.datasets.delete().where(
                        s.datasets.c.id_key == _digest(dataset_id),
                        s.datasets.c.user_team_key == _digest(user_team_id),
                    )
                )

    def delete_dataset_version(self, dataset_id: str, version: int, *, user_team_id: str) -> None:
        with self._transaction() as db:
            dataset = _one(db, s.datasets, Dataset, id=dataset_id, user_team_id=user_team_id, lock=True)
            if dataset is None:
                raise ValueError("expected Dataset does not exist")
            published = _one(db, s.dataset_versions, DatasetVersion, dataset_id=dataset_id,
                             version=version, status="published", user_team_id=user_team_id, lock=True)
            if published is None:
                raise ValueError("expected Dataset publication does not exist")
            db.execute(s.dataset_versions.delete().where(s.dataset_versions.c.id_key == _digest(published.id)))
            if _rows(db, s.dataset_versions, dataset_id=dataset_id, user_team_id=user_team_id):
                _update(db, s.datasets, dataset.model_copy(update={
                    "deleted_version_high_watermark": max(dataset.deleted_version_high_watermark, version),
                }))
            else:
                db.execute(s.datasets.delete().where(s.datasets.c.id_key == _digest(dataset_id)))

    def delete_dataset_record(self, dataset_id: str, *, user_team_id: str) -> None:
        with self._transaction() as db:
            dataset = _one(
                db, s.datasets, Dataset, id=dataset_id, user_team_id=user_team_id, lock=True
            )
            if dataset is None:
                raise ValueError("expected Dataset does not exist")
            db.execute(
                s.dataset_versions.delete().where(
                    s.dataset_versions.c.dataset_key == _digest(dataset_id),
                    s.dataset_versions.c.user_team_key == _digest(user_team_id),
                )
            )
            db.execute(s.datasets.delete().where(s.datasets.c.id_key == _digest(dataset_id)))

    def delete_task_record(self, task_id: str, *, user_team_id: str) -> None:
        with self._transaction() as db:
            task = _one(
                db, s.evaluation_tasks, EvaluationTask, id=task_id, lock=True
            )
            if task is None:
                raise ValueError("task does not exist")
            if any(_one(db, s.runs, EvaluationRun, id=run_id, user_team_id=user_team_id) is None
                   for run_id in task.run_ids + task.previous_run_ids):
                raise ValueError("task does not exist")
            for run_id in task.run_ids + task.previous_run_ids:
                projection.remove_run(db, run_id)
                db.execute(
                    s.run_asset_refs.delete().where(s.run_asset_refs.c.run_key == _digest(run_id))
                )
                db.execute(
                    s.optimization_reports.delete().where(s.optimization_reports.c.run_key == _digest(run_id))
                )
                db.execute(
                    s.results.delete().where(s.results.c.run_key == _digest(run_id))
                )
                db.execute(
                    s.traces.delete().where(s.traces.c.run_key == _digest(run_id))
                )
                db.execute(
                    s.runs.delete().where(s.runs.c.id_key == _digest(run_id))
                )
            db.execute(
                s.evaluation_task_runs.delete().where(s.evaluation_task_runs.c.task_key == _digest(task_id))
            )
            db.execute(
                s.evaluation_tasks.delete().where(
                    s.evaluation_tasks.c.id_key == _digest(task_id)
                )
            )

    def replace_dataset_draft(self, expected_draft_id: str, published: DatasetVersion) -> None:
        with self._transaction() as db:
            _one(db, s.datasets, Dataset, id=published.dataset_id, lock=True)
            draft = _one(
                db,
                s.dataset_versions,
                DatasetVersion,
                id=expected_draft_id,
                status="draft",
                user_team_id=published.user_team_id,
                lock=True,
            )
            if draft is None:
                raise ValueError("expected Dataset draft does not exist")
            if published.status != DatasetVersionStatus.PUBLISHED or published.id == draft.id:
                raise ValueError("replacement must be a publication with a new identity")
            for field in ("dataset_id", "content_sha256", "created_at", "based_on_version"):
                if getattr(draft, field) != getattr(published, field):
                    raise ValueError(f"published DatasetVersion must preserve draft {field}")
            if published.updated_at < draft.updated_at:
                raise ValueError("cannot replace a newer DatasetVersion draft")
            _insert(db, s.dataset_versions, published)
            db.execute(
                s.dataset_versions.delete().where(s.dataset_versions.c.id_key == _digest(draft.id))
            )

    def save_evaluator(self, evaluator: Evaluator) -> None:
        if evaluator.source != EvaluatorSource.USER:
            raise ValueError("only user Evaluators can be persisted")
        with self._transaction() as db:
            stored = _one(db, s.evaluators, Evaluator, id=evaluator.id, lock=True)
            if stored:
                validate_identity_update(evaluator, stored)
                if evaluator.created_at != stored.created_at or evaluator.source != stored.source:
                    raise ValueError("Evaluator source and created_at are immutable")
                if evaluator.updated_at < stored.updated_at:
                    raise ValueError("cannot save a stale Evaluator")
                _update(db, s.evaluators, evaluator)
            else:
                _insert(db, s.evaluators, evaluator)

    def save_evaluator_with_draft(self, evaluator: Evaluator, draft: EvaluatorDraft) -> None:
        if evaluator.source != EvaluatorSource.USER:
            raise ValueError("only user Evaluators can be persisted")
        if draft.evaluator_id != evaluator.id or draft.created_at < evaluator.created_at:
            raise ValueError("EvaluatorDraft must belong to Evaluator and not precede its creation")
        with self._transaction() as db:
            _insert(db, s.evaluators, evaluator)
            _insert(db, s.evaluator_drafts, draft)

    def get_evaluator(self, evaluator_id: str, *, user_team_id: str) -> Evaluator | None:
        with self._transaction() as db:
            return _one(db, s.evaluators, Evaluator, id=evaluator_id, user_team_id=user_team_id)

    def save_evaluator_group_draft(
        self,
        parent: Evaluator,
        draft: EvaluatorDraft,
        children: Sequence[tuple[Evaluator, EvaluatorDraft]],
        *,
        expected_updated_at: datetime | None,
        expected_evaluator_updated_at: datetime | None,
        user_team_id: str,
    ) -> None:
        with self._transaction() as db:
            stored_parent = _one(db, s.evaluators, Evaluator, id=parent.id, lock=True)
            stored_draft = _one(
                db, s.evaluator_drafts, EvaluatorDraft, evaluator_id=parent.id, lock=True,
            )
            validate_group_draft(
                parent, draft, children,
                stored_parent=stored_parent,
                stored_draft=stored_draft,
                expected_updated_at=expected_updated_at,
                expected_evaluator_updated_at=expected_evaluator_updated_at,
                user_team_id=user_team_id,
            )
            identities = {parent.id: stored_parent}
            drafts = {parent.id: stored_draft}
            next_versions = {}
            for child, child_draft in sorted(children, key=lambda pair: pair[0].id):
                identities[child.id] = _one(db, s.evaluators, Evaluator, id=child.id, lock=True)
                drafts[child.id] = _one(
                    db, s.evaluator_drafts, EvaluatorDraft, evaluator_id=child.id, lock=True,
                )
                validate_identity_update(child, identities[child.id])
                validate_draft_update(child_draft, child, drafts[child.id])
                next_versions[child.id] = _next_evaluator_version(db, child.id)
            if any(next_versions[ref.evaluator_id] != _version(ref.evaluator_version)
                   for ref in draft.children):
                raise ValueError("Evaluator group child version changed; reload before saving")
            for identity, item in ((parent, draft), *children):
                (_update if identities[identity.id] else _insert)(db, s.evaluators, identity)
                (_update if drafts[identity.id] else _insert)(db, s.evaluator_drafts, item)
            _cleanup_owned_evaluator_drafts(
                db, parent.id, user_team_id, keep={child.id for child, _ in children},
            )

    def publish_evaluator_group_draft(
        self,
        expected_draft_id: str,
        publications: Sequence[EvaluatorSpec],
        *,
        expected_updated_at: datetime,
        user_team_id: str,
    ) -> None:
        with self._transaction() as db:
            identities = {}
            ordered = list(publications[:1]) + sorted(publications[1:], key=lambda item: item.id)
            for spec in ordered:
                identity = _one(db, s.evaluators, Evaluator, id=spec.id, lock=True)
                if identity is not None:
                    identities[spec.id] = identity
            validate_group_publications(publications, identities, user_team_id=user_team_id)
            drafts = {}
            for index, spec in enumerate(publications):
                item = _one(
                    db, s.evaluator_drafts, EvaluatorDraft, evaluator_id=spec.id, lock=True,
                )
                if item is None or (index == 0 and item.id != expected_draft_id):
                    raise ValueError("expected Evaluator draft does not exist")
                if index == 0 and item.updated_at != expected_updated_at:
                    raise ValueError("Evaluator group draft changed; reload before publishing")
                validate_draft_update(item, identities[spec.id], None)
                version = _next_evaluator_version(db, spec.id)
                if _version(spec.version) != version:
                    raise ValueError(f"Evaluator publication requires version {version}")
                if spec != build_publication(identities[spec.id], item, version):
                    raise ValueError("published EvaluatorSpec does not match the current draft")
                drafts[spec.id] = item
            now = utcnow()
            for spec in publications:
                _insert(db, s.evaluator_versions, spec)
                db.execute(s.evaluator_drafts.delete().where(
                    s.evaluator_drafts.c.id_key == _digest(drafts[spec.id].id),
                ))
                identity = identities[spec.id]
                _update(db, s.evaluators, identity.model_copy(update={
                    "enabled": True, "updated_at": max(now, identity.updated_at),
                }))

    def list_evaluators(
        self, include_disabled: bool = False, *, user_team_id: str
    ) -> list[Evaluator]:
        with self._transaction() as db:
            items = _models(db, s.evaluators, Evaluator, user_team_id=user_team_id)
        return _ordered([item for item in items if include_disabled or item.enabled], "updated_at")

    def delete_unpublished_evaluator(self, evaluator_id: str, *, user_team_id: str) -> None:
        with self._transaction() as db:
            if (
                _one(
                    db,
                    s.evaluators,
                    Evaluator,
                    id=evaluator_id,
                    user_team_id=user_team_id,
                    lock=True,
                )
                is None
            ):
                raise ValueError("unknown Evaluator")
            if _rows(db, s.evaluator_versions, evaluator_id=evaluator_id):
                raise ValueError("published Evaluator cannot be deleted")
            _cleanup_owned_evaluator_drafts(db, evaluator_id, user_team_id)
            db.execute(
                s.evaluator_drafts.delete().where(
                    s.evaluator_drafts.c.evaluator_key == _digest(evaluator_id)
                )
            )
            db.execute(s.evaluators.delete().where(s.evaluators.c.id_key == _digest(evaluator_id)))

    def discard_evaluator_group_draft(
        self,
        evaluator_id: str,
        expected_draft_id: str,
        expected_draft_updated_at: datetime,
        *,
        user_team_id: str,
    ) -> None:
        with self._transaction() as db:
            parent = _one(db, s.evaluators, Evaluator, id=evaluator_id, lock=True)
            draft = _one(db, s.evaluator_drafts, EvaluatorDraft, evaluator_id=evaluator_id, lock=True)
            if parent is None or parent.user_team_id != user_team_id or parent.owner_evaluator_id is not None:
                raise ValueError("unknown Evaluator group")
            if draft is None or draft.id != expected_draft_id or draft.updated_at != expected_draft_updated_at:
                raise ValueError("Evaluator group draft changed; reload before discarding")
            if draft.kind.value != "llm_group" or draft.user_team_id != user_team_id:
                raise ValueError("unknown Evaluator group")
            db.execute(s.evaluator_drafts.delete().where(s.evaluator_drafts.c.id_key == _digest(draft.id)))
            _cleanup_owned_evaluator_drafts(db, evaluator_id, user_team_id)
            if _next_evaluator_version(db, evaluator_id) == 1:
                db.execute(s.evaluators.delete().where(s.evaluators.c.id_key == _digest(evaluator_id)))

    def save_evaluator_draft(self, draft: EvaluatorDraft) -> None:
        with self._transaction() as db:
            evaluator = _one(db, s.evaluators, Evaluator, id=draft.evaluator_id, lock=True)
            if evaluator is None:
                raise ValueError("unknown Evaluator")
            validate_draft_update(draft, evaluator, None)
            if draft.created_at < evaluator.created_at:
                raise ValueError("EvaluatorDraft cannot precede Evaluator creation")
            stored = _one(db, s.evaluator_drafts, EvaluatorDraft, id=draft.id, lock=True)
            if stored:
                for field in ("evaluator_id", "created_at", "based_on_version"):
                    if getattr(stored, field) != getattr(draft, field):
                        raise ValueError(f"EvaluatorDraft {field} is immutable")
                if draft.updated_at < stored.updated_at:
                    raise ValueError("cannot save a stale EvaluatorDraft")
                _update(db, s.evaluator_drafts, draft)
            else:
                _insert(db, s.evaluator_drafts, draft)

    def get_evaluator_draft(self, evaluator_id: str, *, user_team_id: str) -> EvaluatorDraft | None:
        with self._transaction() as db:
            return _one(
                db,
                s.evaluator_drafts,
                EvaluatorDraft,
                evaluator_id=evaluator_id,
                user_team_id=user_team_id,
            )

    def delete_evaluator_draft(
        self, evaluator_id: str, expected_draft_id: str, *, user_team_id: str
    ) -> None:
        with self._transaction() as db:
            _one(db, s.evaluators, Evaluator, id=evaluator_id, lock=True)
            draft = _one(
                db,
                s.evaluator_drafts,
                EvaluatorDraft,
                evaluator_id=evaluator_id,
                id=expected_draft_id,
                user_team_id=user_team_id,
                lock=True,
            )
            if draft is None:
                raise ValueError("expected Evaluator draft does not exist")
            db.execute(
                s.evaluator_drafts.delete().where(s.evaluator_drafts.c.id_key == _digest(draft.id))
            )
            _cleanup_owned_evaluator_drafts(db, evaluator_id, user_team_id)

    def list_evaluator_versions(
        self, evaluator_id: str, *, user_team_id: str
    ) -> list[EvaluatorSpec]:
        with self._transaction() as db:
            items = _models(
                db,
                s.evaluator_versions,
                EvaluatorSpec,
                evaluator_id=evaluator_id,
                user_team_id=user_team_id,
            )
        return sorted(items, key=lambda item: _version(item.version), reverse=True)

    def get_evaluator_version(
        self, evaluator_id: str, version: str, *, user_team_id: str
    ) -> EvaluatorSpec | None:
        with self._transaction() as db:
            return _one(
                db,
                s.evaluator_versions,
                EvaluatorSpec,
                evaluator_id=evaluator_id,
                version=_version(version),
                user_team_id=user_team_id,
            )

    def get_latest_evaluator_version(
        self, evaluator_id: str, *, user_team_id: str
    ) -> EvaluatorSpec | None:
        items = self.list_evaluator_versions(evaluator_id, user_team_id=user_team_id)
        return items[0] if items else None

    def publish_evaluator_draft(self, expected_draft_id: str, published: EvaluatorSpec) -> None:
        with self._transaction() as db:
            evaluator = _one(db, s.evaluators, Evaluator, id=published.id, lock=True)
            draft = _one(db, s.evaluator_drafts, EvaluatorDraft, id=expected_draft_id, lock=True)
            if evaluator is None or draft is None or draft.evaluator_id != evaluator.id:
                raise ValueError("expected Evaluator draft does not exist")
            versions = _models(db, s.evaluator_versions, EvaluatorSpec, evaluator_id=evaluator.id)
            next_version = max((_version(item.version) for item in versions), default=0) + 1
            if _version(published.version) != next_version:
                raise ValueError(f"Evaluator publication requires version {next_version}")
            if published != build_publication(evaluator, draft, next_version):
                raise ValueError("published EvaluatorSpec does not match the current draft")
            _insert(db, s.evaluator_versions, published)
            db.execute(
                s.evaluator_drafts.delete().where(s.evaluator_drafts.c.id_key == _digest(draft.id))
            )

    def save_target_descriptor(self, descriptor: TargetDescriptor) -> None:
        with self._transaction() as db:
            stored = _one(
                db,
                s.target_descriptors,
                TargetDescriptor,
                content_sha256=descriptor.content_sha256,
                lock=True,
            )
            if stored:
                if stored.model_dump(exclude={"fetched_at"}) != descriptor.model_dump(
                    exclude={"fetched_at"}
                ):
                    raise ValueError("TargetDescriptor content hash collision")
            else:
                _insert(db, s.target_descriptors, descriptor)

    def get_target_descriptor(self, content_sha256: str) -> TargetDescriptor | None:
        with self._transaction() as db:
            return _one(db, s.target_descriptors, TargetDescriptor, content_sha256=content_sha256)

    def list_target_descriptors(self, ref: TargetRef | None = None) -> list[TargetDescriptor]:
        with self._transaction() as db:
            items = _models(
                db,
                s.target_descriptors,
                TargetDescriptor,
                **({"target_ref_key": _target_key(ref)} if ref else {}),
            )
        if ref is not None and any(item.ref != ref for item in items):
            raise ValueError("database identity digest collision")
        return sorted(
            sorted(items, key=lambda item: item.content_sha256),
            key=lambda item: item.fetched_at,
            reverse=True,
        )

    def save_skill_analysis_report(self, report: SkillAnalysisReport) -> None:
        with self._transaction() as db:
            if not _one(
                db,
                s.target_descriptors,
                TargetDescriptor,
                content_sha256=report.target_descriptor_sha256,
                lock=True,
            ):
                raise ValueError("unknown TargetDescriptor")
            stored = _one(
                db, s.skill_analysis_reports, SkillAnalysisReport, id=report.id, lock=True
            )
            if stored:
                if stored != report:
                    raise ValueError("SkillAnalysisReport is immutable")
            else:
                _insert(db, s.skill_analysis_reports, report)

    def get_skill_analysis_report(self, report_id: str) -> SkillAnalysisReport | None:
        with self._transaction() as db:
            return _one(db, s.skill_analysis_reports, SkillAnalysisReport, id=report_id)

    def list_skill_analysis_reports(
        self, target_descriptor_sha256: str, limit: int = 50
    ) -> list[SkillAnalysisReport]:
        _limit(limit)
        with self._transaction() as db:
            items = _models(
                db,
                s.skill_analysis_reports,
                SkillAnalysisReport,
                target_descriptor_sha256=target_descriptor_sha256,
            )
        return _ordered(items)[:limit]

    def save_skill_analysis_review(self, report_id: str, review: SkillAnalysisReview) -> None:
        with self._transaction() as db:
            if not _one(db, s.skill_analysis_reports, SkillAnalysisReport, id=report_id, lock=True):
                raise ValueError("unknown SkillAnalysisReport")
            filters = {"report_id": report_id, "finding_id": review.finding_id}
            values = _values(s.skill_analysis_reviews, review)
            values.update(report_id=report_id, report_key=_digest(report_id))
            if _rows(db, s.skill_analysis_reviews, lock=True, **filters):
                db.execute(
                    s.skill_analysis_reviews.update()
                    .where(_where(s.skill_analysis_reviews, filters))
                    .values(**values)
                )
            else:
                db.execute(s.skill_analysis_reviews.insert().values(**values))

    def list_skill_analysis_reviews(self, report_id: str) -> list[SkillAnalysisReview]:
        with self._transaction() as db:
            items = _models(db, s.skill_analysis_reviews, SkillAnalysisReview, report_id=report_id)
        return sorted(items, key=lambda item: item.finding_id)

    def save_api_key(self, metadata: ApiKeyMetadata, encrypted_api_key: str) -> None:
        if not isinstance(encrypted_api_key, str) or not encrypted_api_key.strip():
            raise ValueError("encrypted API Key must be a nonblank string")
        with self._transaction() as db:
            values = _values(s.api_keys, metadata)
            values["encrypted_api_key"] = encrypted_api_key
            db.execute(s.api_keys.insert().values(**values))

    def get_api_key_metadata(self, api_key_id: str) -> ApiKeyMetadata | None:
        with self._transaction() as db:
            rows = _rows(db, s.api_keys, id=api_key_id)
        return ApiKeyMetadata.model_validate_json(rows[0]["metadata_payload"]) if rows else None

    def list_api_key_metadata(self) -> list[ApiKeyMetadata]:
        with self._transaction() as db:
            items = [
                ApiKeyMetadata.model_validate_json(row["metadata_payload"])
                for row in _rows(db, s.api_keys)
            ]
        return _ordered(items, reverse=False)

    def get_encrypted_api_key(self, api_key_id: str) -> str | None:
        with self._transaction() as db:
            rows = _rows(db, s.api_keys, id=api_key_id)
        return rows[0]["encrypted_api_key"] if rows else None

    def delete_api_key(self, api_key_id: str) -> None:
        with self._transaction() as db:
            if not _rows(db, s.api_keys, id=api_key_id, lock=True):
                raise ValueError("unknown API Key")
            db.execute(s.api_keys.delete().where(s.api_keys.c.id_key == _digest(api_key_id)))

    def _insert_run(self, db: Connection, run: EvaluationRun) -> None:
        _insert(db, s.runs, run)
        projection.put_run(db, run)
        manifest = run.manifest
        dataset = manifest.dataset
        refs = [("dataset", "", dataset.dataset_id, str(dataset.version), dataset.content_sha256)]
        refs.extend(
            ("case", dataset.dataset_id, case.id, str(dataset.version), content_sha256(case))
            for case in manifest.execution_cases
        )
        target = manifest.target
        refs.append(
            (
                target.ref.target_type.value,
                target.ref.source_id,
                target.ref.external_target_id,
                target.ref.external_version_id,
                target.descriptor_sha256,
            )
        )
        descriptor = _one(
            db, s.target_descriptors, TargetDescriptor, content_sha256=target.descriptor_sha256
        )
        if descriptor:
            if descriptor.ref != target.ref:
                raise ValueError("TargetDescriptor reference does not match TargetSnapshot")
            refs.extend(
                (
                    "skill",
                    descriptor.ref.source_id,
                    skill.external_skill_id,
                    skill.external_version_id,
                    content_sha256(skill),
                )
                for skill in descriptor.skills
            )
        refs.extend(
            ("evaluator", "", spec.id, spec.version, spec.content_sha256)
            for spec in manifest.evaluator_specs
        )
        for kind, source_id, asset_id, version, digest in refs:
            db.execute(
                s.run_asset_refs.insert().values(
                    reference_key=_digest(run.id, kind, source_id, asset_id, version, digest),
                    run_key=_digest(run.id),
                    run_id=run.id,
                    asset_lookup_key=_digest(kind, source_id, asset_id, version),
                    asset_kind=kind,
                    source_id=source_id,
                    asset_id=asset_id,
                    version=version,
                    content_sha256=digest,
                )
            )

    def audit_evaluation_summaries(self):
        with self._transaction() as db:
            return projection.audit(db)

    def list_summary_backfill_ids(self, *, entity, after_id=None, limit=50):
        with self._transaction() as db:
            return projection.backfill_ids(db, entity, after_id, limit)

    def read_run_summary_source(self, run_id):
        with self._transaction() as db:
            return projection.source(db, run_id)

    def rebuild_run_summary(self, source, summary):
        with self._transaction() as db:
            return projection.rebuild_run(db, source, summary)

    def rebuild_task_query_fields(self, task_id):
        with self._transaction() as db:
            return projection.rebuild_entity(db, 'task', task_id)

    def rebuild_dataset_version_query_fields(self, version_id):
        with self._transaction() as db:
            return projection.rebuild_entity(db, 'dataset_version', version_id)

    def save_completed_run(self, run, summary):
        with self._transaction() as db:
            stored = _one(db, s.runs, EvaluationRun, id=run.id, lock=True)
            if stored is None:
                raise ValueError("unknown EvaluationRun")
            if stored.status not in {RunStatus.RUNNING, RunStatus.COMPLETED}:
                raise ValueError("run cannot complete from its current state")
            if any(getattr(stored, f) != getattr(run, f) for f in ("manifest", "created_at", "scheduled_for", "started_at", "user_team_id")):
                raise ValueError("immutable run fields changed")
            if stored.completed_at is not None and stored != run:
                raise ValueError("terminal EvaluationRun is immutable")
            results = _models(db, s.results, EvaluationResult, run_id=run.id)
            projection.put_completed(db, run, summary, results)
            _update(db, s.runs, run)

    def get_evaluation_overview_summary(self, query, *, user_team_id):
        with self._transaction() as db:
            return projection.overview(db, query, user_team_id)

    def list_evaluation_task_summaries(self, query, *, user_team_id):
        with self._transaction() as db:
            return projection.task_page(db, query, user_team_id)

    def save_run(self, run: EvaluationRun) -> None:
        with self._transaction() as db:
            stored = _one(db, s.runs, EvaluationRun, id=run.id, lock=True)
            if stored is None:
                self._insert_run(db, run)
                return
            if stored == run:
                return
            for field in ("manifest", "created_at", "scheduled_for"):
                if getattr(stored, field) != getattr(run, field):
                    raise ValueError(f"EvaluationRun {field} is immutable")
            if stored.started_at is not None and stored.started_at != run.started_at:
                raise ValueError("EvaluationRun started_at is immutable once set")
            if stored.completed_at is not None:
                raise ValueError("terminal EvaluationRun is immutable")
            _update(db, s.runs, run)

    def get_run(self, run_id: str, *, user_team_id: str | None = None) -> EvaluationRun | None:
        with self._transaction() as db:
            return _one(
                db,
                s.runs,
                EvaluationRun,
                id=run_id,
                **({"user_team_id": user_team_id} if user_team_id is not None else {}),
            )

    def list_runs(self, limit: int = 50, *, user_team_id: str) -> list[EvaluationRun]:
        _limit(limit)
        with self._transaction() as db:
            items = _models(db, s.runs, EvaluationRun, user_team_id=user_team_id)
        return _ordered(items)[:limit]

    def list_runs_by_status(
        self,
        status: RunStatus,
        limit: int | None = None,
        oldest_first: bool = False,
        *,
        user_team_id: str | None = None,
    ) -> list[EvaluationRun]:
        _limit(limit)
        with self._transaction() as db:
            items = _models(
                db,
                s.runs,
                EvaluationRun,
                status=status,
                **({"user_team_id": user_team_id} if user_team_id is not None else {}),
            )
        items.sort(key=lambda item: item.id)
        items.sort(
            key=lambda item: (
                (item.scheduled_for or item.created_at)
                if status in {RunStatus.SCHEDULED, RunStatus.PENDING}
                else item.created_at
            ),
            reverse=not oldest_first,
        )
        return items[:limit]

    def count_runs_by_status(self, *, user_team_id: str) -> dict[RunStatus, int]:
        counts = dict.fromkeys(RunStatus, 0)
        with self._transaction() as db:
            for run in _models(db, s.runs, EvaluationRun, user_team_id=user_team_id):
                counts[run.status] += 1
        return counts

    def recover_run(
        self, expected: EvaluationRun, *, now: datetime, error: str | None = None
    ) -> EvaluationRun | None:
        now = normalize_utc(now, "Run recovery time")
        with self._transaction() as db:
            stored = _one(db, s.runs, EvaluationRun, id=expected.id, lock=True)
            if stored != expected or stored.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                return None
            if error is None:
                if stored.status is not RunStatus.PENDING or stored.timing.pending_started_at is not None:
                    return None
                recovered = stored.model_copy(update={"timing": RunTiming(pending_started_at=now)})
            else:
                recovered = transition_run(stored, RunStatus.FAILED, occurred_at=now, error=error)
            _update(db, s.runs, recovered)
            projection.put_run(db, recovered)
            return recovered

    def count_active_runs_by_api_key(self, api_key: str | None) -> int:
        with self._transaction() as db:
            active: list[EvaluationRun] = []
            for status in (RunStatus.PENDING, RunStatus.RUNNING):
                active.extend(_models(db, s.runs, EvaluationRun, status=status))
        return sum(1 for run in active if run.api_key == api_key)

    def claim_waiting_run(
        self, run_id: str, claimed_at: datetime
    ) -> EvaluationRun | None:
        with self._transaction() as db:
            run = _one(db, s.runs, EvaluationRun, id=run_id, lock=True)
            if run is None or run.status != RunStatus.WAITING:
                return None
            pending = transition_run(run, RunStatus.PENDING, occurred_at=claimed_at)
            _update(db, s.runs, pending)
            return pending

    def claim_pending_run(self, run_id: str, started_at: datetime) -> EvaluationRun | None:
        with self._transaction() as db:
            run = _one(db, s.runs, EvaluationRun, id=run_id, lock=True)
            if run is None or run.status != RunStatus.PENDING:
                return None
            running = transition_run(run, RunStatus.RUNNING, occurred_at=started_at)
            _update(db, s.runs, running)
            return running

    def claim_due_scheduled_runs(self, due_at: datetime, limit: int = 100) -> list[EvaluationRun]:
        _limit(limit)
        now = normalize_utc(due_at, "scheduled Run due_at")
        with self._transaction() as db:
            # A locking read avoids duplicate claims without requiring SKIP LOCKED support.
            candidates = _models(db, s.runs, EvaluationRun, status="scheduled", lock=True)
            due = sorted(
                (run for run in candidates if run.scheduled_for <= now),
                key=lambda run: (run.scheduled_for, run.created_at, run.id),
            )[:limit]
            pending = [transition_run(run, RunStatus.PENDING, occurred_at=now) for run in due]
            for run in pending:
                _update(db, s.runs, run)
            return pending

    def cancel_run(
        self, run_id: str, cancelled_at: datetime, *, user_team_id: str
    ) -> EvaluationRun | None:
        with self._transaction() as db:
            run = _one(db, s.runs, EvaluationRun, id=run_id, user_team_id=user_team_id, lock=True)
            if run is None or run.status not in {
                RunStatus.PENDING,
                RunStatus.SCHEDULED,
                RunStatus.WAITING,
                RunStatus.RUNNING,
            }:
                return None
            cancelled = transition_run(run, RunStatus.CANCELLED, occurred_at=cancelled_at)
            _update(db, s.runs, cancelled)
            return cancelled

    def _list_runs_by_asset(
        self, kind, source_id, asset_id, version, *, limit, user_team_id, content_sha256=None
    ) -> list[EvaluationRun]:
        _limit(limit)
        with self._transaction() as db:
            refs = _rows(
                db, s.run_asset_refs, asset_lookup_key=_digest(kind, source_id, asset_id, version)
            )
            found = {}
            for row in refs:
                if (row["asset_kind"], row["source_id"], row["asset_id"], row["version"]) != (
                    kind,
                    source_id,
                    asset_id,
                    version,
                ):
                    raise ValueError("database identity digest collision")
                if content_sha256 is not None and row["content_sha256"] != content_sha256:
                    continue
                run = _one(db, s.runs, EvaluationRun, id=row["run_id"], user_team_id=user_team_id)
                if run:
                    found[run.id] = run
        return _ordered(list(found.values()))[:limit]

    def list_runs_by_dataset_version(
        self, dataset_id: str, version: int, limit: int = 50, *, user_team_id: str
    ) -> list[EvaluationRun]:
        return self._list_runs_by_asset(
            "dataset", "", dataset_id, str(version), limit=limit, user_team_id=user_team_id
        )

    def list_runs_by_case_content(
        self,
        dataset_id: str,
        version: int,
        case_id: str,
        content_sha256: str,
        limit: int = 50,
        *,
        user_team_id: str,
    ) -> list[EvaluationRun]:
        return self._list_runs_by_asset(
            "case",
            dataset_id,
            case_id,
            str(version),
            limit=limit,
            content_sha256=content_sha256,
            user_team_id=user_team_id,
        )

    def list_runs_by_target_version(
        self,
        source_id: str,
        target_type: TargetType,
        target_id: str,
        version: str,
        limit: int = 50,
        *,
        content_sha256: str | None = None,
        user_team_id: str,
    ) -> list[EvaluationRun]:
        return self._list_runs_by_asset(
            target_type.value,
            source_id,
            target_id,
            version,
            limit=limit,
            content_sha256=content_sha256,
            user_team_id=user_team_id,
        )

    def list_runs_by_skill_version(
        self,
        source_id: str,
        skill_id: str,
        version: str,
        limit: int = 50,
        *,
        content_sha256: str | None = None,
        user_team_id: str,
    ) -> list[EvaluationRun]:
        return self._list_runs_by_asset(
            "skill",
            source_id,
            skill_id,
            version,
            limit=limit,
            content_sha256=content_sha256,
            user_team_id=user_team_id,
        )

    def list_runs_by_evaluator_version(
        self, evaluator_id: str, version: str, limit: int = 50, *, user_team_id: str
    ) -> list[EvaluationRun]:
        return self._list_runs_by_asset(
            "evaluator", "", evaluator_id, version, limit=limit, user_team_id=user_team_id
        )

    def save_trace(self, trace: Trace) -> None:
        with self._transaction() as db:
            # Traces may precede Run persistence, as in the SQLite contract.
            stored = _one(db, s.traces, Trace, id=trace.trace_id, lock=True)
            if stored:
                if (stored.run_id, stored.case_id) != (trace.run_id, trace.case_id):
                    raise ValueError("Trace Run and Case identity are immutable")
                _update(db, s.traces, trace)
            else:
                _insert(db, s.traces, trace)

    def get_trace(self, run_id: str, case_id: str) -> Trace | None:
        with self._transaction() as db:
            return _one(db, s.traces, Trace, run_id=run_id, case_id=case_id)

    def list_traces(self, run_id: str) -> list[Trace]:
        with self._transaction() as db:
            items = _models(db, s.traces, Trace, run_id=run_id)
        return sorted(items, key=lambda item: (item.case_id, item.trace_id))

    def save_results(self, results: Sequence[EvaluationResult]) -> None:
        items = tuple(results)
        if len({item.id for item in items}) != len(items):
            raise ValueError("EvaluationResult ids must be unique within a batch")
        if len({(item.run_id, item.case_id, item.evaluator_id) for item in items}) != len(items):
            raise ValueError("EvaluationResults must be unique by Run, Case, and Evaluator")
        with self._transaction() as db:
            for run_id in sorted({item.run_id for item in items}):
                if _one(db, s.runs, EvaluationRun, id=run_id, lock=True) is None:
                    raise ValueError("unknown EvaluationRun")
            for item in items:
                trace = _one(db, s.traces, Trace, id=item.trace_id, lock=True)
                if trace is None or (trace.run_id, trace.case_id) != (item.run_id, item.case_id):
                    raise ValueError("EvaluationResult requires a matching Run and Trace")
                stored = _one(db, s.results, EvaluationResult, id=item.id, lock=True)
                if stored:
                    if stored != item:
                        raise ValueError("EvaluationResult is immutable")
                else:
                    _insert(db, s.results, item)
                    projection.update_progress(db, item.run_id)

    def list_results(self, run_id: str) -> list[EvaluationResult]:
        with self._transaction() as db:
            items = _models(db, s.results, EvaluationResult, run_id=run_id)
        return sorted(items, key=lambda item: (item.case_id, item.evaluator_id, item.id))

    def save_task_rerun(
        self, task_id: str, source_run_id: str, rerun: EvaluationRun, *, user_team_id: str,
    ) -> EvaluationTask:
        with self._transaction() as db:
            task = _one(db, s.evaluation_tasks, EvaluationTask, id=task_id, lock=True)
            source = _one(db, s.runs, EvaluationRun, id=source_run_id, user_team_id=user_team_id)
            if task is None or source is None:
                raise LookupError("unknown evaluation task")
            updated = task_with_rerun(task, source, rerun)
            self._insert_run(db, rerun)
            db.execute(s.evaluation_task_runs.insert().values(
                run_key=_digest(rerun.id), run_id=rerun.id,
                task_key=_digest(task_id), task_id=task_id,
            ))
            _update(db, s.evaluation_tasks, updated)
            projection.put_task(db, updated)
        return updated

    def save_task_runs(self, task: EvaluationTask, runs: Sequence[EvaluationRun]) -> None:
        if task.previous_run_ids:
            raise ValueError("new task cannot contain execution history")
        if task.run_ids != tuple(run.id for run in runs):
            raise ValueError("invalid task run associations")
        if any(run.status not in {RunStatus.PENDING, RunStatus.SCHEDULED} for run in runs):
            raise ValueError("task creation requires unstarted runs")
        if task.kind == "stability" and any(
            run.manifest != runs[0].manifest or run.status != RunStatus.PENDING for run in runs
        ):
            raise ValueError("stability requires identical snapshots and pending runs")
        with self._transaction() as db:
            for run in runs:
                self._insert_run(db, run)
            self._save_task(db, task)

    def _save_task(self, db: Connection, task: EvaluationTask) -> EvaluationTask:
        for run_id in sorted(task.run_ids):
            if not _one(db, s.runs, EvaluationRun, id=run_id, lock=True):
                raise ValueError("unknown EvaluationRun")
        previous = _one(db, s.evaluation_tasks, EvaluationTask, id=task.id, lock=True)
        if previous:
            if task.previous_run_ids and task.previous_run_ids != previous.previous_run_ids:
                raise ValueError("task execution history is immutable")
            for field in ("kind", "run_ids", "git_commit_refs", "credential_id"):
                if getattr(previous, field) != getattr(task, field):
                    raise ValueError("task identity and run associations are immutable")
        reports = {}
        for report_id in (previous.static_report_ids if previous else ()) + task.static_report_ids:
            report = _one(
                db, s.skill_analysis_reports, SkillAnalysisReport, id=report_id, lock=True
            )
            if report is None:
                raise ValueError("unknown static report")
            reports[report.target_descriptor_sha256] = report_id
        if previous:
            task = EvaluationTask.model_validate(
                {**previous.model_dump(), "name": task.name if task.name is not None else previous.name, "static_report_ids": tuple(reports.values())}
            )
            _update(db, s.evaluation_tasks, task)
        else:
            if task.previous_run_ids:
                raise ValueError("new task cannot contain execution history")
            _insert(db, s.evaluation_tasks, task)
            for run_id in task.run_ids:
                db.execute(
                    s.evaluation_task_runs.insert().values(
                        run_key=_digest(run_id),
                        run_id=run_id,
                        task_key=_digest(task.id),
                        task_id=task.id,
                    )
                )
        projection.put_task(db, task)
        return task

    def save_evaluation_task(self, task: EvaluationTask) -> EvaluationTask:
        with self._transaction() as db:
            return self._save_task(db, task)

    def get_evaluation_task(self, task_id: str) -> EvaluationTask | None:
        with self._transaction() as db:
            return _one(db, s.evaluation_tasks, EvaluationTask, id=task_id)

    def list_evaluation_tasks(self) -> list[EvaluationTask]:
        with self._transaction() as db:
            return _ordered(_models(db, s.evaluation_tasks, EvaluationTask))

    def save_optimization_report(
        self, evidence_key: str, report: OptimizationReport
    ) -> OptimizationReport:
        with self._transaction() as db:
            if not _one(db, s.runs, EvaluationRun, id=report.run_id, lock=True):
                raise ValueError("unknown EvaluationRun")
            stored = _one(
                db, s.optimization_reports, OptimizationReport, evidence_key=evidence_key, lock=True
            )
            if stored:
                return stored
            values = _values(s.optimization_reports, report)
            values.update(evidence_key=evidence_key, evidence_key_digest=_digest(evidence_key))
            db.execute(s.optimization_reports.insert().values(**values))
            return report

    def get_optimization_report(self, evidence_key: str) -> OptimizationReport | None:
        with self._transaction() as db:
            return _one(db, s.optimization_reports, OptimizationReport, evidence_key=evidence_key)
