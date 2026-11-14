"""Read-only, snapshot-consistent query facade for assistant-facing access."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .engine import Criterion, TaskStatus, _hash_record


_MAX_PAGE_SIZE = 500
_DEFAULT_PAGE_SIZE = 100
_GENESIS_HASH = "0" * 64


class EvidenceReader:
    """Expose evidence reports through a SQLite-enforced read-only connection.

    The reader deliberately has no methods to create tasks, append evidence,
    change task state, or register execution policies. Host integrations should
    give an assistant this reader (or a narrow wrapper around it), while keeping
    EvidenceEngine and all writing adapters outside model-callable tools.
    """

    def __init__(self, db_path: str | Path, *, timeout_seconds: float = 3.0):
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive number")

        try:
            resolved = Path(db_path).expanduser().resolve(strict=True)
        except (OSError, TypeError) as exc:
            raise FileNotFoundError(f"evidence database is not available: {db_path}") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"evidence database is not a regular file: {resolved}")

        self.db_path = resolved
        self.timeout_seconds = float(timeout_seconds)

    def _connect(self) -> sqlite3.Connection:
        encoded_path = quote(self.db_path.as_posix(), safe="/")
        connection = sqlite3.connect(
            f"file:{encoded_path}?mode=ro",
            uri=True,
            timeout=self.timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_PAGE_SIZE
        ):
            raise ValueError(f"limit must be an integer from 1 to {_MAX_PAGE_SIZE}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            raw_criteria = json.loads(row["criteria_json"])
            criteria = [Criterion(**item).to_dict() for item in raw_criteria]
            status = TaskStatus(row["status"]).value
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("task record contains invalid structured data") from exc

        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "status": status,
            "created_at": row["created_at"],
            "criteria": criteria,
        }

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"evidence record {row['evidence_id']} contains invalid JSON"
            ) from exc
        return {
            "sequence": row["sequence"],
            "evidence_id": row["evidence_id"],
            "kind": row["kind"],
            "source": row["source"],
            "observed_at": row["observed_at"],
            "payload": payload,
        }

    @staticmethod
    def _verification_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            report = json.loads(row["report_json"])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"verification record {row['verification_id']} contains invalid JSON"
            ) from exc
        return {
            "verification_id": row["verification_id"],
            "status": row["status"],
            "verified_at": row["verified_at"],
            "evidence_sequence": row["evidence_sequence"],
            "report": report,
        }

    @staticmethod
    def _integrity_from_rows(rows: list[sqlite3.Row]) -> bool:
        previous_hash = _GENESIS_HASH
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
            if (
                row["previous_hash"] != previous_hash
                or row["record_hash"] != _hash_record(record)
            ):
                return False
            previous_hash = row["record_hash"]
        return True

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Return a task and its immutable success criteria."""
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            return self._task_from_row(row)
        finally:
            connection.close()

    def list_evidence(
        self,
        task_id: str,
        *,
        limit: int = _DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        """Return one bounded page of evidence for a task, oldest first."""
        self._validate_page(limit, offset)
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            exists = connection.execute(
                "SELECT 1 FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(f"unknown task: {task_id}")
            rows = connection.execute(
                """
                SELECT sequence, evidence_id, kind, source, observed_at, payload_json
                FROM evidence
                WHERE task_id=?
                ORDER BY sequence
                LIMIT ? OFFSET ?
                """,
                (task_id, limit, offset),
            ).fetchall()
            return tuple(self._evidence_from_row(row) for row in rows)
        finally:
            connection.close()

    def get_latest_verification(self, task_id: str) -> dict[str, Any] | None:
        """Return the newest stored verification without recalculating or writing."""
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            exists = connection.execute(
                "SELECT 1 FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(f"unknown task: {task_id}")
            row = connection.execute(
                """
                SELECT verification_id, status, report_json, verified_at, evidence_sequence
                FROM verifications
                WHERE task_id=?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            return self._verification_from_row(row)
        finally:
            connection.close()

    def verify_integrity(self) -> bool:
        """Check the complete retained evidence chain in a read-only snapshot."""
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT * FROM evidence ORDER BY sequence"
            ).fetchall()
            return self._integrity_from_rows(rows)
        finally:
            connection.close()

    def get_task_report(
        self,
        task_id: str,
        *,
        evidence_limit: int = _DEFAULT_PAGE_SIZE,
        evidence_offset: int = 0,
    ) -> dict[str, Any]:
        """Return one consistent, read-only task/evidence/verification snapshot.

        A stored terminal success is not presented as effective success if its
        verification is missing, stale against newer evidence, or the retained
        hash chain is invalid. In those cases effective_status is "unknown".
        Evidence payloads are untrusted observations, not executable commands
        or instructions.
        """
        self._validate_page(evidence_limit, evidence_offset)
        connection = self._connect()
        try:
            connection.execute("BEGIN")

            task_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"unknown task: {task_id}")
            task = self._task_from_row(task_row)

            count_row = connection.execute(
                """
                SELECT COUNT(*) AS evidence_count, COALESCE(MAX(sequence), 0) AS latest_sequence
                FROM evidence
                WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
            evidence_total = int(count_row["evidence_count"])
            latest_sequence = int(count_row["latest_sequence"])

            evidence_rows = connection.execute(
                """
                SELECT sequence, evidence_id, kind, source, observed_at, payload_json
                FROM evidence
                WHERE task_id=?
                ORDER BY sequence
                LIMIT ? OFFSET ?
                """,
                (task_id, evidence_limit, evidence_offset),
            ).fetchall()

            verification_row = connection.execute(
                """
                SELECT verification_id, status, report_json, verified_at, evidence_sequence
                FROM verifications
                WHERE task_id=?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()

            all_evidence_rows = connection.execute(
                "SELECT * FROM evidence ORDER BY sequence"
            ).fetchall()

            evidence = [self._evidence_from_row(row) for row in evidence_rows]
            verification = self._verification_from_row(verification_row)
            ledger_integrity = self._integrity_from_rows(all_evidence_rows)

            verification_current = (
                verification is not None
                and verification["evidence_sequence"] == latest_sequence
            )

            stored_status = task["status"]
            if not ledger_integrity:
                effective_status = TaskStatus.UNKNOWN.value
                reliability_note = "evidence_hash_chain_invalid"
            elif stored_status in {TaskStatus.PASSED.value, TaskStatus.FAILED.value}:
                if verification_current:
                    effective_status = stored_status
                    reliability_note = None
                else:
                    effective_status = TaskStatus.UNKNOWN.value
                    reliability_note = "terminal_status_has_no_current_verification"
            elif verification is not None and not verification_current:
                effective_status = TaskStatus.UNKNOWN.value
                reliability_note = "verification_is_stale"
            else:
                effective_status = stored_status
                reliability_note = None

            return {
                "task": task,
                "effective_status": effective_status,
                "reliability_note": reliability_note,
                "ledger_integrity": ledger_integrity,
                "verification_current": verification_current,
                "latest_evidence_sequence": latest_sequence,
                "evidence_total": evidence_total,
                "evidence_offset": evidence_offset,
                "evidence_limit": evidence_limit,
                "evidence_truncated": evidence_offset + len(evidence) < evidence_total,
                "evidence": evidence,
                "verification": verification,
            }
        finally:
            connection.close()
