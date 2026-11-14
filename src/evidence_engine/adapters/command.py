"""Allowlisted subprocess execution that records bounded, structured evidence.

The model never supplies an executable or argv at run time. An operator creates
CommandPolicy objects in trusted host code; the adapter accepts only a policy ID.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..engine import EvidenceEngine, TaskStatus


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _capture_stream(stream, limit: int) -> tuple[bytes, bool]:
    """Drain a pipe completely while retaining at most limit bytes."""
    retained = bytearray()
    truncated = False

    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        remaining = limit - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True

    return bytes(retained), truncated


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process group on POSIX, or the process on other platforms."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            process.kill()
    else:
        process.kill()


@dataclass(frozen=True)
class CommandPolicy:
    """Operator-defined command specification.

    The executable and all arguments are fixed when the policy is created.
    Runtime callers can select a policy ID, but cannot add or alter arguments.
    """

    command_id: str
    executable: str
    args: tuple[str, ...]
    cwd: str
    timeout_seconds: float = 30.0
    max_output_bytes: int = 16_384
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not self.command_id.strip():
            raise ValueError("command_id must be a non-empty string")
        if any(character in self.command_id for character in "\\x00\\r\\n"):
            raise ValueError("command_id contains a forbidden control character")

        if not isinstance(self.executable, str) or not self.executable:
            raise ValueError("executable must be a non-empty absolute path")
        executable_path = Path(self.executable)
        if not executable_path.is_absolute():
            raise ValueError("executable must be an absolute path")
        try:
            executable_path = executable_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"executable does not exist: {self.executable}") from exc
        if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
            raise ValueError("executable must be an executable regular file")
        object.__setattr__(self, "executable", str(executable_path))

        if isinstance(self.args, (str, bytes)):
            raise TypeError("args must be a sequence of strings, not one string")
        try:
            arguments = tuple(self.args)
        except TypeError as exc:
            raise TypeError("args must be an iterable of strings") from exc
        if not all(isinstance(argument, str) for argument in arguments):
            raise TypeError("every command argument must be a string")
        if any("\\x00" in argument for argument in arguments):
            raise ValueError("command arguments must not contain NUL characters")
        object.__setattr__(self, "args", arguments)

        if not isinstance(self.cwd, str) or not self.cwd:
            raise ValueError("cwd must be an existing directory")
        try:
            working_directory = Path(self.cwd).resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"working directory does not exist: {self.cwd}") from exc
        if not working_directory.is_dir():
            raise ValueError("cwd must be a directory")
        object.__setattr__(self, "cwd", str(working_directory))

        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite number greater than zero")
        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or self.max_output_bytes < 1
        ):
            raise ValueError("max_output_bytes must be a positive integer")

        if isinstance(self.environment, (str, bytes)):
            raise TypeError("environment must be a sequence of key/value pairs")
        try:
            environment = tuple(tuple(item) for item in self.environment)
        except TypeError as exc:
            raise TypeError("environment must contain key/value pairs") from exc
        seen: set[str] = set()
        for item in environment:
            if len(item) != 2:
                raise ValueError("each environment entry must contain exactly two values")
            key, value = item
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("environment keys and values must be strings")
            if not key or "=" in key or "\\x00" in key or "\\x00" in value:
                raise ValueError("environment contains an invalid key or value")
            if key in seen:
                raise ValueError(f"duplicate environment key: {key}")
            seen.add(key)
        object.__setattr__(self, "environment", environment)


@dataclass(frozen=True)
class CommandResult:
    task_id: str
    command_id: str
    started: bool
    return_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    launch_error: str | None
    started_at: str
    finished_at: str
    duration_ms: float
    evidence_id: str | None = None


class CommandAdapter:
    """Run only operator-allowlisted commands and immediately record the result.

    This adapter is intended for trusted host code. Do not expose arbitrary
    policy construction or evidence-writing methods as LLM-callable tools.
    """

    def __init__(self, engine: EvidenceEngine, policies: Iterable[CommandPolicy]):
        if not isinstance(engine, EvidenceEngine):
            raise TypeError("engine must be an EvidenceEngine")
        policy_map: dict[str, CommandPolicy] = {}
        for policy in policies:
            if not isinstance(policy, CommandPolicy):
                raise TypeError("policies must contain CommandPolicy objects")
            if policy.command_id in policy_map:
                raise ValueError(f"duplicate command_id: {policy.command_id}")
            policy_map[policy.command_id] = policy
        self._engine = engine
        self._policies = policy_map

    def run(self, task_id: str, command_id: str) -> CommandResult:
        """Run a preconfigured command and persist its actual process result."""
        if not isinstance(command_id, str) or command_id not in self._policies:
            raise KeyError(f"command is not allowlisted: {command_id!r}")

        task = self._engine.get_task(task_id)
        if task.status is not TaskStatus.RUNNING:
            raise ValueError("commands can only run for a running task")

        policy = self._policies[command_id]
        argv = [policy.executable, *policy.args]
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8"}
        environment.update(dict(policy.environment))

        started_at = _utc_now()
        monotonic_start = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        stdout_bytes = b""
        stderr_bytes = b""
        stdout_truncated = False
        stderr_truncated = False
        timed_out = False
        launch_error: str | None = None
        return_code: int | None = None
        started = False

        try:
            process = subprocess.Popen(
                argv,
                shell=False,
                cwd=policy.cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                start_new_session=(os.name == "posix"),
            )
            started = True
        except OSError as exc:
            launch_error = f"{type(exc).__name__}: {exc}"

        if process is not None:
            assert process.stdout is not None
            assert process.stderr is not None
            captured: dict[str, tuple[bytes, bool]] = {}
            stdout_thread = threading.Thread(
                target=lambda: captured.setdefault(
                    "stdout", _capture_stream(process.stdout, policy.max_output_bytes)
                ),
                name="evidence-stdout-reader",
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=lambda: captured.setdefault(
                    "stderr", _capture_stream(process.stderr, policy.max_output_bytes)
                ),
                name="evidence-stderr-reader",
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                process.wait(timeout=policy.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process(process)
                process.wait()
            finally:
                stdout_thread.join()
                stderr_thread.join()
                process.stdout.close()
                process.stderr.close()

            return_code = process.returncode
            stdout_bytes, stdout_truncated = captured.get("stdout", (b"", False))
            stderr_bytes, stderr_truncated = captured.get("stderr", (b"", False))

        finished_at = _utc_now()
        duration_ms = round((time.monotonic() - monotonic_start) * 1000, 3)
        result = CommandResult(
            task_id=task_id,
            command_id=command_id,
            started=started,
            return_code=return_code,
            timed_out=timed_out,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            launch_error=launch_error,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )

        evidence_id = self._engine.record_evidence(
            task_id,
            kind="command_execution",
            source=f"command-adapter:{command_id}",
            payload={
                "command_id": command_id,
                "executable": policy.executable,
                "argv": argv,
                "started": result.started,
                "return_code": result.return_code,
                "timed_out": result.timed_out,
                "launch_error": result.launch_error,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "duration_ms": result.duration_ms,
            },
            observed_at=finished_at,
        )
        return replace(result, evidence_id=evidence_id)
