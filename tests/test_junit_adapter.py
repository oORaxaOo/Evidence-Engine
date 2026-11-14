import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine, TaskStatus
from evidence_engine.adapters import FilePolicy
from evidence_engine.adapters.junit import JUnitReportAdapter


SOURCE = "evidence_engine.junit_report_adapter"


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
        self.adapter = JUnitReportAdapter(FilePolicy(allowed_roots=(self.root,)))

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_running_task(self):
        task = self.engine.create_task(
            "Validate JUnit test report",
            [
                Criterion("junit_report", "report_valid", "eq", True, SOURCE),
                Criterion("junit_report", "metadata_consistent", "eq", True, SOURCE),
                Criterion("junit_report", "tests", "gte", 1, SOURCE),
                Criterion("junit_report", "failures", "eq", 0, SOURCE),
                Criterion("junit_report", "errors", "eq", 0, SOURCE),
            ],
        )
        self.engine.start_task(task.task_id)
        return task.task_id

    def write_report(self, content, name="junit.xml"):
        path = self.root / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def test_valid_report_recomputes_counts_and_can_pass_task(self):
        xml = successful_report()
        self.write_report(xml)
        task_id = self.make_running_task()

        result = self.adapter.inspect(self.engine, task_id, "junit.xml")

        self.assertTrue(result.available)
        self.assertTrue(result.report_valid)
        self.assertTrue(result.metadata_consistent)
        self.assertEqual(result.tests, 3)
        self.assertEqual(result.passed, 2)
        self.assertEqual(result.failures, 0)
        self.assertEqual(result.errors, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.sha256, hashlib.sha256(xml.encode("utf-8")).hexdigest())
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.PASSED)

    def test_report_counts_are_recomputed_instead_of_trusting_summary_attributes(self):
        xml = successful_report().replace('tests="3"', 'tests="300"')
        self.write_report(xml)
        task_id = self.make_running_task()

        result = self.adapter.inspect(self.engine, task_id, "junit.xml")

        self.assertEqual(result.tests, 3)
        self.assertFalse(result.metadata_consistent)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_failure_and_error_testcases_are_counted_separately(self):
        xml = """<testsuite tests="3" failures="1" errors="1" skipped="0">
          <testcase name="ok"/>
          <testcase name="failed"><failure message="assertion failed"/></testcase>
          <testcase name="error"><error message="uncaught exception"/></testcase>
        </testsuite>"""
        self.write_report(xml)
        task_id = self.make_running_task()

        result = self.adapter.inspect(self.engine, task_id, "junit.xml")

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
        self.write_report(xml)
        task_id = self.make_running_task()

        result = self.adapter.inspect(self.engine, task_id, "junit.xml")

        self.assertEqual(result.tests, 2)
        self.assertEqual(result.failures, 1)
        self.assertTrue(result.report_valid)

    def test_malformed_xml_is_recorded_as_invalid_and_cannot_pass(self):
        self.write_report("<testsuite><testcase></testsuite>")
        task_id = self.make_running_task()

        result = self.adapter.inspect(self.engine, task_id, "junit.xml")

        self.assertTrue(result.available)
        self.assertFalse(result.report_valid)
        self.assertIsNotNone(result.inspection_error)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_doctype_and_entity_declarations_are_rejected(self):
        xml = b"""<!DOCTYPE testsuite [<!ENTITY x "expanded">]>
        <testsuite tests="1"><testcase name="&x;"/></testsuite>"""
        self.write_report(xml)
        task_id = self.make_running_task()

        result = self.adapter.inspect(self.engine, task_id, "junit.xml")

        self.assertFalse(result.report_valid)
        self.assertIn("DOCTYPE", result.inspection_error)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.FAILED)

    def test_missing_report_is_inconclusive_not_a_pass_or_failure(self):
        task_id = self.make_running_task()

        result = self.adapter.inspect(self.engine, task_id, "missing.xml")

        self.assertFalse(result.available)
        self.assertIsNone(result.report_valid)
        evidence = self.engine.list_evidence(task_id)
        self.assertTrue(any(item["kind"] == "junit_report_attempt" for item in evidence))
        self.assertFalse(any(item["kind"] == "junit_report" for item in evidence))
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.UNKNOWN)

    def test_report_over_size_limit_is_inconclusive_and_not_parsed(self):
        self.write_report("<testsuite>" + (" " * 2048) + "</testsuite>")
        policy = FilePolicy(allowed_roots=(self.root,), max_file_size_bytes=1024)
        adapter = JUnitReportAdapter(policy)
        task_id = self.make_running_task()

        result = adapter.inspect(self.engine, task_id, "junit.xml")

        self.assertFalse(result.available)
        self.assertIsNone(result.report_valid)
        self.assertEqual(self.engine.finalize_task(task_id).status, TaskStatus.UNKNOWN)

    def test_parent_traversal_is_rejected_before_evidence_is_written(self):
        task_id = self.make_running_task()

        with self.assertRaises(ValueError):
            self.adapter.inspect(self.engine, task_id, "../outside.xml")

        self.assertEqual(self.engine.list_evidence(task_id), ())

    def test_symlink_outside_allowed_root_is_rejected(self):
        outside = Path(self.temp_dir.name) / "outside.xml"
        outside.write_text(successful_report(), encoding="utf-8")
        link = self.root / "outside.xml"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        task_id = self.make_running_task()

        with self.assertRaises(PermissionError):
            self.adapter.inspect(self.engine, task_id, "outside.xml")

        self.assertEqual(self.engine.list_evidence(task_id), ())

    def test_evidence_contains_digest_and_summary_but_never_raw_xml(self):
        xml = successful_report()
        self.write_report(xml)
        task_id = self.make_running_task()

        self.adapter.inspect(self.engine, task_id, "junit.xml")

        evidence = self.engine.list_evidence(task_id)
        report = next(item for item in evidence if item["kind"] == "junit_report")
        payload = report["payload"]
        self.assertEqual(payload["sha256"], hashlib.sha256(xml.encode("utf-8")).hexdigest())
        self.assertFalse(payload["content_stored"])
        self.assertNotIn("xml", payload)
        self.assertNotIn(xml, str(payload))


if __name__ == "__main__":
    unittest.main()
