import hashlib
import os
import tempfile
import unittest
from pathlib import Path
import sys
import time

from evidence_engine import Criterion, EvidenceEngine, TaskStatus
from evidence_engine.adapters import (
    CommandAdapter,
    CommandSpec,
    FilePolicy,
    JUnitReportAdapter,
)


COMMAND_SOURCE = "evidence_engine.command_adapter:pytest"
REPORT_SOURCE = "evidence_engine.junit_report_adapter"


def successful_report():
    return """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="unit" tests="3" failures="0" errors="0" skipped="1">
  <testcase classname="sample" name="passes"/>
  <testcase classname="sample" name="also-passes"/>
  <testcase classname="sample" name="is-skipped"><skipped/></testcase>
</testsuite>
"""


class JUnitReportAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "reports"
        self.root.mkdir()
        self.engine = EvidenceEngine(Path(self.temp_dir.name) / "evidence.sqlite3")
        self.policy = FilePolicy(allowed_roots=(self.root,))
        self.adapter = JUnitReportAdapter(self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_running_task(self):
        task = self.engine.create_task(
            "Validate JUnit test report",
            [
                Criterion("command_result", "process_started", "eq", True, COMMAND_SOURCE),
                Criterion("command_result", "timed_out", "eq", False, COMMAND_SOURCE),
                Criterion("command_result", "return_code", "eq", 0, COMMAND_SOURCE),
                Criterion("junit_report", "report_valid", "eq", True, REPORT_SOURCE),
                Criterion("junit_report", "metadata_consistent", "eq", True, REPORT_SOURCE),
                Criterion("junit_report", "tests", "gte", 1, REPORT_SOURCE),
                Criterion("junit_report", "failures", "eq", 0, REPORT_SOURCE),
                Criterion("junit_report", "errors", "eq", 0, REPORT_SOURCE),
            ],
        )
        self.engine.start_task(task.task_id)
        return task.task_id

    def run_report_command(self, task_id, xml_text=None, name="junit.xml", *, write=True, exit_code=0):
        target = (self.root / name).resolve()
        if write:
            # The command receives a fixed --junitxml path from CommandSpec.
            # It writes the provided XML to that path; the adapter later checks
            # this exact command evidence and path before parsing the report.
            code = (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1].split('=', 1)[1]).write_text(sys.argv[2], encoding='utf-8'); "
                "raise SystemExit(int(sys.argv[3]))"
            )
            args = (code, f"--junitxml={target}", xml_text or "", str(exit_code))
        else:
            code = "pass"
            args = (code, f"--junitxml={target}")

        spec = CommandSpec(
            command_id="pytest",
            argv=(sys.executable, "-c", *args),
            cwd=self.root,
            max_output_bytes=4096,
        )
        return CommandAdapter([spec]).run(self.engine, task_id, "pytest")

    def inspect(self, task_id, command, path="junit.xml", adapter=None):
        return (adapter or self.adapter).inspect(
            self.engine,
            task_id,
            path,
            command_evidence_id=command.evidence_id,
        )

    def test_valid_report_recomputes_counts_and_can_pass_task(self):
        xml = successful_report()
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, xml)

        result = self.inspect(task_id, command)

        self.assertTrue(result.available)
        self.assertTrue(result.report_valid)
        self.assertTrue(result.metadata_consistent)
        self.assertEqual(result.tests, 3)
        self.assertEqual(result.passed, 2)
        self.assertEqual(result.failures, 0)
        self.assertEqual(result.errors, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.sha256, hashlib.sha256(xml.encode("utf-8")).hexdigest())
        self.assertEqual(result.command_evidence_id, command.evidence_id)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.PASSED)

    def test_report_counts_are_recomputed_instead_of_trusting_summary_attributes(self):
        xml = successful_report().replace('tests="3"', 'tests="300"')
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, xml)

        result = self.inspect(task_id, command)

        self.assertEqual(result.tests, 3)
        self.assertFalse(result.metadata_consistent)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_failure_and_error_testcases_are_counted_separately(self):
        xml = """<testsuite tests="3" failures="1" errors="1" skipped="0">
          <testcase name="ok"/>
          <testcase name="failed"><failure message="assertion failed"/></testcase>
          <testcase name="error"><error message="uncaught exception"/></testcase>
        </testsuite>"""
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, xml)

        result = self.inspect(task_id, command)

        self.assertEqual(result.tests, 3)
        self.assertEqual(result.passed, 1)
        self.assertEqual(result.failures, 1)
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_nested_suites_and_xml_namespaces_are_supported(self):
        xml = """<testsuites xmlns="urn:junit">
          <testsuite name="first" tests="2" failures="1" errors="0" skipped="0">
            <testcase name="ok"/>
            <testcase name="bad"><failure/></testcase>
          </testsuite>
        </testsuites>"""
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, xml)

        result = self.inspect(task_id, command)

        self.assertEqual(result.tests, 2)
        self.assertEqual(result.failures, 1)
        self.assertTrue(result.report_valid)

    def test_malformed_xml_is_recorded_as_invalid_and_cannot_pass(self):
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, "<testsuite><testcase></testsuite>")

        result = self.inspect(task_id, command)

        self.assertTrue(result.available)
        self.assertFalse(result.report_valid)
        self.assertIsNotNone(result.inspection_error)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_doctype_and_entity_declarations_are_rejected(self):
        xml = """<!DOCTYPE testsuite [<!ENTITY x "expanded">]>
        <testsuite tests="1"><testcase name="&x;"/></testsuite>"""
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, xml)

        result = self.inspect(task_id, command)

        self.assertFalse(result.report_valid)
        self.assertIn("DOCTYPE", result.inspection_error)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_missing_report_is_inconclusive_not_a_pass_or_failure(self):
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, write=False)

        result = self.inspect(task_id, command)

        self.assertFalse(result.available)
        self.assertIsNone(result.report_valid)
        evidence = self.engine.list_evidence(task_id)
        self.assertTrue(any(item["kind"] == "junit_report_attempt" for item in evidence))
        self.assertFalse(any(item["kind"] == "junit_report" for item in evidence))
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.UNKNOWN)

    def test_report_over_size_limit_is_inconclusive_and_not_parsed(self):
        task_id = self.make_running_task()
        xml = "<testsuite>" + (" " * 2048) + "</testsuite>"
        command = self.run_report_command(task_id, xml)
        policy = FilePolicy(allowed_roots=(self.root,), max_file_size_bytes=1024)
        adapter = JUnitReportAdapter(policy)

        result = self.inspect(task_id, command, adapter=adapter)

        self.assertFalse(result.available)
        self.assertIsNone(result.report_valid)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.UNKNOWN)

    def test_parent_traversal_is_rejected_before_report_evidence_is_written(self):
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, write=False)

        with self.assertRaises(ValueError):
            self.inspect(task_id, command, path="../outside.xml")

        evidence = self.engine.list_evidence(task_id)
        self.assertFalse(any(item["kind"] == "junit_report" for item in evidence))

    def test_symlink_outside_allowed_root_is_rejected(self):
        outside = Path(self.temp_dir.name) / "outside.xml"
        outside.write_text(successful_report(), encoding="utf-8")
        link = self.root / "outside.xml"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, write=False, name="outside.xml")

        with self.assertRaises(PermissionError):
            self.inspect(task_id, command, path="outside.xml")

        evidence = self.engine.list_evidence(task_id)
        self.assertFalse(any(item["kind"] == "junit_report" for item in evidence))

    def test_report_must_match_the_command_declared_output_path(self):
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, successful_report(), name="other.xml")

        with self.assertRaises(ValueError):
            self.inspect(task_id, command, path="junit.xml")

    def test_missing_or_untrusted_command_evidence_is_rejected(self):
        task_id = self.make_running_task()

        with self.assertRaises(ValueError):
            self.adapter.inspect(
                self.engine,
                task_id,
                "junit.xml",
                command_evidence_id="missing-evidence-id",
            )

    def test_stale_report_created_before_command_is_inconclusive(self):
        (self.root / "junit.xml").write_text(successful_report(), encoding="utf-8")
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, write=False)

        result = self.inspect(task_id, command)

        self.assertFalse(result.available)
        self.assertIsNone(result.report_valid)
        self.assertIn("predates", result.inspection_error or "")
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.UNKNOWN)

    def test_report_modified_after_command_completion_is_inconclusive(self):
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, write=False)
        report_path = self.root / "junit.xml"
        report_path.write_text(successful_report(), encoding="utf-8")
        future_timestamp = time.time() + 60
        os.utime(report_path, (future_timestamp, future_timestamp))

        result = self.inspect(task_id, command)

        self.assertFalse(result.available)
        self.assertIsNone(result.report_valid)
        self.assertIn("after the command completed", result.inspection_error or "")
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.UNKNOWN)

    def test_evidence_contains_digest_and_summary_but_never_raw_xml(self):
        xml = successful_report()
        task_id = self.make_running_task()
        command = self.run_report_command(task_id, xml)

        self.inspect(task_id, command)

        evidence = self.engine.list_evidence(task_id)
        report = next(item for item in evidence if item["kind"] == "junit_report")
        payload = report["payload"]
        self.assertEqual(payload["sha256"], hashlib.sha256(xml.encode("utf-8")).hexdigest())
        self.assertEqual(payload["command_evidence_id"], command.evidence_id)
        self.assertFalse(payload["content_stored"])
        self.assertNotIn("xml", payload)
        self.assertNotIn(xml, str(payload))


if __name__ == "__main__":
    unittest.main()
