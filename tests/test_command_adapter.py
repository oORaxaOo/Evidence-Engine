"""Tests for the allowlisted, host-side command adapter."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine, TaskStatus, VerificationStatus
from evidence_engine.adapters.command import CommandAdapter, CommandPolicy


class CommandAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.engine = EvidenceEngine(self.root / "evidence.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def policy(self, command_id="safe-python", args=(), **overrides):
        values = {
            "command_id": command_id,
            "executable": sys.executable,
            "args": tuple(args),
            "cwd": str(self.root),
            "timeout_seconds": 5.0,
            "max_output_bytes": 4096,
        }
        values.update(overrides)
        return CommandPolicy(**values)

    def make_running_task(self, command_id="safe-python"):
        source = f"command-adapter:{command_id}"
        task = self.engine.create_task(
            "Run an allowlisted command",
            [Criterion(
                kind="command_execution",
                field="return_code",
                operator="eq",
                expected=0,
                source=source,
            )],
        )
        self.engine.start_task(task.task_id)
        return task.task_id

    def test_successful_process_result_is_recorded_and_can_pass_task(self):
        task_id = self.make_running_task()
        adapter = CommandAdapter(self.engine, [self.policy(args=("-c", "print('hello')"))])

        result = adapter.run(task_id, "safe-python")

        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.stdout.strip(), "hello")
        self.assertTrue(result.started)
        self.assertIsNotNone(result.evidence_id)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.PASSED)

    def test_nonzero_exit_is_recorded_and_fails_success_criterion(self):
        task_id = self.make_running_task()
        adapter = CommandAdapter(self.engine, [self.policy(args=("-c", "raise SystemExit(7)"))])

        result = adapter.run(task_id, "safe-python")

        self.assertEqual(result.return_code, 7)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_timeout_is_recorded_and_never_reported_as_success(self):
        task_id = self.make_running_task()
        adapter = CommandAdapter(
            self.engine,
            [self.policy(args=("-c", "import time; time.sleep(2)"), timeout_seconds=0.1)],
        )

        result = adapter.run(task_id, "safe-python")

        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.return_code, 0)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_output_is_bounded_and_streams_are_fully_drained(self):
        task_id = self.make_running_task()
        adapter = CommandAdapter(
            self.engine,
            [self.policy(
                args=("-c", "print('x' * 200000); print('y' * 200000, file=__import__('sys').stderr)"),
                max_output_bytes=64,
            )],
        )

        result = adapter.run(task_id, "safe-python")

        self.assertEqual(result.return_code, 0)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 64)
        self.assertLessEqual(len(result.stderr.encode("utf-8")), 64)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)

    def test_unconfigured_command_is_rejected_before_any_process_runs(self):
        task_id = self.make_running_task()
        adapter = CommandAdapter(self.engine, [self.policy()])

        with self.assertRaises(KeyError):
            adapter.run(task_id, "not-allowlisted")

        self.assertEqual(self.engine.list_evidence(task_id), ())

    def test_command_cannot_run_for_a_task_that_is_not_running(self):
        task = self.engine.create_task("Not started", [Criterion("x", "ok")])
        marker = self.root / "must-not-exist"
        policy = self.policy(args=(
            "-c",
            "from pathlib import Path; Path('must-not-exist').write_text('ran')",
        ))
        adapter = CommandAdapter(self.engine, [policy])

        with self.assertRaises(ValueError):
            adapter.run(task.task_id, "safe-python")

        self.assertFalse(marker.exists())
        self.assertEqual(self.engine.list_evidence(task.task_id), ())

    def test_environment_is_not_inherited_from_the_assistant_process(self):
        task_id = self.make_running_task()
        variable = "EVIDENCE_ENGINE_TEST_SECRET"
        previous = os.environ.get(variable)
        os.environ[variable] = "should-not-leak"
        try:
            policy = self.policy(args=(
                "-c",
                "import os; print(os.getenv('EVIDENCE_ENGINE_TEST_SECRET', 'absent'))",
            ))
            result = CommandAdapter(self.engine, [policy]).run(task_id, "safe-python")
        finally:
            if previous is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = previous

        self.assertEqual(result.stdout.strip(), "absent")

    def test_policy_rejects_relative_executable(self):
        with self.assertRaises(ValueError):
            self.policy(executable="python")

    def test_evidence_records_source_and_timeout_metadata(self):
        task_id = self.make_running_task()
        result = CommandAdapter(
            self.engine,
            [self.policy(args=("-c", "print('recorded')"))],
        ).run(task_id, "safe-python")

        evidence = self.engine.list_evidence(task_id)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["source"], "command-adapter:safe-python")
        self.assertEqual(evidence[0]["payload"]["command_id"], "safe-python")
        self.assertFalse(evidence[0]["payload"]["timed_out"])
        self.assertEqual(evidence[0]["evidence_id"], result.evidence_id)


if __name__ == "__main__":
    unittest.main()
