"""Bounded, host-side verification workflow for coding tasks only.

The workflow never treats an executor's return value or model prose as proof.
It creates a fresh immutable Evidence Engine task for each attempt, runs a
host-controlled callback, finalizes the task, and releases completion only
when the independent read-only report passes every gate check.

The callback is trusted host orchestration. It must not give the language
model a writer, database connection, unrestricted command runner, or policy
configuration. Tool output and observed values remain untrusted data.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .engine import Criterion, EvidenceEngine, TaskStatus
from .reader import EvidenceReader

_MAX_ATTEMPTS = 5
_MAX_EVIDENCE_PAGE = 500
_WORKFLOW_SOURCE = "evidence_engine.coding_workflow"
_NON_RETRYABLE_GATE_REASONS = frozenset({
    "invalid_report",
    "task_record_missing",
    "task_id_missing",
    "success_criteria_missing",
    "ledger_integrity",
    "verification_not_current",
    "verification_record_invalid",
    "verification_record_inconsistent",
    "verification_sequence_mismatch",
    "evidence_truncated",
    "evidence_snapshot_incomplete",
    "evidence_identifiers_invalid",
    "verification_missing",
    "verification_report_missing",
    "verification_task_mismatch",
    "criterion_check_count_mismatch",
})


@dataclass(frozen=True, slots=True)
class AttemptContext:
    """Safe identifiers and limits for one host-managed coding attempt."""

    workflow_id: str
    task_id: str
    attempt_number: int
    max_attempts: int
    previous_task_id: str | None


@dataclass(frozen=True, slots=True)
class CriterionFeedback:
    """One deterministic criterion result; observed values are untrusted data."""

    index: int
    status: str
    message: str
    evidence_ids: tuple[str, ...]
    observed_values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class AttemptFeedback:
    """Verification feedback passed to the next host-controlled attempt."""

    previous_task_id: str
    previous_status: str
    reasons: tuple[str, ...]
    checks: tuple[CriterionFeedback, ...]


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether a read-only verification snapshot may release completion."""

    released: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodingAttemptResult:
    attempt_number: int
    task_id: str
    task_status: TaskStatus
    decision: GateDecision
    report: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CodingWorkflowResult:
    completed: bool
    workflow_id: str
    attempts: tuple[CodingAttemptResult, ...]
    final_task_id: str
    final_report: dict[str, Any]
    stop_reason: str


