import json
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

from evidence_engine.kie_events import publish_verification_unavailable


class _CaptureServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class EvidenceKIEPublisherTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.socket_path = self.root / "kie.sock"

    def _capture(self, callback):
        received = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(inner_self):
                received.append(json.loads(inner_self.rfile.readline(65537)))
                inner_self.wfile.write(b'{"ok":true}\n')
                inner_self.wfile.flush()

        server = _CaptureServer(str(self.socket_path), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(callback(self.socket_path))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        return received[0]

    def test_publisher_sends_only_stable_failure_code(self):
        message = self._capture(
            lambda path: publish_verification_unavailable(path, "policy_invalid")
        )
        self.assertEqual(message["engine_id"], "evidence")
        self.assertEqual(message["event_type"], "evidence.verification_unavailable")
        self.assertEqual(message["payload"], {"failure_code": "policy_invalid"})
        self.assertNotIn("path", json.dumps(message))
        self.assertNotIn("exception", json.dumps(message))

    def test_unknown_failure_code_is_rejected(self):
        self.assertFalse(
            publish_verification_unavailable(self.socket_path, "database_password")
        )

    def test_unavailable_kie_is_fail_open(self):
        self.assertFalse(
            publish_verification_unavailable(self.root / "missing.sock", "verification_exception")
        )

    def test_missing_socket_disables_publishing(self):
        self.assertFalse(publish_verification_unavailable(None, "configuration_missing"))


if __name__ == "__main__":
    unittest.main()
