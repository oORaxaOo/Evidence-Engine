import io
import json
import tempfile
import unittest
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine, EvidenceReader, TaskStatus
from evidence_engine.bridge import EvidenceBridge, serve_stdio


class EvidenceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "evidence.sqlite3"
        writer = EvidenceEngine(self.database)
        task = writer.create_task(
            "Bridge read-only test",
            [
                Criterion(
                    kind="test_result",
                    field="passed",
                    operator="eq",
                    expected=True,
                    source="trusted-test",
                )
            ],
        )
        self.task_id = task.task_id
        writer.start_task(task.task_id)
        writer.record_evidence(
            task.task_id,
            kind="test_result",
            source="trusted-test",
            payload={"passed": True},
        )
        writer.finalize_task(task.task_id)
        self.reader = EvidenceReader(self.database)
        self.bridge = EvidenceBridge(self.reader)

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, method, params=None, request_id=1):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {} if params is None else params,
        }

    def test_get_task_report_returns_snapshot_with_effective_status(self):
        response = self.bridge.handle(
            self.request("get_task_report", {"task_id": self.task_id})
        )

        self.assertEqual(response["id"], 1)
        report = response["result"]
        self.assertEqual(report["task"]["status"], TaskStatus.PASSED.value)
        self.assertEqual(report["effective_status"], TaskStatus.PASSED.value)
        self.assertTrue(report["verification_current"])
        self.assertTrue(report["verification_record_valid"])
        self.assertTrue(report["ledger_integrity"])
        self.assertEqual(report["evidence"][0]["payload"]["passed"], True)
        self.assertEqual(report["verification"]["status"], "supported")
        json.dumps(response, allow_nan=False)

    def test_read_only_methods_are_available_individually(self):
        task = self.bridge.handle(
            self.request("get_task", {"task_id": self.task_id})
        )
        evidence = self.bridge.handle(
            self.request("list_evidence", {"task_id": self.task_id})
        )
        verification = self.bridge.handle(
            self.request("get_latest_verification", {"task_id": self.task_id})
        )
        integrity = self.bridge.handle(self.request("verify_integrity"))

        self.assertEqual(task["result"]["status"], TaskStatus.PASSED.value)
        self.assertEqual(len(evidence["result"]), 1)
        self.assertEqual(verification["result"]["status"], "supported")
        self.assertTrue(verification["result"]["record_consistent"])
        self.assertTrue(integrity["result"]["valid"])

    def test_evidence_pagination_is_forwarded_to_the_reader(self):
        for value in (False, False):
            self.reader  # keep the bridge backed by the same read-only reader
        response = self.bridge.handle(
            self.request(
                "get_task_report",
                {"task_id": self.task_id, "evidence_limit": 1, "evidence_offset": 0},
            )
        )

        self.assertEqual(response["result"]["evidence_total"], 1)
        self.assertEqual(len(response["result"]["evidence"]), 1)
        self.assertFalse(response["result"]["evidence_truncated"])

    def test_unknown_and_mutating_methods_are_not_exposed(self):
        for method in (
            "record_evidence",
            "create_task",
            "start_task",
            "finalize_task",
            "execute_command",
            "query_sql",
        ):
            with self.subTest(method=method):
                response = self.bridge.handle(self.request(method))
                self.assertEqual(response["error"]["code"], -32601)

        self.assertEqual(len(self.reader.list_evidence(self.task_id)), 1)

    def test_invalid_params_and_unexpected_fields_are_rejected(self):
        cases = [
            ("get_task", {"task_id": 5}),
            ("get_task", {"task_id": self.task_id, "sql": "DELETE FROM tasks"}),
            ("list_evidence", {"task_id": self.task_id, "limit": True}),
            ("list_evidence", {"task_id": self.task_id, "offset": -1}),
            ("get_task_report", {"task_id": self.task_id, "write": True}),
            ("verify_integrity", {"task_id": self.task_id}),
        ]
        for method, params in cases:
            with self.subTest(method=method, params=params):
                response = self.bridge.handle(self.request(method, params))
                self.assertEqual(response["error"]["code"], -32602)

    def test_malformed_json_and_invalid_requests_return_protocol_errors(self):
        malformed = self.bridge.handle_line("{not-json")
        self.assertEqual(json.loads(malformed)["error"]["code"], -32700)

        invalid = self.bridge.handle({})
        self.assertEqual(invalid["error"]["code"], -32600)

        notification = self.bridge.handle(
            {"jsonrpc": "2.0", "method": "verify_integrity", "params": {}}
        )
        self.assertEqual(notification["error"]["code"], -32600)

    def test_unknown_task_does_not_leak_paths_or_stack_traces(self):
        response = self.bridge.handle(
            self.request("get_task", {"task_id": "not-a-task"})
        )
        encoded = json.dumps(response)
        self.assertEqual(response["error"]["code"], -32004)
        self.assertNotIn("traceback", encoded.lower())
        self.assertNotIn(str(self.database), encoded)

    def test_request_line_size_limit_is_enforced(self):
        oversized = self.bridge.handle_line(
            json.dumps(self.request("verify_integrity")).ljust(1025, " "),
            max_line_bytes=128,
        )
        self.assertEqual(json.loads(oversized)["error"]["code"], -32600)

    def test_json_non_finite_numbers_are_rejected(self):
        response = self.bridge.handle_line(
            '{"jsonrpc":"2.0","id":1,"method":"list_evidence","params":{"task_id":"x","limit":NaN}}'
        )
        self.assertEqual(json.loads(response)["error"]["code"], -32700)

    def test_stdio_transport_handles_multiple_lines(self):
        requests = [
            json.dumps(self.request("get_task", {"task_id": self.task_id})),
            json.dumps(self.request("verify_integrity", request_id=2)),
        ]
        source = io.BytesIO(("\n".join(requests) + "\n").encode("utf-8"))
        destination = io.BytesIO()

        serve_stdio(
            self.reader,
            stdin=source,
            stdout=destination,
            max_line_bytes=4096,
        )

        responses = [
            json.loads(line)
            for line in destination.getvalue().decode("utf-8").splitlines()
        ]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["result"]["status"], TaskStatus.PASSED.value)
        self.assertTrue(responses[1]["result"]["valid"])

    def test_stdio_transport_discards_oversized_line_and_continues(self):
        oversized = json.dumps(self.request("verify_integrity")).ljust(2048, " ")
        valid = json.dumps(self.request("verify_integrity", request_id=2))
        source = io.BytesIO((oversized + "\n" + valid + "\n").encode("utf-8"))
        destination = io.BytesIO()

        serve_stdio(
            self.reader,
            stdin=source,
            stdout=destination,
            max_line_bytes=256,
        )

        responses = [
            json.loads(line)
            for line in destination.getvalue().decode("utf-8").splitlines()
        ]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertTrue(responses[1]["result"]["valid"])


if __name__ == "__main__":
    unittest.main()
