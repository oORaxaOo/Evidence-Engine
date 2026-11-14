import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine, TaskStatus
from evidence_engine.adapters import FileAdapter, FilePolicy


class FileAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "allowed"
        self.root.mkdir()
        self.engine = EvidenceEngine(Path(self.temp_dir.name) / "evidence.sqlite3")
        self.adapter = FileAdapter(FilePolicy(allowed_roots=(self.root,)))

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_running_task(self, *, field="exists", operator="eq", expected=True):
        task = self.engine.create_task(
            "File inspection test",
            [Criterion(
                kind="file_snapshot",
                field=field,
                operator=operator,
                expected=expected,
                source="evidence_engine.file_adapter",
            )],
        )
        self.engine.start_task(task.task_id)
        return task.task_id

    def test_existing_file_is_hashed_without_storing_its_contents(self):
        file_path = self.root / "report.txt"
        content = b"verified report content\n"
        file_path.write_bytes(content)
        expected_digest = hashlib.sha256(content).hexdigest()
        task_id = self.make_running_task(
            field="sha256", operator="eq", expected=expected_digest
        )

        result = self.adapter.inspect(self.engine, task_id, "report.txt")

        self.assertTrue(result.exists)
        self.assertTrue(result.is_file)
        self.assertTrue(result.stable_read)
        self.assertEqual(result.sha256, expected_digest)
        evidence = self.engine.list_evidence(task_id)
        payload = next(item["payload"] for item in evidence if item["kind"] == "file_snapshot")
        self.assertNotIn("content", payload)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.PASSED)

    def test_missing_file_is_recorded_as_missing_not_as_an_error(self):
        task_id = self.make_running_task(field="exists", operator="eq", expected=False)

        result = self.adapter.inspect(self.engine, task_id, "not-created.txt")

        self.assertFalse(result.exists)
        self.assertIsNone(result.sha256)
        self.assertIsNone(result.inspection_error)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.PASSED)

    def test_absolute_path_is_rejected_before_evidence_is_written(self):
        task_id = self.make_running_task()
        with self.assertRaises(ValueError):
            self.adapter.inspect(self.engine, task_id, str(self.root / "report.txt"))
        self.assertEqual(self.engine.list_evidence(task_id), ())

    def test_parent_traversal_is_rejected_before_evidence_is_written(self):
        task_id = self.make_running_task()
        with self.assertRaises(ValueError):
            self.adapter.inspect(self.engine, task_id, "../outside.txt")
        self.assertEqual(self.engine.list_evidence(task_id), ())

    def test_symlink_that_resolves_outside_root_is_rejected(self):
        outside = Path(self.temp_dir.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "outside-link.txt"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")

        task_id = self.make_running_task()
        with self.assertRaises(PermissionError):
            self.adapter.inspect(self.engine, task_id, "outside-link.txt")
        self.assertEqual(self.engine.list_evidence(task_id), ())

    def test_file_over_size_limit_is_not_hashed(self):
        (self.root / "large.bin").write_bytes(b"x" * 2048)
        policy = FilePolicy(allowed_roots=(self.root,), max_file_size_bytes=1024)
        adapter = FileAdapter(policy)
        task_id = self.make_running_task(field="stable_read", operator="eq", expected=False)

        result = adapter.inspect(self.engine, task_id, "large.bin")

        self.assertTrue(result.exists)
        self.assertIsNone(result.sha256)
        self.assertFalse(result.stable_read)
        self.assertIsNotNone(result.inspection_error)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.PASSED)

    def test_file_inspection_requires_a_running_task(self):
        task = self.engine.create_task(
            "Not running",
            [Criterion("file_snapshot", "exists", "eq", True)],
        )
        with self.assertRaises(ValueError):
            self.adapter.inspect(self.engine, task.task_id, "report.txt")

    def test_file_policy_rejects_a_root_that_does_not_exist(self):
        with self.assertRaises(ValueError):
            FilePolicy(allowed_roots=(self.root / "missing",))

    def test_file_policy_requires_a_positive_bounded_size_limit(self):
        with self.assertRaises(ValueError):
            FilePolicy(allowed_roots=(self.root,), max_file_size_bytes=0)
        with self.assertRaises(ValueError):
            FilePolicy(allowed_roots=(self.root,), max_file_size_bytes=1024 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
