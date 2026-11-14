"""Structured JUnit XML report verification bound to a completed command record."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..engine import EvidenceEngine, TaskStatus
from .file import FilePolicy, _stat_identity


_SOURCE = "evidence_engine.junit_adapter"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


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


@dataclass(frozen=True, slots=True)
class JUnitReportSnapshot:
    relative_path: str
    resolved_path: str
    command_evidence_id: str
    report_sha256: str | None
    parse_status: str
    tests_total: int | None
    tests_passed: int | None
    tests_failed: int | None
    tests_errors: int | None
    tests_skipped: int | None
    stable_read: bool | None
    inspection_error: str | None
    evidence_id: str


class JUnitReportAdapter:
    """Parse bounded JUnit XML only when tied to an actual command evidence record.

    A valid XML document alone is not accepted as proof of a test run. The
    command evidence must identify the same --junitxml output path, and the
    report file must have been modified no earlier than that command started.
    """

    def __init__(self, policy: FilePolicy):
        if not isinstance(policy, FilePolicy):
            raise TypeError("policy must be a FilePolicy")
        self.policy = policy

    @staticmethod
    def _junit_output_path(argv: object, cwd: object) -> Path:
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ValueError("command evidence has no valid argv")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("command evidence has no valid working directory")

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
            raise ValueError("registered command must contain exactly one --junitxml output path")
        target = Path(targets[0]).expanduser()
        if not target.is_absolute():
            target = Path(cwd) / target
        return target.resolve(strict=False)

    def inspect(
        self,
        engine: EvidenceEngine,
        task_id: str,
        relative_path: str,
        *,
        command_evidence_id: str,
    ) -> JUnitReportSnapshot:
        task = engine.get_task(task_id)
        if task.status is not TaskStatus.RUNNING:
            raise ValueError("JUnit reports can only be inspected for a running task")
        if not isinstance(command_evidence_id, str) or not command_evidence_id:
            raise ValueError("command_evidence_id is required")

        command_record = next(
            (
                item
                for item in engine.list_evidence(task_id)
                if item["evidence_id"] == command_evidence_id
                and item["kind"] == "command_result"
            ),
            None,
        )
        if command_record is None:
            raise ValueError("command_evidence_id does not identify a command result for this task")

        command = command_record["payload"]
        if command.get("process_started") is not True or command.get("timed_out") is True:
            raise ValueError("command did not complete as a started, non-timeout process")
        if command.get("output_limit_exceeded") is True:
            raise ValueError("command output limit was exceeded; report provenance is inconclusive")
        return_code = command.get("return_code")
        if isinstance(return_code, bool) or not isinstance(return_code, int):
            raise ValueError("command evidence has no final process exit code")

        command_started = _parse_timestamp(command.get("started_at"))
        command_finished = _parse_timestamp(command.get("finished_at"))
        if command_finished < command_started:
            raise ValueError("command evidence has an invalid execution time range")
        expected_output_path = self._junit_output_path(command.get("argv"), command.get("cwd"))
        root, resolved = self.policy.resolve(relative_path)
        if expected_output_path != resolved:
            raise ValueError("report path does not match the command's registered --junitxml output path")

        parse_status = "inconclusive"
        report_hash: str | None = None
        tests_total: int | None = None
        tests_passed: int | None = None
        tests_failed: int | None = None
        tests_errors: int | None = None
        tests_skipped: int | None = None
        stable_read: bool | None = None
        inspection_error: str | None = None

        if not resolved.exists():
            inspection_error = "JUnit report file does not exist"
        else:
            try:
                before_path = resolved.stat()
                if not stat.S_ISREG(before_path.st_mode):
                    inspection_error = "JUnit report path is not a regular file"
                elif before_path.st_size > self.policy.max_file_size_bytes:
                    stable_read = False
                    inspection_error = "JUnit report exceeds configured file size limit"
                else:
                    with resolved.open("rb") as handle:
                        before_fd = os.fstat(handle.fileno())
                        content = handle.read(self.policy.max_file_size_bytes + 1)
                        after_fd = os.fstat(handle.fileno())

                    after_path = resolved.stat()
                    stable_read = (
                        _stat_identity(before_path) == _stat_identity(before_fd)
                        and _stat_identity(before_fd) == _stat_identity(after_fd)
                        and _stat_identity(after_fd) == _stat_identity(after_path)
                        and len(content) == after_fd.st_size
                        and len(content) <= self.policy.max_file_size_bytes
                        and resolved.is_relative_to(root)
                    )
                    if not stable_read:
                        inspection_error = "JUnit report changed during inspection or exceeded its size limit"
                    else:
                        report_hash = hashlib.sha256(content).hexdigest()
                        modified_at = datetime.fromtimestamp(
                            before_path.st_mtime_ns / 1_000_000_000,
                            tz=timezone.utc,
                        )
                        if modified_at < command_started:
                            stable_read = False
                            inspection_error = "JUnit report predates the command execution"
                        elif modified_at > command_finished:
                            stable_read = False
                            inspection_error = "JUnit report was modified after command completion"
                        elif re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", content, re.IGNORECASE):
                            parse_status = "invalid"
                            inspection_error = "DTD/entity declarations are not allowed in JUnit reports"
                        else:
                            try:
                                document = ET.fromstring(content)
                            except ET.ParseError as exc:
                                parse_status = "invalid"
                                inspection_error = f"invalid JUnit XML: {exc}"
                            else:
                                root_name = _local_name(document.tag)
                                if root_name not in {"testsuite", "testsuites"}:
                                    parse_status = "invalid"
                                    inspection_error = "XML root is not testsuite or testsuites"
                                else:
                                    testcases = [
                                        element
                                        for element in document.iter()
                                        if _local_name(element.tag) == "testcase"
                                    ]
                                    if not testcases:
                                        parse_status = "inconclusive"
                                        inspection_error = "JUnit report contains no test cases"
                                    else:
                                        total = len(testcases)
                                        failures = 0
                                        errors = 0
                                        skipped = 0
                                        passed = 0
                                        for testcase in testcases:
                                            child_names = {
                                                _local_name(element.tag)
                                                for element in testcase.iter()
                                                if element is not testcase
                                            }
                                            has_failure = "failure" in child_names
                                            has_error = "error" in child_names
                                            has_skipped = "skipped" in child_names
                                            failures += int(has_failure)
                                            errors += int(has_error)
                                            skipped += int(has_skipped)
                                            passed += int(not (has_failure or has_error or has_skipped))

                                        declared_counts = {
                                            "tests": total,
                                            "failures": failures,
                                            "errors": errors,
                                            "skipped": skipped,
                                        }
                                        mismatches = []
                                        for field, actual in declared_counts.items():
                                            declared = document.attrib.get(field)
                                            if declared is not None:
                                                try:
                                                    declared_value = int(declared)
                                                except ValueError:
                                                    mismatches.append(field)
                                                else:
                                                    if declared_value != actual:
                                                        mismatches.append(field)

                                        tests_total = total
                                        tests_passed = passed
                                        tests_failed = failures
                                        tests_errors = errors
                                        tests_skipped = skipped
                                        if mismatches:
                                            parse_status = "invalid"
                                            inspection_error = (
                                                "JUnit root counts disagree with parsed test cases: "
                                                + ", ".join(mismatches)
                                            )
                                        else:
                                            parse_status = "valid"
            except FileNotFoundError:
                inspection_error = "JUnit report disappeared during inspection"
                stable_read = False
            except OSError as exc:
                inspection_error = f"{type(exc).__name__}: {exc}"
                stable_read = False

        payload = {
            "relative_path": relative_path,
            "resolved_path": str(resolved),
            "root": str(root),
            "command_evidence_id": command_evidence_id,
            "command_request_id": command.get("request_id"),
            "command_return_code": command.get("return_code"),
            "command_started_at": command.get("started_at"),
            "report_sha256": report_hash,
            "parse_status": parse_status,
            "tests_total": tests_total,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "tests_errors": tests_errors,
            "tests_skipped": tests_skipped,
            "stable_read": stable_read,
            "inspection_error": inspection_error,
            "report_content_stored": False,
        }
        evidence_id = engine.record_evidence(
            task_id,
            kind="junit_report",
            source=_SOURCE,
            payload=payload,
        )
        return JUnitReportSnapshot(
            relative_path=relative_path,
            resolved_path=str(resolved),
            command_evidence_id=command_evidence_id,
            report_sha256=report_hash,
            parse_status=parse_status,
            tests_total=tests_total,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            tests_errors=tests_errors,
            tests_skipped=tests_skipped,
            stable_read=stable_read,
            inspection_error=inspection_error,
            evidence_id=evidence_id,
        )
