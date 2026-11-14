"""Bounded, allowlisted JUnit XML report inspection.

The adapter recomputes test counts from testcase elements instead of trusting
summary attributes. It stores only a digest and structured summary, never the
raw XML. Missing or unreadable reports produce attempt evidence but no
junit_report evidence, so task verification remains inconclusive.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..engine import EvidenceEngine, TaskStatus
from .file import FilePolicy


_SOURCE = "evidence_engine.junit_report_adapter"
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
    evidence_id: str


def _local_name(tag: str) -> str:
    """Return an XML element's local name, ignoring an optional namespace."""
    return tag.rsplit("}", 1)[-1].lower()


def _declared_counts(element: ET.Element) -> dict[str, int]:
    """Parse only declared count attributes; malformed values are rejected."""
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
    """Compute outcomes from testcase child elements, not root summary values."""
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


def _metadata_is_consistent(root: ET.Element, totals: dict[str, int]) -> tuple[bool, dict[str, Any]]:
    """Compare each declared suite summary with counts recomputed from its cases."""
    suite_elements = [
        element for element in root.iter()
        if _local_name(element.tag) == "testsuite"
    ]
    if _local_name(root.tag) == "testsuite" and root not in suite_elements:
        suite_elements.insert(0, root)

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

    # Some JUnit producers put aggregate counts on a <testsuites> root.
    if _local_name(root.tag) == "testsuites":
        root_declared = _declared_counts(root)
        if root_declared:
            declared_summaries.append(root_declared)
            for field, value in root_declared.items():
                if totals[field] != value:
                    consistent = False

    return consistent, {"suites": declared_summaries}


class JUnitReportAdapter:
    """Inspect JUnit XML within an operator-allowlisted file root."""

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
        *,
        reason: str,
        size_bytes: int | None = None,
        sha256: str | None = None,
        stable_read: bool | None = None,
    ) -> JUnitReportResult:
        payload = {
            "relative_path": relative_path,
            "available": False,
            "reason": reason,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "stable_read": stable_read,
            "content_stored": False,
        }
        evidence_id = engine.record_evidence(
            task_id,
            kind="junit_report_attempt",
            source=_SOURCE,
            payload=payload,
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
            evidence_id=evidence_id,
        )

    def _record_parsed(
        self,
        engine: EvidenceEngine,
        task_id: str,
        relative_path: str,
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
            evidence_id=evidence_id,
        )

    def inspect(
        self,
        engine: EvidenceEngine,
        task_id: str,
        relative_path: str,
    ) -> JUnitReportResult:
        """Read and verify one bounded JUnit XML report, then append its summary.

        A path must be relative to one of FilePolicy.allowed_roots. The report
        bytes are held only in bounded process memory while parsed; raw XML is
        never persisted to the Evidence Engine.
        """
        if not isinstance(engine, EvidenceEngine):
            raise TypeError("engine must be an EvidenceEngine")
        task = engine.get_task(task_id)
        if task.status is not TaskStatus.RUNNING:
            raise ValueError("JUnit reports can only be inspected for a running task")
        root, resolved = self.policy.resolve(relative_path)

        try:
            path_stat = resolved.stat()
        except FileNotFoundError:
            return self._record_attempt(
                engine, task_id, relative_path, reason="report file does not exist"
            )
        except OSError as exc:
            return self._record_attempt(
                engine, task_id, relative_path, reason=f"{type(exc).__name__}: {exc}"
            )

        if not stat.S_ISREG(path_stat.st_mode):
            return self._record_attempt(
                engine, task_id, relative_path, reason="report path is not a regular file"
            )
        if path_stat.st_size > self.max_report_bytes:
            return self._record_attempt(
                engine, task_id, relative_path,
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
                        engine, task_id, relative_path,
                        reason="opened report is not a regular file",
                    )
                if before.st_size > self.max_report_bytes:
                    return self._record_attempt(
                        engine, task_id, relative_path,
                        reason="report exceeds configured size limit",
                        size_bytes=before.st_size,
                        stable_read=False,
                    )
                data = handle.read(self.max_report_bytes + 1)
                after = os.fstat(handle.fileno())
            after_path = resolved.stat()
        except FileNotFoundError:
            return self._record_attempt(
                engine, task_id, relative_path,
                reason="report disappeared while opening",
                stable_read=False,
            )
        except OSError as exc:
            return self._record_attempt(
                engine, task_id, relative_path,
                reason=f"{type(exc).__name__}: {exc}",
                stable_read=False,
            )

        if len(data) > self.max_report_bytes:
            return self._record_attempt(
                engine, task_id, relative_path,
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
                engine, task_id, relative_path,
                reason="report changed during inspection",
                size_bytes=len(data),
                stable_read=False,
            )

        digest = hashlib.sha256(data).hexdigest()
        if _DTD_OR_ENTITY.search(data):
            return self._record_parsed(
                engine, task_id, relative_path,
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
                engine, task_id, relative_path,
                data=data, digest=digest, valid=False, totals=None,
                metadata_consistent=False, declared_counts=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        return self._record_parsed(
            engine, task_id, relative_path,
            data=data, digest=digest, valid=True, totals=totals,
            metadata_consistent=metadata_consistent,
            declared_counts=declared, error=None,
        )