class CodingVerificationGate:
    """Fail-closed release gate over EvidenceReader task reports."""

    def evaluate(self, report: dict[str, Any]) -> GateDecision:
        reasons: list[str] = []
        if not isinstance(report, dict):
            return GateDecision(False, ("invalid_report",))

        task = report.get("task")
        if not isinstance(task, dict):
            return GateDecision(False, ("task_record_missing",))

        task_id = task.get("task_id")
        criteria = task.get("criteria")
        if not isinstance(task_id, str) or not task_id:
            reasons.append("task_id_missing")
        if not isinstance(criteria, list) or not criteria:
            reasons.append("success_criteria_missing")
            criteria = []

        if task.get("status") != TaskStatus.PASSED.value:
            reasons.append("task_status_not_passed")
        if report.get("effective_status") != TaskStatus.PASSED.value:
            reasons.append("effective_status_not_passed")
        if report.get("ledger_integrity") is not True:
            reasons.append("ledger_integrity")
        if report.get("verification_current") is not True:
            reasons.append("verification_not_current")
        if report.get("verification_record_valid") is not True:
            reasons.append("verification_record_invalid")
        if report.get("evidence_truncated") is not False:
            reasons.append("evidence_truncated")

        evidence = report.get("evidence")
        evidence_total = report.get("evidence_total")
        if (
            not isinstance(evidence, list)
            or isinstance(evidence_total, bool)
            or not isinstance(evidence_total, int)
            or evidence_total != len(evidence)
        ):
            reasons.append("evidence_snapshot_incomplete")
            evidence = []

        evidence_ids = {
            item.get("evidence_id")
            for item in evidence
            if isinstance(item, dict)
            and isinstance(item.get("evidence_id"), str)
            and item.get("evidence_id")
        }
        if len(evidence_ids) != len(evidence):
            reasons.append("evidence_identifiers_invalid")

        verification = report.get("verification")
        if not isinstance(verification, dict):
            reasons.append("verification_missing")
        else:
            if verification.get("status") != "supported":
                reasons.append("verification_not_supported")
            if verification.get("record_consistent") is not True:
                reasons.append("verification_record_inconsistent")
            if verification.get("evidence_sequence") != report.get(
                "latest_evidence_sequence"
            ):
                reasons.append("verification_sequence_mismatch")

            verification_report = verification.get("report")
            if not isinstance(verification_report, dict):
                reasons.append("verification_report_missing")
            else:
                if verification_report.get("task_id") != task_id:
                    reasons.append("verification_task_mismatch")
                if verification_report.get("status") != "supported":
                    reasons.append("verification_report_not_supported")
                checks = verification_report.get("checks")
                if not isinstance(checks, list) or len(checks) != len(criteria):
                    reasons.append("criterion_check_count_mismatch")
                    checks = []

                if len(checks) == len(criteria):
                    for position, (criterion, check) in enumerate(
                        zip(criteria, checks), start=1
                    ):
                        if not isinstance(criterion, dict) or not isinstance(check, dict):
                            reasons.append(f"criterion_{position}_record_invalid")
                            continue
                        if check.get("index") != position:
                            reasons.append(f"criterion_{position}_index_mismatch")
                        if check.get("status") != "supported":
                            reasons.append(f"criterion_{position}_not_supported")

                        minimum = criterion.get("min_evidence", 1)
                        check_ids = check.get("evidence_ids")
                        valid_check_ids = (
                            isinstance(check_ids, list)
                            and all(
                                isinstance(item, str) and item in evidence_ids
                                for item in check_ids
                            )
                        )
                        if (
                            isinstance(minimum, bool)
                            or not isinstance(minimum, int)
                            or minimum < 1
                            or not valid_check_ids
                            or len(check_ids) < minimum
                            or len(set(check_ids)) < minimum
                        ):
                            reasons.append(f"criterion_{position}_evidence_missing")

        # Preserve first occurrence while keeping the decision deterministic.
        unique_reasons = tuple(dict.fromkeys(reasons))
        return GateDecision(not unique_reasons, unique_reasons)


