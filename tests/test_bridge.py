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
            "params": params or {},
        }

    def test_get_task_report_returns_serializable_evidence_and_verification(self):
        response = self.bridge.handle(self.request(
            "get_task_report",
            {"task_id": self.task_id},
        ))

        self.assertEqual(response["id"], 1)
        result = response["result"]
        self.assertEqual(result["task"]["status"], "passed")
        self.assertEqual(result["evidence"][0]["payload"]["passed"], True)
        self.assertEqual(result["latest_verification"]["status"], "supported")
        self.assertTrue(result["integrity_valid"])
        json.dumps(response)

    def test_reader_methods_are_exposed_individually(self):
        task = self.bridge.handle(self.request("get_task", {"task_id": self.task_id}))
        evidence = self.bridge.handle(self.request("list_evidence", {"task_id": self.task_id}))
        verification = self.bridge.handle(
            self.request("get_latest_verification", {"task_id": self.task_id})
        )
        integrity = self.bridge.handle(self.request("verify_integrity"))

        self.assertEqual(task["result"]["status"], "passed")
        self.assertEqual(len(evidence["result"]), 1)
        self.assertEqual(verification["result"]["status"], "supported")
        self.assertTrue(integrity["result"]["valid"])

    def test_unknown_or_mutating_methods_are_not_exposed(self):
        for method in ("record_evidence", "create_task", "finalize_task", "execute_command", "query_sql"):
            response = self.bridge.handle(self.request(method))
            self.assertEqual(response["error"]["code"], -32601)

        self.assertEqual(len(self.reader.list_evidence(self.task_id)), 1)

    def test_invalid_params_are_rejected(self):
        response = self.bridge.handle(self.request("get_task", {"task_id": 5}))
        self.assertEqual(response["error"]["code"], -32602)

        response = self.bridge.handle(
            self.request("get_task", {"task_id": self.task_id, "sql": "DELETE FROM tasks"})
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_malformed_json_and_invalid_requests_return_protocol_errors(self):
        malformed = self.bridge.handle_line("{not-json")
        self.assertEqual(json.loads(malformed)["error"]["code"], -32700)

        invalid = self.bridge.handle({})
        self.assertEqual(invalid["error"]["code"], -32600)

    def test_unknown_task_returns_not_found_without_internal_traceback(self):
        response = self.bridge.handle(self.request("get_task", {"task_id": "not-a-task"}))
        self.assertEqual(response["error"]["code"], -32004)
        self.assertNotIn("traceback", json.dumps(response).lower())
        self.assertNotIn(str(self.database), json.dumps(response))

    def test_request_line_size_limit_is_enforced(self):
        response = self.bridge.handle_line(
            json.dumps(self.request("verify_integrity")).ljust(1025, " "),
            max_line_bytes=128,
        )
        self.assertEqual(json.loads(response)["error"]["code"], -32600)

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
        self.assertEqual(responses[0]["result"]["status"], "passed")
        self.assertTrue(responses[1]["result"]["valid"])

    def test_stdio_transport_rejects_oversized_line_and_continues(self):
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
