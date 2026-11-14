import sys
import tempfile
import unittest
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine, TaskStatus
from evidence_engine.adapters.command import CommandAdapter, CommandSpec


class CommandAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = EvidenceEngine(Path(self.temp_dir.name) / "evidence.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_running_task(self, expected_return_code=0):
        task = self.engine.create_task(
            "Command adapter test",
            [Criterion(
                kind="command_result",
                field="return_code",
                operator="eq",
                expected=expected_return_code,
                source="evidence_engine.command_adapter:test",
            )],
        )
        self.engine.start_task(task.task_id)
        return task.task_id

    def make_adapter(self, code, *, timeout=3.0, output_limit=65536):
        spec = CommandSpec(
            command_id="test",
            argv=(sys.executable, "-c", code),
            cwd=Path(self.temp_dir.name),
            timeout_seconds=timeout,
            max_output_bytes=output_limit,
        )
        return CommandAdapter([spec])

    def test_successful_process_is_recorded_as_structured_evidence(self):
        task_id = self.make_running_task()
        result = self.make_adapter("print('evidence-ok')").run(
            self.engine, task_id, "test"
        )

        self.assertTrue(result.process_started)
        self.assertEqual(result.return_code, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.output_limit_exceeded)
        self.assertEqual(result.stdout_bytes, len(b"evidence-ok\\n"))
        self.assertNotEqual(result.stdout_sha256, "")
        evidence = self.engine.list_evidence(task_id)
        recorded = [item for item in evidence if item["kind"] == "command_result"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["payload"]["return_code"], 0)
        self.assertNotIn("stdout_preview", recorded[0]["payload"])
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.PASSED)

    def test_nonzero_exit_is_recorded_and_fails_the_declared_success_criterion(self):
        task_id = self.make_running_task(expected_return_code=0)
        result = self.make_adapter("raise SystemExit(7)").run(
            self.engine, task_id, "test"
        )

        self.assertEqual(result.return_code, 7)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_timeout_terminates_the_process_and_records_timeout(self):
        task_id = self.make_running_task()
        result = self.make_adapter("import time; time.sleep(5)", timeout=0.15).run(
            self.engine, task_id, "test"
        )

        self.assertTrue(result.timed_out)
        self.assertTrue(result.process_started)
        self.assertLess(result.duration_ms, 3000)
        evidence = self.engine.list_evidence(task_id)
        payload = next(item["payload"] for item in evidence if item["kind"] == "command_result")
        self.assertTrue(payload["timed_out"])

    def test_output_limit_terminates_the_process_without_storing_plaintext(self):
        task_id = self.make_running_task()
        result = self.make_adapter("print('x' * 100000)", output_limit=1024).run(
            self.engine, task_id, "test"
        )

        self.assertTrue(result.output_limit_exceeded)
        evidence = self.engine.list_evidence(task_id)
        payload = next(item["payload"] for item in evidence if item["kind"] == "command_result")
        self.assertTrue(payload["output_limit_exceeded"])
        self.assertNotIn("stdout_preview", payload)

    def test_unregistered_command_id_is_rejected_without_running_anything(self):
        task_id = self.make_running_task()
        adapter = self.make_adapter("raise SystemExit(0)")

        with self.assertRaises(KeyError):
            adapter.run(self.engine, task_id, "not-registered")

        self.assertEqual(self.engine.list_evidence(task_id), [])

    def test_command_requires_running_task(self):
        task = self.engine.create_task(
            "Not running",
            [Criterion("command_result", "return_code", "eq", 0)],
        )
        adapter = self.make_adapter("raise SystemExit(0)")
        with self.assertRaises(ValueError):
            adapter.run(self.engine, task.task_id, "test")

    def test_command_spec_rejects_relative_executable_paths(self):
        with self.assertRaises(ValueError):
            CommandSpec(
                command_id="unsafe",
                argv=("python", "-c", "print('no')"),
                cwd=Path(self.temp_dir.name),
            )


if __name__ == "__main__":
    unittest.main()
