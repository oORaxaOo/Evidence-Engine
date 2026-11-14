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
        task = self.engine.create_task(
            "Read-only view test",
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
        self.task_id = task.task_id
        self.engine.start_task(self.task_id)
        self.engine.record_evidence(
            self.task_id,
            kind="command_result",
            source="trusted-test-runner",
            payload={"return_code": 0},
        )
        self.engine.finalize_task(self.task_id)
        self.reader = EvidenceReader(self.database)

    def tearDown(self):
        self.engine._db().close if False else None
        self.temp_dir.cleanup()

    def test_reader_returns_task_evidence_and_latest_verification(self):
        task = self.reader.get_task(self.task_id)
        evidence = self.reader.list_evidence(self.task_id)
        verification = self.reader.get_latest_verification(self.task_id)

        self.assertEqual(task.status, TaskStatus.PASSED)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["payload"]["return_code"], 0)
        self.assertEqual(verification["status"], "supported")
        self.assertEqual(verification["evidence_sequence"], evidence[0]["sequence"])

    def test_reader_exposes_no_mutating_task_or_evidence_methods(self):
        self.assertFalse(hasattr(self.reader, "create_task"))
        self.assertFalse(hasattr(self.reader, "start_task"))
        self.assertFalse(hasattr(self.reader, "record_evidence"))
        self.assertFalse(hasattr(self.reader, "finalize_task"))

    def test_sqlite_connection_is_read_only(self):
        connection = self.reader._connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "UPDATE tasks SET title='forged' WHERE task_id=?",
                    (self.task_id,),
                )
        finally:
            connection.close()

        self.assertEqual(self.reader.get_task(self.task_id).title, "Read-only view test")

    def test_reader_verifies_the_evidence_hash_chain(self):
        self.assertTrue(self.reader.verify_integrity())

        with self.engine._db() as db:
            db.execute("DROP TRIGGER evidence_no_update")
            db.execute("UPDATE evidence SET payload_json='{}' WHERE task_id=?", (self.task_id,))

        self.assertFalse(self.reader.verify_integrity())

    def test_reader_rejects_a_missing_database_instead_of_initializing_it(self):
        missing = Path(self.temp_dir.name) / "does-not-exist.sqlite3"
        with self.assertRaises(FileNotFoundError):
            EvidenceReader(missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
