import tempfile
import unittest
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine, TaskStatus


class EvidenceEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = EvidenceEngine(Path(self.temp_dir.name) / "evidence.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_running_task(self, operator="eq", expected=0):
        task = self.engine.create_task(
            "Test task",
            [Criterion(kind="command_exit", field="return_code", operator=operator, expected=expected)],
        )
        self.engine.start_task(task.task_id)
        return task.task_id

    def test_task_passes_only_when_predeclared_criterion_is_supported(self):
        task_id = self.make_running_task()
        self.engine.record_evidence(
            task_id, kind="command_exit", source="test-runner",
            payload={"return_code": 0},
        )
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.PASSED)

    def test_missing_evidence_is_unknown_not_success(self):
        task_id = self.make_running_task()
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.UNKNOWN)

    def test_contradictory_evidence_fails_task(self):
        task_id = self.make_running_task()
        self.engine.record_evidence(
            task_id, kind="command_exit", source="test-runner",
            payload={"return_code": 1},
        )
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_evidence_can_only_be_recorded_for_running_task(self):
        task = self.engine.create_task("Not started", [Criterion("x", "ok")])
        with self.assertRaises(ValueError):
            self.engine.record_evidence(
                task.task_id, kind="x", source="test", payload={"ok": True},
            )

    def test_ledger_detects_payload_tampering(self):
        task_id = self.make_running_task()
        self.engine.record_evidence(
            task_id, kind="command_exit", source="test-runner",
            payload={"return_code": 0},
        )
        with self.engine._db() as db:
            db.execute("DROP TRIGGER evidence_no_update")
            db.execute("UPDATE evidence SET payload_json='{}'")
        self.assertFalse(self.engine.verify_integrity())

    def test_boolean_is_not_accepted_as_numeric_threshold(self):
        with self.assertRaises(ValueError):
            Criterion("count", "items", operator="gte", expected=True)

    def test_naive_observation_timestamp_is_rejected(self):
        task_id = self.make_running_task()
        with self.assertRaises(ValueError):
            self.engine.record_evidence(
                task_id, kind="command_exit", source="test-runner",
                payload={"return_code": 0}, observed_at="2026-11-14T12:00:00",
            )

    def test_evidence_is_append_only_under_normal_sql_operations(self):
        task_id = self.make_running_task()
        evidence_id = self.engine.record_evidence(
            task_id, kind="command_exit", source="test-runner",
            payload={"return_code": 0},
        )
        with self.engine._db() as db:
            with self.assertRaises(Exception):
                db.execute("DELETE FROM evidence WHERE evidence_id=?", (evidence_id,))


if __name__ == "__main__":
    unittest.main()
