import tempfile
import unittest
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine, EvidenceReader, TaskStatus
from evidence_engine.coding_workflow import CodingVerificationGate, CodingWorkflow


class CodingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "evidence.sqlite3"
        self.engine = EvidenceEngine(self.database)
        self.reader = EvidenceReader(self.database)
        self.criteria = (
            Criterion(
                kind="build_result",
                field="return_code",
                operator="eq",
                expected=0,
                source="trusted-build",
            ),
            Criterion(
                kind="test_result",
                field="failures",
                operator="eq",
                expected=0,
                source="trusted-test",
            ),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def record_success(self, task_id):
        self.engine.record_evidence(
            task_id,
            kind="build_result",
            source="trusted-build",
            payload={"return_code": 0},
        )
        self.engine.record_evidence(
            task_id,
            kind="test_result",
            source="trusted-test",
            payload={"failures": 0},
        )

    def test_first_attempt_is_released_only_after_all_criteria_are_supported(self):
        calls = []

        def execute(context, feedback):
            calls.append((context, feedback))
            self.record_success(context.task_id)
            return "the model says this is finished"

        result = CodingWorkflow(
            self.engine, self.reader, max_attempts=3
        ).run("Build a tool", self.criteria, execute)

        self.assertTrue(result.completed)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].task_status, TaskStatus.PASSED)
        self.assertTrue(result.attempts[0].decision.released)
        self.assertEqual(calls[0][1], None)
        self.assertEqual(result.final_task_id, calls[0][0].task_id)

    def test_failed_attempt_gets_a_new_task_and_actionable_feedback(self):
        seen = []

        def execute(context, feedback):
            seen.append((context, feedback))
            if context.attempt_number == 1:
                self.engine.record_evidence(
                    context.task_id,
                    kind="build_result",
                    source="trusted-build",
                    payload={"return_code": 1},
                )
                self.engine.record_evidence(
                    context.task_id,
                    kind="test_result",
                    source="trusted-test",
                    payload={"failures": 0},
                )
            else:
                self.record_success(context.task_id)

        result = CodingWorkflow(
            self.engine, self.reader, max_attempts=3
        ).run("Repair a tool", self.criteria, execute)

        self.assertTrue(result.completed)
        self.assertEqual(len(result.attempts), 2)
        self.assertNotEqual(seen[0][0].task_id, seen[1][0].task_id)
        self.assertEqual(seen[1][1].previous_task_id, seen[0][0].task_id)
        self.assertEqual(seen[1][1].previous_status, "failed")
        self.assertTrue(seen[1][1].checks)
        self.assertEqual(
            self.engine.get_task(seen[0][0].task_id).criteria,
            self.engine.get_task(seen[1][0].task_id).criteria,
        )

    def test_missing_evidence_retries_but_never_becomes_success(self):
        calls = []

        def execute(context, feedback):
            calls.append(context.task_id)
            return "SUCCESS"

        result = CodingWorkflow(
            self.engine, self.reader, max_attempts=2
        ).run("Build without proof", self.criteria, execute)

        self.assertFalse(result.completed)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(len(set(calls)), 2)
        self.assertEqual(result.attempts[-1].task_status, TaskStatus.UNKNOWN)
        self.assertEqual(result.stop_reason, "attempt_limit_reached")

    def test_executor_exception_is_recorded_and_does_not_release_the_gate(self):
        def execute(context, feedback):
            raise RuntimeError("untrusted exception text")

        result = CodingWorkflow(
            self.engine, self.reader, max_attempts=1
        ).run("Broken execution", self.criteria, execute)

        self.assertFalse(result.completed)
        self.assertEqual(result.attempts[0].task_status, TaskStatus.UNKNOWN)
        evidence = self.engine.list_evidence(result.final_task_id)
        errors = [item for item in evidence if item["kind"] == "coding_attempt_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["payload"]["error_type"], "RuntimeError")
        self.assertNotIn("untrusted exception text", str(errors[0]["payload"]))

    def test_model_claim_or_callback_return_value_cannot_release_gate(self):
        def execute(context, feedback):
            return {"status": "passed", "tests": "all green"}

        result = CodingWorkflow(
            self.engine, self.reader, max_attempts=1
        ).run("Claimed success", self.criteria, execute)

        self.assertFalse(result.completed)
        self.assertEqual(result.attempts[0].task_status, TaskStatus.UNKNOWN)
        self.assertFalse(result.attempts[0].decision.released)

    def test_gate_rejects_a_truncated_report_even_when_status_was_passed(self):
        task = self.engine.create_task("Truncated report", self.criteria)
        self.engine.start_task(task.task_id)
        self.record_success(task.task_id)
        self.engine.finalize_task(task.task_id)
        report = self.reader.get_task_report(task.task_id)
        report["evidence_truncated"] = True

        decision = CodingVerificationGate().evaluate(report)

        self.assertFalse(decision.released)
        self.assertIn("evidence_truncated", decision.reasons)

    def test_gate_rejects_invalid_ledger_and_inconsistent_verification(self):
        task = self.engine.create_task("Integrity check", self.criteria)
        self.engine.start_task(task.task_id)
        self.record_success(task.task_id)
        self.engine.finalize_task(task.task_id)
        report = self.reader.get_task_report(task.task_id)
        report["ledger_integrity"] = False

        decision = CodingVerificationGate().evaluate(report)

        self.assertFalse(decision.released)
        self.assertIn("ledger_integrity", decision.reasons)

    def test_retry_limit_is_bounded_and_validated(self):
        for value in (0, 6, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    CodingWorkflow(self.engine, self.reader, max_attempts=value)


if __name__ == "__main__":
    unittest.main()
