"""Read-only query facade for assistant-facing evidence access."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import quote
from typing import Any

from .engine import (
    Criterion,
    Task,
    TaskStatus,
    VerificationStatus,
    _hash_record,
)


class EvidenceReader:
    """A read-only view over an existing Evidence Engine SQLite database.

    The class intentionally has no task creation, evidence-writing, or
    finalization methods. A host integration should expose only these query
    methods to an assistant and keep the writer/adapter objects host-side.
    """

    def __init__(self, db_path: str | Path):
        try:
            resolved = Path(db_path).expanduser().resolve(strict=True)
        except (OSError, TypeError) as exc:
            raise FileNotFoundError(f"evidence database is not available: {db_path}") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"evidence database is not a regular file: {resolved}")
        self.db_path = resolved

    def _connect(self) -> sqlite3.Connection:
        encoded_path = quote(self.db_path.as_posix(), safe="/")
        connection = sqlite3.connect(
            f"file:{encoded_path}?mode=ro",
            uri=True,
            timeout=3.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def get_task(self, task_id: str) -> Task:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")

        criteria = tuple(
            Criterion(**item) for item in json.loads(row["criteria_json"])
        )
        return Task(
            task_id=row["task_id"],
            title=row["title"],
            status=TaskStatus(row["status"]),
            criteria=criteria,
            created_at=row["created_at"],
        )

    def list_evidence(self, task_id: str) -> tuple[dict[str, Any], ...]:
        self.get_task(task_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM evidence WHERE task_id=? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        finally:
            connection.close()

        return tuple(
            {
                "sequence": row["sequence"],
                "evidence_id": row["evidence_id"],
                "kind": row["kind"],
                "source": row["source"],
                "observed_at": row["observed_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        )

    def get_latest_verification(self, task_id: str) -> dict[str, Any] | None:
        self.get_task(task_id)
        connection = self._connect()
        try:
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
        finally:
            connection.close()

        if row is None:
            return None
        return {
            "verification_id": row["verification_id"],
            "status": VerificationStatus(row["status"]),
            "report": json.loads(row["report_json"]),
            "verified_at": row["verified_at"],
            "evidence_sequence": row["evidence_sequence"],
        }

    def verify_integrity(self) -> bool:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM evidence ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()

        previous_hash = "0" * 64
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
