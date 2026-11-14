import sqlite3
import tempfile
import unittest
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine, TaskStatus
from evidence_engine.reader import EvidenceReader


class EvidenceReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "evidence.sqlite3"
        self.engine = EvidenceEngine(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_running_task(self, title="Reader contract"):
        task = self.engine.create_task(
            title,
            [
                Criterion(
                    kind="command_result",
                    field="return_code",
                    operator="eq",
                    expected=0,
                    source="trusted-test-runner",
                )
            ],
        )
        self.engine.start_task(task.task_id)
        return task.task_id

    def record_success(self, task_id):
        return self.engine.record_evidence(
            task_id,
            kind="command_result",
            source="trusted-test-runner",
            payload={"return_code": 0},
        )

    def test_report_contains_task_evidence_and_current_verification(self):
        task_id = self.make_running_task()
        self.record_success(task_id)
        self.engine.finalize_task(task_id)

        report = EvidenceReader(self.database).get_task_report(task_id)

        self.assertEqual(report["task"]["status"], TaskStatus.PASSED.value)
        self.assertEqual(report["effective_status"], TaskStatus.PASSED.value)
        self.assertTrue(report["ledger_integrity"])
        self.assertTrue(report["verification_current"])
        self.assertEqual(report["verification"]["status"], "supported")
        self.assertEqual(report["evidence_total"], 1)
        self.assertEqual(len(report["evidence"]), 1)
        self.assertEqual(report["evidence"][0]["payload"]["return_code"], 0)

    def test_new_evidence_makes_an_older_verification_stale(self):
        task_id = self.make_running_task()
        self.record_success(task_id)
        old_verification = self.engine.verify_task(task_id)
        self.record_success(task_id)

        report = EvidenceReader(self.database).get_task_report(task_id)

        self.assertEqual(report["verification"]["status"], "supported")
        self.assertLess(
            report["verification"]["evidence_sequence"],
            report["latest_evidence_sequence"],
        )
        self.assertFalse(report["verification_current"])
        self.assertEqual(report["effective_status"], TaskStatus.UNKNOWN.value)

    def test_missing_verification_is_unknown_not_success(self):
        task_id = self.make_running_task()

        report = EvidenceReader(self.database).get_task_report(task_id)

        self.assertIsNone(report["verification"])
        self.assertFalse(report["verification_current"])
        self.assertEqual(report["effective_status"], TaskStatus.RUNNING.value)

    def test_reader_can_page_evidence_without_mutating_it(self):
        task_id = self.make_running_task()
        for value in range(3):
            self.engine.record_evidence(
                task_id,
                kind="command_result",
                source="trusted-test-runner",
                payload={"return_code": value},
            )
        reader = EvidenceReader(self.database)

        first_page = reader.list_evidence(task_id, limit=2, offset=0)
        second_page = reader.list_evidence(task_id, limit=2, offset=2)

        self.assertEqual([item["sequence"] for item in first_page], [1, 2])
        self.assertEqual([item["sequence"] for item in second_page], [3])
        self.assertEqual(len(reader.list_evidence(task_id)), 3)

    def test_page_arguments_are_bounded_and_validated(self):
        task_id = self.make_running_task()
        reader = EvidenceReader(self.database)

        for limit, offset in ((0, 0), (501, 0), (True, 0), (1, -1), (1, True)):
            with self.subTest(limit=limit, offset=offset):
                with self.assertRaises(ValueError):
                    reader.list_evidence(task_id, limit=limit, offset=offset)

    def test_reader_exposes_no_mutating_engine_methods(self):
        reader = EvidenceReader(self.database)

        for method in ("create_task", "start_task", "record_evidence", "finalize_task"):
            with self.subTest(method=method):
                self.assertFalse(hasattr(reader, method))

    def test_sqlite_connection_is_read_only(self):
        reader = EvidenceReader(self.database)
        connection = reader._connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forged(value TEXT)")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM tasks")
        finally:
            connection.close()

    def test_reader_rejects_a_verification_row_that_disagrees_with_its_report(self):
        task_id = self.make_running_task()
        self.record_success(task_id)
        self.engine.finalize_task(task_id)

        with self.engine._db() as db:
            db.execute("DROP TRIGGER verification_no_update")
            db.execute(
                "UPDATE verifications SET status='inconclusive' WHERE task_id=?",
                (task_id,),
            )

        report = EvidenceReader(self.database).get_task_report(task_id)

        self.assertFalse(report["verification_record_valid"])
        self.assertFalse(report["verification_current"])
        self.assertEqual(report["effective_status"], TaskStatus.UNKNOWN.value)
        self.assertEqual(report["reliability_note"], "verification_record_inconsistent")

    def test_reader_verifies_the_entire_evidence_hash_chain(self):
        task_id = self.make_running_task()
        self.record_success(task_id)
        reader = EvidenceReader(self.database)
        self.assertTrue(reader.verify_integrity())

        with self.engine._db() as db:
            db.execute("DROP TRIGGER evidence_no_update")
            db.execute(
                "UPDATE evidence SET payload_json='{}' WHERE task_id=?",
                (task_id,),
            )

        self.assertFalse(reader.verify_integrity())
        report = reader.get_task_report(task_id)
        self.assertFalse(report["ledger_integrity"])
        self.assertEqual(report["effective_status"], TaskStatus.UNKNOWN.value)

    def test_reader_rejects_non_finite_or_invalid_timeouts(self):
        for value in (float("nan"), float("inf"), 0, -1, True, "3"):
            with self.subTest(timeout=value):
                with self.assertRaises(ValueError):
                    EvidenceReader(self.database, timeout_seconds=value)

    def test_reader_does_not_create_a_missing_database(self):
        missing = Path(self.temp_dir.name) / "missing.sqlite3"

        with self.assertRaises(FileNotFoundError):
            EvidenceReader(missing)

        self.assertFalse(missing.exists())

    def test_reader_rejects_invalid_page_type_without_coercion(self):
        reader = EvidenceReader(self.database)
        task_id = self.make_running_task()

        with self.assertRaises(ValueError):
            reader.list_evidence(task_id, limit="10")


if __name__ == "__main__":
    unittest.main()
