import sys
import tempfile
import unittest
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine, TaskStatus
from evidence_engine.adapters import (
    CommandAdapter,
    CommandSpec,
    FilePolicy,
    JUnitReportAdapter,
)


VALID_XML = """<?xml version='1.0' encoding='utf-8'?>
<testsuites>
  <testsuite name='sample' tests='2' failures='0' errors='0' skipped='0'>
    <testcase classname='sample' name='one'/>
    <testcase classname='sample' name='two'/>
  </testsuite>
</testsuites>
"""

FAILED_XML = """<testsuites>
  <testsuite name='sample' tests='2' failures='1' errors='0' skipped='0'>
    <testcase classname='sample' name='one'/>
    <testcase classname='sample' name='two'><failure message='assertion failed'>details</failure></testcase>
  </testsuite>
</testsuites>
"""


class JUnitReportAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "allowed"
        self.root.mkdir()
        self.engine = EvidenceEngine(Path(self.temp_dir.name) / "evidence.sqlite3")
        self.policy = FilePolicy(allowed_roots=(self.root,))
        self.adapter = JUnitReportAdapter(self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_running_task(self, *, expected_failures=0):
        task = self.engine.create_task(
            "JUnit report verification",
            [
                Criterion(
                    kind="command_result",
                    field="return_code",
                    operator="eq",
                    expected=0,
                    source="evidence_engine.command_adapter:pytest",
                ),
                Criterion(
                    kind="junit_report",
                    field="parse_status",
                    operator="eq",
                    expected="valid",
                    source="evidence_engine.junit_adapter",
                ),
                Criterion(
                    kind="junit_report",
                    field="tests_failed",
                    operator="eq",
                    expected=expected_failures,
                    source="evidence_engine.junit_adapter",
                ),
            ],
        )
        self.engine.start_task(task.task_id)
        return task.task_id

    def run_registered_report_command(self, task_id, xml_text, *, report_arg="report.xml", write=True):
        target = (self.root / report_arg).resolve()
        if write:
            code = (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1].split('=', 1)[1]).write_text(sys.argv[2], encoding='utf-8')"
            )
            argv = (
                sys.executable,
                "-c",
                code,
                f"--junitxml={target}",
                xml_text,
            )
        else:
            argv = (
                sys.executable,
                "-c",
                "pass",
                f"--junitxml={target}",
            )
        spec = CommandSpec(
            command_id="pytest",
            argv=argv,
            cwd=self.root,
            max_output_bytes=4096,
        )
        return CommandAdapter([spec]).run(self.engine, task_id, "pytest")

    def test_valid_report_is_bound_to_the_command_and_counts_are_verified(self):
        task_id = self.make_running_task()
        command = self.run_registered_report_command(task_id, VALID_XML)

        report = self.adapter.inspect(
            self.engine,
            task_id,
            "report.xml",
            command_evidence_id=command.evidence_id,
        )

        self.assertEqual(report.parse_status, "valid")
        self.assertEqual(report.tests_total, 2)
        self.assertEqual(report.tests_passed, 2)
        self.assertEqual(report.tests_failed, 0)
        self.assertEqual(report.tests_errors, 0)
        self.assertTrue(report.stable_read)
        self.assertNotEqual(report.report_sha256, "")
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.PASSED)

    def test_failed_testcase_is_counted_and_fails_the_declared_contract(self):
        task_id = self.make_running_task(expected_failures=0)
        command = self.run_registered_report_command(task_id, FAILED_XML)

        report = self.adapter.inspect(
            self.engine,
            task_id,
            "report.xml",
            command_evidence_id=command.evidence_id,
        )

        self.assertEqual(report.parse_status, "valid")
        self.assertEqual(report.tests_total, 2)
        self.assertEqual(report.tests_failed, 1)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_malformed_xml_is_recorded_as_invalid_and_cannot_pass(self):
        task_id = self.make_running_task()
        command = self.run_registered_report_command(task_id, "<testsuites><broken>")

        report = self.adapter.inspect(
            self.engine,
            task_id,
            "report.xml",
            command_evidence_id=command.evidence_id,
        )

        self.assertEqual(report.parse_status, "invalid")
        self.assertIsNotNone(report.inspection_error)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_doctype_and_entity_declarations_are_rejected(self):
        xml_text = """<!DOCTYPE testsuites [<!ENTITY x 'expanded'>]>
        <testsuites><testsuite><testcase name='&x;'/></testsuite></testsuites>"""
        task_id = self.make_running_task()
        command = self.run_registered_report_command(task_id, xml_text)

        report = self.adapter.inspect(
            self.engine,
            task_id,
            "report.xml",
            command_evidence_id=command.evidence_id,
        )

        self.assertEqual(report.parse_status, "invalid")
        self.assertIn("DTD", report.inspection_error or "")

    def test_stale_report_created_before_command_is_inconclusive(self):
        (self.root / "report.xml").write_text(VALID_XML, encoding="utf-8")
        task_id = self.make_running_task()
        command = self.run_registered_report_command(task_id, "", write=False)

        report = self.adapter.inspect(
            self.engine,
            task_id,
            "report.xml",
            command_evidence_id=command.evidence_id,
        )

        self.assertEqual(report.parse_status, "inconclusive")
        self.assertIn("predates", report.inspection_error or "")

    def test_report_path_must_match_the_registered_command_argument(self):
        task_id = self.make_running_task()
        command = self.run_registered_report_command(
            task_id, VALID_XML, report_arg="other.xml"
        )

        with self.assertRaises(ValueError):
            self.adapter.inspect(
                self.engine,
                task_id,
                "report.xml",
                command_evidence_id=command.evidence_id,
            )

    def test_missing_command_evidence_is_rejected(self):
        task_id = self.make_running_task()
        with self.assertRaises(ValueError):
            self.adapter.inspect(
                self.engine,
                task_id,
                "report.xml",
                command_evidence_id="not-a-record",
            )

    def test_report_cannot_be_inspected_outside_the_allowlisted_root(self):
        task_id = self.make_running_task()
        command = self.run_registered_report_command(task_id, VALID_XML)

        with self.assertRaises(PermissionError):
            self.adapter.inspect(
                self.engine,
                task_id,
                "../outside.xml",
                command_evidence_id=command.evidence_id,
            )


if __name__ == "__main__":
    unittest.main()