class CodingWorkflow:
    """Run a coding-only workflow with fresh task records and bounded retries.

    The callback receives only an attempt context and deterministic feedback.
    It is invoked by trusted host code, not registered directly as an LLM tool.
    Any command/file adapters and model interaction remain host-controlled.
    """

    def __init__(
        self,
        engine: EvidenceEngine,
        reader: EvidenceReader,
        *,
        max_attempts: int = 5,
        evidence_limit: int = 500,
    ) -> None:
        if not isinstance(engine, EvidenceEngine):
            raise TypeError("engine must be an EvidenceEngine")
        if not isinstance(reader, EvidenceReader):
            raise TypeError("reader must be an EvidenceReader")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= _MAX_ATTEMPTS
        ):
            raise ValueError(f"max_attempts must be an integer from 1 to {_MAX_ATTEMPTS}")
        if (
            isinstance(evidence_limit, bool)
            or not isinstance(evidence_limit, int)
            or not 1 <= evidence_limit <= _MAX_EVIDENCE_PAGE
        ):
            raise ValueError(
                f"evidence_limit must be an integer from 1 to {_MAX_EVIDENCE_PAGE}"
            )
        if str(reader.db_path) != str(Path(engine.db_path).expanduser().resolve()):
            raise ValueError("engine and reader must point to the same evidence database")

        self.engine = engine
        self.reader = reader
        self.max_attempts = max_attempts
        self.evidence_limit = evidence_limit
        self.gate = CodingVerificationGate()

    @staticmethod
    def _make_feedback(
        task_id: str,
        report: dict[str, Any],
        decision: GateDecision,
    ) -> AttemptFeedback:
        task = report.get("task", {})
        verification = report.get("verification")
        verification_report = (
            verification.get("report")
            if isinstance(verification, dict)
            else None
        )
        raw_checks = (
            verification_report.get("checks")
            if isinstance(verification_report, dict)
            else []
        )
        checks: list[CriterionFeedback] = []
        if isinstance(raw_checks, list):
            for raw in raw_checks:
                if not isinstance(raw, dict):
                    continue
                checks.append(
                    CriterionFeedback(
                        index=raw.get("index", 0),
                        status=str(raw.get("status", "unknown")),
                        message=str(raw.get("message", "No deterministic check detail")),
                        evidence_ids=tuple(
                            item for item in raw.get("evidence_ids", [])
                            if isinstance(item, str)
                        ) if isinstance(raw.get("evidence_ids", []), list) else (),
                        observed_values=tuple(raw.get("observed_values", []))
                        if isinstance(raw.get("observed_values", []), list)
                        else (),
                    )
                )
        return AttemptFeedback(
            previous_task_id=task_id,
            previous_status=str(report.get("effective_status", task.get("status", "unknown"))),
            reasons=decision.reasons,
            checks=tuple(checks),
        )

    def run(
        self,
        title: str,
        criteria: Iterable[Criterion],
        execute_attempt: Callable[
            [AttemptContext, AttemptFeedback | None], Any
        ],
    ) -> CodingWorkflowResult:
        """Execute, verify, and retry; callback return values are ignored.

        Every attempt gets a new task because FAILED tasks are terminal in the
        current Evidence Engine state machine. Criteria are recreated unchanged.
        """
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        if not callable(execute_attempt):
            raise TypeError("execute_attempt must be callable")
        fixed_criteria = tuple(criteria)
        if not fixed_criteria or not all(
            isinstance(item, Criterion) for item in fixed_criteria
        ):
            raise ValueError("criteria must contain at least one Criterion")

        workflow_id = uuid.uuid4().hex
        previous_task_id: str | None = None
        feedback: AttemptFeedback | None = None
        attempts: list[CodingAttemptResult] = []

        for attempt_number in range(1, self.max_attempts + 1):
            attempt_title = (
                f"{title.strip()} [coding attempt {attempt_number}/{self.max_attempts}]"
            )
            task = self.engine.create_task(attempt_title, fixed_criteria)
            self.engine.start_task(task.task_id)
            self.engine.record_evidence(
                task.task_id,
                kind="coding_attempt_started",
                source=_WORKFLOW_SOURCE,
                payload={
                    "workflow_id": workflow_id,
                    "attempt_number": attempt_number,
                    "max_attempts": self.max_attempts,
                    "previous_task_id": previous_task_id,
                },
            )
            context = AttemptContext(
                workflow_id=workflow_id,
                task_id=task.task_id,
                attempt_number=attempt_number,
                max_attempts=self.max_attempts,
                previous_task_id=previous_task_id,
            )

            try:
                # The return value is intentionally ignored. Only recorded,
                # trusted observations can satisfy immutable success criteria.
                execute_attempt(context, feedback)
            except Exception as exc:
                self.engine.record_evidence(
                    task.task_id,
                    kind="coding_attempt_error",
                    source=_WORKFLOW_SOURCE,
                    payload={
                        "workflow_id": workflow_id,
                        "attempt_number": attempt_number,
                        "error_type": type(exc).__name__,
                    },
                )

            final_task = self.engine.finalize_task(task.task_id)
            report = self.reader.get_task_report(
                task.task_id,
                evidence_limit=self.evidence_limit,
                evidence_offset=0,
            )
            decision = self.gate.evaluate(report)
            attempt_result = CodingAttemptResult(
                attempt_number=attempt_number,
                task_id=task.task_id,
                task_status=final_task.status,
                decision=decision,
                report=report,
            )
            attempts.append(attempt_result)

            if decision.released:
                return CodingWorkflowResult(
                    completed=True,
                    workflow_id=workflow_id,
                    attempts=tuple(attempts),
                    final_task_id=task.task_id,
                    final_report=report,
                    stop_reason="verified",
                )

            if set(decision.reasons) & _NON_RETRYABLE_GATE_REASONS:
                return CodingWorkflowResult(
                    completed=False,
                    workflow_id=workflow_id,
                    attempts=tuple(attempts),
                    final_task_id=task.task_id,
                    final_report=report,
                    stop_reason="verification_integrity_failure",
                )

            previous_task_id = task.task_id
            feedback = self._make_feedback(task.task_id, report, decision)

        last = attempts[-1]
        return CodingWorkflowResult(
            completed=False,
            workflow_id=workflow_id,
            attempts=tuple(attempts),
            final_task_id=last.task_id,
            final_report=last.report,
            stop_reason="attempt_limit_reached",
        )
