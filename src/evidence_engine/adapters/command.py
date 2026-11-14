"""Allowlisted subprocess execution that records bounded, structured evidence.

The adapter never invokes a shell and never accepts model-supplied argv, cwd,
environment variables, or timeout settings. The operator supplies immutable
CommandSpec objects when constructing the adapter; callers select only a
registered command_id.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from ..engine import EvidenceEngine, TaskStatus


_COMMAND_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_TIMEOUT_SECONDS = 60.0
_MAX_CAPTURE_BYTES = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """An operator-defined, fixed command policy.

    argv is complete and immutable: no caller-supplied arguments are appended.
    The executable must be an absolute, existing executable path. No shell is
    ever used. Environment values are supplied by trusted host configuration.
    """

    command_id: str
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float = 30.0
    max_output_bytes: int = 256 * 1024
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(self.command_id):
            raise ValueError("command_id must use lowercase letters, digits, dot, underscore, or hyphen")

        if isinstance(self.argv, (str, bytes)):
            raise ValueError("argv must be a sequence of fixed argument strings")
        argv = tuple(self.argv)
        if not argv or not all(isinstance(item, str) and item and "\\x00" not in item for item in argv):
            raise ValueError("argv must contain non-empty strings without NUL characters")

        executable = Path(argv[0])
        if not executable.is_absolute():
            raise ValueError("the executable path must be absolute")
        try:
            executable = executable.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"executable path is not available: {exc}") from exc
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("the executable must be an existing executable file")
        object.__setattr__(self, "argv", (str(executable), *argv[1:]))

        try:
            working_directory = Path(self.cwd).expanduser().resolve(strict=True)
        except (OSError, TypeError) as exc:
            raise ValueError(f"working directory is not available: {exc}") from exc
        if not working_directory.is_dir():
            raise ValueError("cwd must resolve to an existing directory")
        object.__setattr__(self, "cwd", working_directory)

        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 0 < float(self.timeout_seconds) <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(f"timeout_seconds must be greater than 0 and at most {_MAX_TIMEOUT_SECONDS:g}")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or not 1024 <= self.max_output_bytes <= _MAX_CAPTURE_BYTES
        ):
            raise ValueError(f"max_output_bytes must be between 1024 and {_MAX_CAPTURE_BYTES}")

        environment = tuple(self.environment)
        seen: set[str] = set()
        for item in environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("environment entries must be (key, value) tuples")
            key, value = item
            if (
                not isinstance(key, str)
                or not _ENV_KEY.fullmatch(key)
                or key in seen
                or not isinstance(value, str)
                or "\\x00" in value
            ):
                raise ValueError("environment contains an invalid or duplicate entry")
            seen.add(key)
        object.__setattr__(self, "environment", environment)


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """Result of one allowlisted execution; output content is never retained."""

    request_id: str
    command_id: str
    argv: tuple[str, ...]
    cwd: str
    started_at: str
    finished_at: str
    duration_ms: int
    process_started: bool
    return_code: int | None
    timed_out: bool
    output_limit_exceeded: bool
    stdout_sha256: str
    stdout_bytes: int
    stderr_sha256: str
    stderr_bytes: int
    launch_error: str | None
    evidence_id: str


class CommandAdapter:
    """Execute only named, pre-registered command specifications."""

    def __init__(self, specs: Iterable[CommandSpec]):
        policies: dict[str, CommandSpec] = {}
        for spec in specs:
            if not isinstance(spec, CommandSpec):
                raise TypeError("all command policies must be CommandSpec instances")
            if spec.command_id in policies:
                raise ValueError(f"duplicate command_id: {spec.command_id}")
            policies[spec.command_id] = spec
        self._policies: Mapping[str, CommandSpec] = MappingProxyType(policies)

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        """Stop the process and ordinary descendants in its isolated process group."""
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            # Also stop descendants that kept the pipe descriptors open after
            # the group leader exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                process.wait()
        else:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def run(
        self,
        engine: EvidenceEngine,
        task_id: str,
        command_id: str,
    ) -> CommandExecution:
        """Run a registered command for an already-running task.

        Callers select a command_id only. They cannot alter argv, cwd, timeout,
        output bounds, or environment for an individual invocation.
        """
        if not isinstance(command_id, str):
            raise TypeError("command_id must be a string")
        try:
            spec = self._policies[command_id]
        except KeyError as exc:
            raise KeyError(f"unregistered command_id: {command_id}") from exc

        task = engine.get_task(task_id)
        if task.status is not TaskStatus.RUNNING:
            raise ValueError("commands can only be executed for a running task")

        request_id = uuid.uuid4().hex
        source = f"evidence_engine.command_adapter:{command_id}"
        engine.record_evidence(
            task_id,
            kind="command_request",
            source=source,
            payload={
                "request_id": request_id,
                "command_id": spec.command_id,
                "argv": list(spec.argv),
                "cwd": str(spec.cwd),
                "timeout_seconds": spec.timeout_seconds,
                "max_output_bytes": spec.max_output_bytes,
                "environment_keys": [key for key, _ in spec.environment],
                "shell": False,
            },
        )

        started_at = _utc_now()
        started_clock = time.monotonic()
        return_code: int | None = None
        process_started = False
        timed_out = False
        output_limit_exceeded = False
        launch_error: str | None = None

        capture_lock = threading.Lock()
        combined_bytes = 0
        limit_event = threading.Event()
        capture = {
            "stdout": {"hasher": hashlib.sha256(), "bytes": 0},
            "stderr": {"hasher": hashlib.sha256(), "bytes": 0},
        }

        def drain(name: str, stream) -> None:
            nonlocal combined_bytes
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        break
                    with capture_lock:
                        capture[name]["hasher"].update(chunk)
                        capture[name]["bytes"] += len(chunk)
                        combined_bytes += len(chunk)
                        if combined_bytes > spec.max_output_bytes:
                            limit_event.set()
            except OSError:
                # The process may have been terminated while the reader was active.
                pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        process: subprocess.Popen[bytes] | None = None
        readers: list[threading.Thread] = []

        try:
            process = subprocess.Popen(
                spec.argv,
                cwd=str(spec.cwd),
                env=dict(spec.environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=(os.name == "posix"),
            )
            process_started = True
            assert process.stdout is not None and process.stderr is not None
            readers = [
                threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
                threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
            ]
            for reader in readers:
                reader.start()

            deadline = started_clock + spec.timeout_seconds
            while process.poll() is None:
                if limit_event.wait(timeout=0.01):
                    output_limit_exceeded = True
                    self._terminate_process_group(process)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._terminate_process_group(process)
                    break

            if process.poll() is None:
                self._terminate_process_group(process)
            return_code = process.wait()
        except OSError as exc:
            launch_error = f"{type(exc).__name__}: {exc}"
            if process is not None and process.poll() is None:
                self._terminate_process_group(process)
                return_code = process.wait()
        finally:
            for reader in readers:
                reader.join()
            if limit_event.is_set():
                output_limit_exceeded = True

        finished_at = _utc_now()
        duration_ms = round((time.monotonic() - started_clock) * 1000)
        payload = {
            "request_id": request_id,
            "command_id": spec.command_id,
            "argv": list(spec.argv),
            "cwd": str(spec.cwd),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "process_started": process_started,
            "return_code": return_code,
            "timed_out": timed_out,
            "output_limit_exceeded": output_limit_exceeded,
            "stdout_sha256": capture["stdout"]["hasher"].hexdigest(),
            "stdout_bytes": capture["stdout"]["bytes"],
            "stderr_sha256": capture["stderr"]["hasher"].hexdigest(),
            "stderr_bytes": capture["stderr"]["bytes"],
            "launch_error": launch_error,
            "output_content_stored": False,
        }
        evidence_id = engine.record_evidence(
            task_id,
            kind="command_result",
            source=source,
            payload=payload,
            observed_at=finished_at,
        )
        return CommandExecution(
            request_id=request_id,
            command_id=spec.command_id,
            argv=spec.argv,
            cwd=str(spec.cwd),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            process_started=process_started,
            return_code=return_code,
            timed_out=timed_out,
            output_limit_exceeded=output_limit_exceeded,
            stdout_sha256=payload["stdout_sha256"],
            stdout_bytes=payload["stdout_bytes"],
            stderr_sha256=payload["stderr_sha256"],
            stderr_bytes=payload["stderr_bytes"],
            launch_error=launch_error,
            evidence_id=evidence_id,
        )
