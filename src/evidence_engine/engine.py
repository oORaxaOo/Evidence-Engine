"""A small, deterministic evidence ledger.

This module deliberately does not ask an LLM whether a task succeeded.
A task can pass only when predeclared criteria are supported by recorded,
structured evidence. Missing evidence is UNKNOWN, never PASS.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Iterator


class TaskStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"
    INVALID = "invalid"


@dataclass(frozen=True)
class Criterion:
    """Machine-checkable success condition fixed when a task is created."""

    kind: str
    field: str
    operator: str = "exists"
    expected: Any = None
    source: str | None = None
    min_evidence: int = 1

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.field.strip():
            raise ValueError("kind and field must be non-empty")
        if self.operator not in {"exists", "eq", "gte"}:
            raise ValueError("operator must be one of: exists, eq, gte")
        if self.min_evidence < 1:
            raise ValueError("min_evidence must be at least 1")
        if self.operator == "gte":
            if isinstance(self.expected, bool) or not isinstance(self.expected, (int, float)):
                raise ValueError("gte expected value must be numeric")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    status: TaskStatus
    criteria: tuple[Criterion, ...]
    created_at: str


@dataclass(frozen=True)
class CriterionResult:
    index: int
    status: VerificationStatus
    message: str
    evidence_ids: tuple[str, ...]
    observed_values: tuple[Any, ...]


@dataclass(frozen=True)
class VerificationReport:
    task_id: str
    status: VerificationStatus
    checks: tuple[CriterionResult, ...]
    verified_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _normalize_timestamp(value: str | None) -> str:
    if value is None:
        return _now()
    if not isinstance(value, str):
        raise ValueError("observed_at must be an ISO-8601 string with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not strict JSON: {exc}") from exc


def _hash_record(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


class EvidenceEngine:
    """SQLite-backed evidence ledger with deterministic task verification.

    Treat the database as an audit record, not as an authentication system.
    Source labels are descriptive metadata and do not prove source identity.
    """

    GENESIS_HASH = "0" * 64

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """Open, commit or roll back, and always close one database connection."""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    criteria_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    record_hash TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS verifications (
                    verification_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    verified_at TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS task_criteria_immutable
                BEFORE UPDATE OF criteria_json ON tasks
                BEGIN
                    SELECT RAISE(ABORT, 'task criteria are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS task_status_transition_guard
                BEFORE UPDATE OF status ON tasks
                WHEN NOT (
                    (OLD.status = 'planned' AND NEW.status = 'running')
                    OR (OLD.status = 'unknown' AND NEW.status = 'running')
                    OR (
                        OLD.status = 'running'
                        AND NEW.status = 'passed'
                        AND EXISTS (
                            SELECT 1 FROM verifications
                            WHERE task_id = OLD.task_id AND status = 'supported'
                        )
                    )
                    OR (
                        OLD.status = 'running'
                        AND NEW.status = 'failed'
                        AND EXISTS (
                            SELECT 1 FROM verifications
                            WHERE task_id = OLD.task_id AND status IN ('contradicted', 'invalid')
                        )
                    )
                    OR (
                        OLD.status = 'running'
                        AND NEW.status = 'unknown'
                        AND EXISTS (
                            SELECT 1 FROM verifications
                            WHERE task_id = OLD.task_id AND status = 'inconclusive'
                        )
                    )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task status transition or missing verification');
                END;

                CREATE TRIGGER IF NOT EXISTS evidence_no_update
                BEFORE UPDATE ON evidence
                BEGIN
                    SELECT RAISE(ABORT, 'evidence rows are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS evidence_no_delete
                BEFORE DELETE ON evidence
                BEGIN
                    SELECT RAISE(ABORT, 'evidence rows are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS verification_no_update
                BEFORE UPDATE ON verifications
                BEGIN
                    SELECT RAISE(ABORT, 'verification rows are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS verification_no_delete
                BEFORE DELETE ON verifications
                BEGIN
                    SELECT RAISE(ABORT, 'verification rows are append-only');
                END;
                """
            )

    def create_task(self, title: str, criteria: Iterable[Criterion]) -> Task:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        criteria_tuple = tuple(criteria)
        if not criteria_tuple:
            raise ValueError("at least one success criterion is required")
        if not all(isinstance(item, Criterion) for item in criteria_tuple):
            raise TypeError("criteria must contain Criterion objects")

        task_id = uuid.uuid4().hex
        created_at = _now()
        criteria_json = _canonical_json([item.to_dict() for item in criteria_tuple])
        with self._db() as db:
            db.execute(
                "INSERT INTO tasks(task_id,title,status,criteria_json,created_at) VALUES(?,?,?,?,?)",
                (task_id, title.strip(), TaskStatus.PLANNED.value, criteria_json, created_at),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task:
        with self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        raw_criteria = json.loads(row["criteria_json"])
        criteria = tuple(Criterion(**item) for item in raw_criteria)
        return Task(
            task_id=row["task_id"],
            title=row["title"],
            status=TaskStatus(row["status"]),
            criteria=criteria,
            created_at=row["created_at"],
        )

    def start_task(self, task_id: str) -> Task:
        with self._db() as db:
            row = db.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            if row["status"] not in {TaskStatus.PLANNED.value, TaskStatus.UNKNOWN.value}:
                raise ValueError("only planned or unknown tasks can be started")
            db.execute(
                "UPDATE tasks SET status=? WHERE task_id=?",
                (TaskStatus.RUNNING.value, task_id),
            )
        return self.get_task(task_id)

    def record_evidence(
        self,
        task_id: str,
        *,
        kind: str,
        source: str,
        payload: dict[str, Any],
        observed_at: str | None = None,
    ) -> str:
        if not kind.strip() or not source.strip():
            raise ValueError("kind and source must be non-empty")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a JSON object")
        payload_json = _canonical_json(payload)
        observed_at = _normalize_timestamp(observed_at)

        with self._db() as db:
            task = db.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"unknown task: {task_id}")
            if task["status"] != TaskStatus.RUNNING.value:
                raise ValueError("evidence can only be recorded for a running task")
            last = db.execute(
                "SELECT record_hash FROM evidence ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous_hash = last["record_hash"] if last else self.GENESIS_HASH
            evidence_id = uuid.uuid4().hex
            record = {
                "evidence_id": evidence_id,
                "task_id": task_id,
                "kind": kind,
                "source": source,
                "observed_at": observed_at,
                "payload_json": payload_json,
                "previous_hash": previous_hash,
            }
            record_hash = _hash_record(record)
            db.execute(
                """INSERT INTO evidence(
                    evidence_id,task_id,kind,source,observed_at,payload_json,
                    previous_hash,record_hash
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    evidence_id, task_id, kind, source, observed_at,
                    payload_json, previous_hash, record_hash,
                ),
            )
        return evidence_id

    def list_evidence(self, task_id: str) -> tuple[dict[str, Any], ...]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM evidence WHERE task_id=? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return tuple(
            {
                "evidence_id": row["evidence_id"],
                "kind": row["kind"],
                "source": row["source"],
                "observed_at": row["observed_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        )

    def verify_integrity(self) -> bool:
        with self._db() as db:
            rows = db.execute("SELECT * FROM evidence ORDER BY sequence").fetchall()
        previous_hash = self.GENESIS_HASH
        for row in rows:
            record = {
                "evidence_id": row["evidence_id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "source": row["source"],
                "observed_at": row["observed_at"],
                "payload_json": row["payload_json"],
                "previous_hash": row["previous_hash"],
            }
            if row["previous_hash"] != previous_hash or row["record_hash"] != _hash_record(record):
                return False
            previous_hash = row["record_hash"]
        return True

    def verify_task(self, task_id: str) -> VerificationReport:
        task = self.get_task(task_id)
        evidence = self.list_evidence(task_id)
        if not self.verify_integrity():
            checks = tuple(
                CriterionResult(i, VerificationStatus.INVALID, "evidence ledger integrity check failed", (), ())
                for i, _ in enumerate(task.criteria, start=1)
            )
            overall = VerificationStatus.INVALID
        else:
            checks_list: list[CriterionResult] = []
            for index, criterion in enumerate(task.criteria, start=1):
                matching = [
                    item for item in evidence
                    if item["kind"] == criterion.kind
                    and (criterion.source is None or item["source"] == criterion.source)
                ]
                if len(matching) < criterion.min_evidence:
                    checks_list.append(CriterionResult(
                        index, VerificationStatus.INCONCLUSIVE,
                        f"insufficient evidence: required {criterion.min_evidence}, found {len(matching)}",
                        tuple(item["evidence_id"] for item in matching), (),
                    ))
                    continue

                observed: list[Any] = []
                missing = 0
                mismatch = 0
                invalid = 0
                for item in matching:
                    payload = item["payload"]
                    if criterion.field not in payload:
                        missing += 1
                        continue
                    actual = payload[criterion.field]
                    observed.append(actual)
                    if criterion.operator == "exists":
                        mismatch += int(actual is None)
                    elif criterion.operator == "eq":
                        mismatch += int(type(actual) is not type(criterion.expected) or actual != criterion.expected)
                    elif criterion.operator == "gte":
                        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                            invalid += 1
                        elif actual < criterion.expected:
                            mismatch += 1

                if mismatch:
                    status = VerificationStatus.CONTRADICTED
                    message = f"criterion contradicted by {mismatch} observation(s)"
                elif missing or invalid:
                    status = VerificationStatus.INCONCLUSIVE
                    message = f"insufficient structured data: {missing} missing field(s), {invalid} invalid value(s)"
                else:
                    status = VerificationStatus.SUPPORTED
                    message = f"criterion supported by {len(matching)} observation(s)"
                checks_list.append(CriterionResult(
                    index, status, message,
                    tuple(item["evidence_id"] for item in matching), tuple(observed),
                ))
            checks = tuple(checks_list)
            if any(item.status is VerificationStatus.CONTRADICTED for item in checks):
                overall = VerificationStatus.CONTRADICTED
            elif any(item.status is not VerificationStatus.SUPPORTED for item in checks):
                overall = VerificationStatus.INCONCLUSIVE
            else:
                overall = VerificationStatus.SUPPORTED

        report = VerificationReport(task_id, overall, checks, _now())
        report_json = _canonical_json({
            "task_id": task_id,
            "status": overall.value,
            "verified_at": report.verified_at,
            "checks": [
                {
                    "index": check.index,
                    "status": check.status.value,
                    "message": check.message,
                    "evidence_ids": list(check.evidence_ids),
                    "observed_values": list(check.observed_values),
                }
                for check in checks
            ],
        })
        with self._db() as db:
            db.execute(
                "INSERT INTO verifications(verification_id,task_id,status,report_json,verified_at) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, task_id, overall.value, report_json, report.verified_at),
            )
        return report

    def finalize_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task.status is not TaskStatus.RUNNING:
            raise ValueError("only a running task can be finalized")
        report = self.verify_task(task_id)
        if report.status is VerificationStatus.SUPPORTED:
            status = TaskStatus.PASSED
        elif report.status is VerificationStatus.CONTRADICTED or report.status is VerificationStatus.INVALID:
            status = TaskStatus.FAILED
        else:
            status = TaskStatus.UNKNOWN
        with self._db() as db:
            db.execute("UPDATE tasks SET status=? WHERE task_id=?", (status.value, task_id))
        return self.get_task(task_id)
