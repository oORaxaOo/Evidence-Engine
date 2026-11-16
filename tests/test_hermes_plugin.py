import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from evidence_engine.hermes_plugin import register


class FakeContext:
    def __init__(self, settings=None):
        self.hooks = {}
        self.hook_options = {}
        self.settings = settings or {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback, **kwargs):
        self.hooks[name] = callback
        self.hook_options[name] = kwargs


class HermesPluginIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.policy_file = Path(self.temp.name) / "policies.json"
        self.database = Path(self.temp.name) / "evidence.sqlite3"
        self.session_id = "session-test-001"
        self._install_runtime_cwd()

    def tearDown(self):
        self.temp.cleanup()

    def _install_runtime_cwd(self):
        agent_package = types.ModuleType("agent")
        agent_package.__path__ = []
        runtime_module = types.ModuleType("agent.runtime_cwd")
        runtime_module.resolve_agent_cwd = lambda: self.root
        self.runtime_patch = patch.dict(
            "sys.modules",
            {"agent": agent_package, "agent.runtime_cwd": runtime_module},
        )
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)

    def _write_policy(self, *, failures=0, exit_code=0):
        junit = (
            f'<testsuite tests="1" failures="{failures}" errors="0" skipped="0">'
            '<testcase classname="sample" name="test_example">'
        )
        if failures:
            junit += '<failure type="AssertionError">untrusted detail</failure>'
        junit += "</testcase></testsuite>"
        script = (
            "from pathlib import Path; "
            f"Path('junit.xml').write_text({junit!r}, encoding='utf-8')"
            + ("; raise SystemExit(1)" if exit_code else "")
        )
        policy = {
            "schema_version": 1,
            "projects": [
                {
                    "root": str(self.root),
                    "commands": [
                        {
                            "id": "unit-tests",
                            "argv": [
                                sys.executable,
                                "-c",
                                script,
                                "--junitxml=junit.xml",
                            ],
                            "junit_xml": "junit.xml",
                            "timeout_seconds": 10,
                        }
                    ],
                }
            ],
        }
        self.policy_file.write_text(json.dumps(policy), encoding="utf-8")
        self.policy_file.chmod(0o600)

    def _register(self, *, max_attempts=5, kie_event_socket=None):
        env = {
            "EVIDENCE_ENGINE_DB": str(self.database),
            "EVIDENCE_ENGINE_POLICY_FILE": str(self.policy_file),
            "EVIDENCE_ENGINE_MAX_ATTEMPTS": str(max_attempts),
        }
        self.env_patch = patch.dict(os.environ, env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        ctx = FakeContext({"kie_event_socket": kie_event_socket})
        register(ctx)
        self.assertIn("pre_verify", ctx.hooks)
        self.assertIn("transform_llm_output", ctx.hooks)
        self.assertTrue(ctx.hook_options["transform_llm_output"]["hold_stream"])
        return ctx

    def _pre_verify(self, ctx, *, attempt=0, coding=True):
        return ctx.hooks["pre_verify"](
            session_id=self.session_id,
            platform="whatsapp",
            model="test-model",
            coding=coding,
            attempt=attempt,
            final_response="The task is complete.",
            changed_paths=["src/example.py"] if coding else [],
        )

    def test_coding_turn_passes_only_after_command_and_junit_evidence(self):
        self._write_policy()
        ctx = self._register()

        directive = self._pre_verify(ctx)

        self.assertIsNone(directive)
        transformed = ctx.hooks["transform_llm_output"](
            response_text="The task is complete.",
            session_id=self.session_id,
            model="test-model",
            platform="whatsapp",
        )
        self.assertIsNone(transformed)

    def test_failed_checks_nudge_then_final_output_is_replaced(self):
        self._write_policy(failures=1, exit_code=1)
        ctx = self._register(max_attempts=2)

        first = self._pre_verify(ctx, attempt=0)
        self.assertEqual(first["action"], "continue")
        self.assertIn("Attempt 1/2", first["message"])
        self.assertIn("untrusted tool data", first["message"].lower())

        second = self._pre_verify(ctx, attempt=1)
        self.assertIsNone(second)
        transformed = ctx.hooks["transform_llm_output"](
            response_text="Everything succeeded.",
            session_id=self.session_id,
            model="test-model",
            platform="whatsapp",
        )
        self.assertIsInstance(transformed, str)
        self.assertIn("not verified", transformed.lower())
        self.assertNotIn("Everything succeeded.", transformed)

    def test_non_coding_turn_does_not_create_evidence_or_rewrite_reply(self):
        ctx = self._register()

        directive = self._pre_verify(ctx, coding=False)
        transformed = ctx.hooks["transform_llm_output"](
            response_text="A normal conversation.",
            session_id=self.session_id,
            model="test-model",
            platform="whatsapp",
        )

        self.assertIsNone(directive)
        self.assertIsNone(transformed)
        self.assertFalse(self.database.exists())

    def test_missing_policy_fails_closed_for_coding_turn(self):
        ctx = self._register()

        directive = self._pre_verify(ctx)
        self.assertIsNone(directive)
        transformed = ctx.hooks["transform_llm_output"](
            response_text="I finished the code.",
            session_id=self.session_id,
            model="test-model",
            platform="whatsapp",
        )

        self.assertIn("not verified", transformed.lower())
        self.assertIn("policy", transformed.lower())


    def test_final_gate_blocks_while_pre_verify_callback_is_still_running(self):
        self._write_policy()
        ctx = self._register(max_attempts=2)
        entered = threading.Event()
        release = threading.Event()
        results = []

        def slow_command(self, engine, task_id, command_id):
            entered.set()
            release.wait(timeout=5)
            raise RuntimeError("simulated slow host command")

        with patch(
            "evidence_engine.hermes_plugin.CommandAdapter.run",
            new=slow_command,
        ):
            worker = threading.Thread(
                target=lambda: results.append(self._pre_verify(ctx, attempt=0))
            )
            worker.start()
            self.assertTrue(entered.wait(timeout=3))
            transformed = ctx.hooks["transform_llm_output"](
                response_text="Everything is complete.",
                session_id=self.session_id,
                model="test-model",
                platform="whatsapp",
            )
            self.assertIn("not verified", transformed.lower())
            release.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_attempts_beyond_configured_limit_cannot_continue(self):
        self._write_policy(failures=1, exit_code=1)
        ctx = self._register(max_attempts=1)

        directive = self._pre_verify(ctx, attempt=0)

        self.assertIsNone(directive)
        transformed = ctx.hooks["transform_llm_output"](
            response_text="Done.",
            session_id=self.session_id,
            model="test-model",
            platform="whatsapp",
        )
        self.assertIn("not verified", transformed.lower())


if __name__ == "__main__":
    unittest.main()



    def test_policy_failure_publishes_only_allowlisted_health_code_to_kie(self):
        with patch("evidence_engine.hermes_plugin.publish_verification_unavailable") as publish:
            ctx = self._register(kie_event_socket="/tmp/kie-engine-events.sock")
            result = self._pre_verify(ctx)

        self.assertIsNone(result)
        publish.assert_called_once_with(
            "/tmp/kie-engine-events.sock",
            "policy_invalid",
        )
