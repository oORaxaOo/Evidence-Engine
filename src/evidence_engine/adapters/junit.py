"""Bounded JUnit XML inspection tied to an actual allowlisted test command.

The adapter never treats a standalone XML file as proof of a test run. It
requires a command_result recorded by the trusted CommandAdapter, checks that
the command declared this exact JUnit output path, and rejects stale report
files. It stores only a structured summary and digest, never raw XML.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..engine import EvidenceEngine, TaskStatus
from .file import FilePolicy


_SOURCE = "evidence_engine.junit_report_adapter"
_COMMAND_SOURCE_PREFIX = "evidence_engine.command_adapter:"
_DTD_OR_ENTITY = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_COUNT_FIELDS = ("tests", "failures", "errors", "skipped")


@dataclass(frozen=True, slots=True)
class JUnitReportResult:
    """Summary of one JUnit report inspection."""

    relative_path: str
    available: bool
    report_valid: bool | None
    metadata_consistent: bool | None
    tests: int | None
    passed: int | None
    failures: int | None
    errors: int | None
    skipped: int | None
    size_bytes: int | None
    sha256: str | None
    stable_read: bool | None
    inspection_error: str | None
    command_evidence_id: str
    evidence_id: str


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("command evidence has no valid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("command evidence has an invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("command evidence timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _command_report_path(argv: object, cwd: object) -> Path:
    """Resolve the single fixed --junitxml output path in command evidence."""
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise ValueError("command evidence has no valid argument vector")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("command evidence has no valid working directory")

    command_cwd = Path(cwd).expanduser()
    if not command_cwd.is_absolute():
        raise ValueError("command evidence working directory must be absolute")

    targets: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument.startswith("--junitxml="):
            targets.append(argument.split("=", 1)[1])
        elif argument == "--junitxml":
            if index + 1 >= len(argv):
                raise ValueError("--junitxml option has no output path")
            index += 1
            targets.append(argv[index])
        index += 1

    if len(targets) != 1 or not targets[0]:
        raise ValueError("command must declare exactly one --junitxml output path")

    target = Path(targets[0]).expanduser()
    if not target.is_absolute():
        target = command_cwd / target
    return target.resolve(strict=False)


def _declared_counts(element: ET.Element) -> dict[str, int]:
    declared: dict[str, int] = {}
    for field in _COUNT_FIELDS:
        raw = element.attrib.get(field)
        if raw is None:
            continue
        try:
            value = int(raw, 10)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"JUnit {field} attribute is not an integer") from exc
        if value < 0:
            raise ValueError(f"JUnit {field} attribute must not be negative")
        declared[field] = value
    return declared


def _count_testcases(root: ET.Element) -> dict[str, int]:
    totals = {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0}
    for testcase in root.iter():
        if _local_name(testcase.tag) != "testcase":
            continue
        totals["tests"] += 1
        outcomes = {_local_name(child.tag) for child in list(testcase)}
        if "error" in outcomes:
            totals["errors"] += 1
        elif "failure" in outcomes:
            totals["failures"] += 1
        elif "skipped" in outcomes:
            totals["skipped"] += 1
        else:
            totals["passed"] += 1
    return totals


def _metadata_is_consistent(
    root: ET.Element,
    totals: dict[str, int],
) -> tuple[bool, dict[str, Any]]:
    """Compare declared root and suite counts with recomputed testcase counts."""
    suite_elements = [
        element for element in root.iter()
        if _local_name(element.tag) == "testsuite"
    ]
    declared_summaries: list[dict[str, int]] = []
    consistent = True

    for suite in suite_elements:
        declared = _declared_counts(suite)
        if not declared:
            continue
        suite_totals = _count_testcases(suite)
        declared_summaries.append(declared)
        for field, value in declared.items():
            if suite_totals[field] != value:
                consistent = False

    if _local_name(root.tag) == "testsuites":
        root_declared = _declared_counts(root)
        if root_declared:
            declared_summaries.append(root_declared)
            for field, value in root_declared.items():
                if totals[field] != value:
                    consistent = False

    return consistent, {"suites": declared_summaries}


class JUnitReportAdapter:
    """Inspect JUnit XML only when linked to trusted command execution evidence."""

    def __init__(self, policy: FilePolicy):
        if not isinstance(policy, FilePolicy):
            raise TypeError("policy must be a FilePolicy")
        self.policy = policy
        self.max_report_bytes = policy.max_file_size_bytes

    def _record_attempt(
        self,
        engine: EvidenceEngine,
        task_id: str,
        relative_path: str,
        command_evidence_id: str,
        *,
        reason: str,
        size_bytes: int | None = None,
        sha256: str | None = None,
        stable_read: bool | None = None,
    ) -> JUnitReportResult:
        evidence_id = engine.record_evidence(
            task_id,
            kind="junit_report_attempt",
            source=_SOURCE,
            payload={
                "relative_path": relative_path,
                "command_evidence_id": command_evidence_id,
                "available": False,
                "reason": reason,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "stable_read": stable_read,
                "content_stored": False,
            },
        )
        return JUnitReportResult(
            relative_path=relative_path,
            available=False,
            report_valid=None,
            metadata_consistent=None,
            tests=None,
            passed=None,
            failures=None,
            errors=None,
            skipped=None,
            size_bytes=size_bytes,
            sha256=sha256,
            stable_read=stable_read,
            inspection_error=reason,
            command_evidence_id=command_evidence_id,
            evidence_id=evidence_id,
        )

    def _record_parsed(
        self,
        engine: EvidenceEngine,
        task_id: str,
        relative_path: str,
        command_evidence_id: str,
        command_payload: dict[str, Any],
        *,
        data: bytes,
        digest: str,
        valid: bool,
        totals: dict[str, int] | None,
        metadata_consistent: bool | None,
        declared_counts: dict[str, Any] | None,
        error: str | None,
    ) -> JUnitReportResult:
        payload = {
            "relative_path": relative_path,
            "command_evidence_id": command_evidence_id,
            "command_request_id": command_payload.get("request_id"),
            "command_id": command_payload.get("command_id"),
            "command_return_code": command_payload.get("return_code"),
            "command_started_at": command_payload.get("started_at"),
            "command_finished_at": command_payload.get("finished_at"),
            "report_valid": valid,
            "metadata_consistent": metadata_consistent,
            "parse_status": "valid" if valid else "invalid",
            "tests": totals["tests"] if totals else None,
            "passed": totals["passed"] if totals else None,
            "failures": totals["failures"] if totals else None,
            "errors": totals["errors"] if totals else None,
            "skipped": totals["skipped"] if totals else None,
            "declared_counts": declared_counts,
            "size_bytes": len(data),
            "sha256": digest,
            "stable_read": True,
            "inspection_error": error,
            "content_stored": False,
        }
        evidence_id = engine.record_evidence(
            task_id,
            kind="junit_report",
            source=_SOURCE,
            payload=payload,
        )
        return JUnitReportResult(
            relative_path=relative_path,
            available=True,
            report_valid=valid,
            metadata_consistent=metadata_consistent,
            tests=totals["tests"] if totals else None,
            passed=totals["passed"] if totals else None,
            failures=totals["failures"] if totals else None,
            errors=totals["errors"] if totals else None,
            skipped=totals["skipped"] if totals else None,
            size_bytes=len(data),
            sha256=digest,
            stable_read=True,
            inspection_error=error,
            command_evidence_id=command_evidence_id,
            evidence_id=evidence_id,
        )

    def inspect(
        self,
        engine: EvidenceEngine,
        task_id: str,
        relative_path: str,
        *,
        command_evidence_id: str,
    ) -> JUnitReportResult:
        """Inspect a bounded report belonging to one recorded command execution.

        The command evidence must come from CommandAdapter, must have started,
        must not have timed out or exceeded the output cap, and must declare
        the exact report path being inspected. A stale or missing report is
        recorded as an attempt only, leaving report criteria inconclusive.
        """
        if not isinstance(engine, EvidenceEngine):
            raise TypeError("engine must be an EvidenceEngine")
        task = engine.get_task(task_id)
        if task.status is not TaskStatus.RUNNING:
            raise ValueError("JUnit reports can only be inspected for a running task")
        if not isinstance(command_evidence_id, str) or not command_evidence_id:
            raise ValueError("command_evidence_id is required")

        command_record = next(
            (
                item for item in engine.list_evidence(task_id)
                if item["evidence_id"] == command_evidence_id
                and item["kind"] == "command_result"
                and item["source"].startswith(_COMMAND_SOURCE_PREFIX)
            ),
            None,
        )
        if command_record is None:
            raise ValueError(
                "command_evidence_id must identify trusted CommandAdapter evidence for this task"
            )

        command_payload = command_record["payload"]
        if command_payload.get("process_started") is not True:
            raise ValueError("the recorded command did not start")
        if command_payload.get("timed_out") is True:
            raise ValueError("timed-out commands cannot establish report provenance")
        if command_payload.get("output_limit_exceeded") is True:
            raise ValueError("commands exceeding the output limit cannot establish report provenance")

        command_started = _parse_timestamp(command_payload.get("started_at"))
        command_finished = _parse_timestamp(command_payload.get("finished_at"))
        if command_finished < command_started:
            raise ValueError("command evidence has an invalid execution time interval")
        expected_report_path = _command_report_path(
            command_payload.get("argv"),
            command_payload.get("cwd"),
        )
        root, resolved = self.policy.resolve(relative_path)
        if expected_report_path != resolved:
            raise ValueError("report path does not match the command's --junitxml output path")

        if command_payload.get("return_code") is None:
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason="command has no final return code",
            )

        try:
            path_stat = resolved.stat()
        except FileNotFoundError:
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason="report file does not exist",
            )
        except OSError as exc:
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason=f"{type(exc).__name__}: {exc}",
            )

        if not stat.S_ISREG(path_stat.st_mode):
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason="report path is not a regular file",
            )
        if path_stat.st_size > self.max_report_bytes:
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason="report exceeds configured size limit",
                size_bytes=path_stat.st_size,
                stable_read=False,
            )

        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        try:
            descriptor = os.open(str(resolved), flags)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    return self._record_attempt(
                        engine, task_id, relative_path, command_evidence_id,
                        reason="opened report is not a regular file",
                    )
                if before.st_size > self.max_report_bytes:
                    return self._record_attempt(
                        engine, task_id, relative_path, command_evidence_id,
                        reason="report exceeds configured size limit",
                        size_bytes=before.st_size,
                        stable_read=False,
                    )
                data = handle.read(self.max_report_bytes + 1)
                after = os.fstat(handle.fileno())
            after_path = resolved.stat()
        except FileNotFoundError:
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason="report disappeared while opening",
                stable_read=False,
            )
        except OSError as exc:
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason=f"{type(exc).__name__}: {exc}",
                stable_read=False,
            )

        if len(data) > self.max_report_bytes:
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason="report exceeds configured size limit",
                size_bytes=len(data),
                stable_read=False,
            )

        identity = lambda item: (
            item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns
        )
        stable = (
            identity(before) == identity(after)
            and identity(after) == identity(after_path)
            and len(data) == after.st_size
            and resolved.is_relative_to(root)
        )
        if not stable:
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason="report changed during inspection",
                size_bytes=len(data),
                stable_read=False,
            )

        digest = hashlib.sha256(data).hexdigest()
        report_modified = datetime.fromtimestamp(
            after.st_mtime_ns / 1_000_000_000,
            tz=timezone.utc,
        )
        if report_modified < command_started or report_modified > command_finished:
            reason = (
                "report predates the command execution"
                if report_modified < command_started
                else "report was modified after the command completed"
            )
            return self._record_attempt(
                engine, task_id, relative_path, command_evidence_id,
                reason=reason,
                size_bytes=len(data),
                sha256=digest,
                stable_read=True,
            )

        if _DTD_OR_ENTITY.search(data):
            return self._record_parsed(
                engine, task_id, relative_path, command_evidence_id, command_payload,
                data=data, digest=digest, valid=False, totals=None,
                metadata_consistent=False, declared_counts=None,
                error="DOCTYPE and ENTITY declarations are not allowed",
            )

        try:
            document = ET.fromstring(data)
            root_name = _local_name(document.tag)
            if root_name not in {"testsuite", "testsuites"}:
                raise ValueError("XML root must be <testsuite> or <testsuites>")
            totals = _count_testcases(document)
            metadata_consistent, declared = _metadata_is_consistent(document, totals)
        except (ET.ParseError, ValueError) as exc:
            return self._record_parsed(
                engine, task_id, relative_path, command_evidence_id, command_payload,
                data=data, digest=digest, valid=False, totals=None,
                metadata_consistent=False, declared_counts=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        return self._record_parsed(
            engine, task_id, relative_path, command_evidence_id, command_payload,
            data=data, digest=digest, valid=True, totals=totals,
            metadata_consistent=metadata_consistent,
            declared_counts=declared, error=None,
        )
