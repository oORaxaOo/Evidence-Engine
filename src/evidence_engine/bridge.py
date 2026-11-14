"""Small JSON-RPC 2.0 stdio bridge exposing only read-only evidence queries.

The bridge is a transport adapter, not an authorization boundary by itself.
The host must launch it with access to only the intended evidence database and
must not expose the underlying EvidenceEngine writer or database file to the
language model through other tools.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import BinaryIO, Any

from .engine import Task, VerificationStatus
from .reader import EvidenceReader


_DEFAULT_MAX_LINE_BYTES = 1024 * 1024
_ALLOWED_METHODS = frozenset({
    "get_task",
    "list_evidence",
    "get_latest_verification",
    "verify_integrity",
    "get_task_report",
})


def _task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "status": task.status.value,
        "created_at": task.created_at,
        "criteria": [criterion.to_dict() for criterion in task.criteria],
    }


def _verification_to_dict(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    status = value["status"]
    return {
        "verification_id": value["verification_id"],
        "status": status.value if isinstance(status, VerificationStatus) else str(status),
        "report": value["report"],
        "verified_at": value["verified_at"],
        "evidence_sequence": value["evidence_sequence"],
    }


def _response(request_id: str | int, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(
    request_id: str | int | None,
    code: int,
    message: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class EvidenceBridge:
    """Dispatch a small allowlist of read-only JSON-RPC methods."""

    def __init__(self, reader: EvidenceReader):
        if not isinstance(reader, EvidenceReader):
            raise TypeError("reader must be an EvidenceReader")
        self._reader = reader

    @staticmethod
    def _validate_request(request: Any) -> tuple[str | int, str, dict[str, Any]]:
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        request_id = request.get("id")
        if (
            "jsonrpc" not in request
            or request.get("jsonrpc") != "2.0"
            or isinstance(request_id, bool)
            or not isinstance(request_id, (str, int))
            or not isinstance(request.get("method"), str)
            or not request["method"]
        ):
            raise ValueError("request must contain jsonrpc='2.0', a string/integer id, and a method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise TypeError("params must be a JSON object")
        return request_id, request["method"], params

    @staticmethod
    def _task_id(params: dict[str, Any]) -> str:
        if set(params) != {"task_id"}:
            raise TypeError("params must contain only task_id")
        task_id = params.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise TypeError("task_id must be a non-empty string")
        return task_id

    def handle(self, request: Any) -> dict[str, Any]:
        """Handle one already-decoded JSON-RPC request."""
        try:
            request_id, method, params = self._validate_request(request)
        except TypeError as exc:
            request_id = request.get("id") if isinstance(request, dict) else None
            if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                request_id = None
            return _error(request_id, -32602, str(exc))
        except ValueError as exc:
            request_id = request.get("id") if isinstance(request, dict) else None
            if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                request_id = None
            return _error(request_id, -32600, str(exc))

        if method not in _ALLOWED_METHODS:
            return _error(request_id, -32601, "Method not found")

        try:
            if method == "get_task":
                task_id = self._task_id(params)
                return _response(request_id, _task_to_dict(self._reader.get_task(task_id)))

            if method == "list_evidence":
                task_id = self._task_id(params)
                return _response(request_id, list(self._reader.list_evidence(task_id)))

            if method == "get_latest_verification":
                task_id = self._task_id(params)
                value = self._reader.get_latest_verification(task_id)
                return _response(request_id, _verification_to_dict(value))

            if method == "verify_integrity":
                if params:
                    raise TypeError("verify_integrity does not accept parameters")
                return _response(request_id, {"valid": self._reader.verify_integrity()})

            if method == "get_task_report":
                task_id = self._task_id(params)
                task = self._reader.get_task(task_id)
                evidence = list(self._reader.list_evidence(task_id))
                verification = self._reader.get_latest_verification(task_id)
                return _response(
                    request_id,
                    {
                        "task": _task_to_dict(task),
                        "evidence": evidence,
                        "latest_verification": _verification_to_dict(verification),
                        "integrity_valid": self._reader.verify_integrity(),
                    },
                )

        except TypeError as exc:
            return _error(request_id, -32602, str(exc))
        except KeyError:
            return _error(request_id, -32004, "Task not found")
        except Exception:
            # Do not forward database paths, SQL, stack traces, or host details.
            return _error(request_id, -32603, "Internal error")

        return _error(request_id, -32601, "Method not found")

    def handle_line(
        self,
        line: str | bytes,
        *,
        max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES,
    ) -> str:
        """Decode and handle one bounded newline-delimited JSON request."""
        request_id: str | int | None = None
        if (
            isinstance(max_line_bytes, bool)
            or not isinstance(max_line_bytes, int)
            or max_line_bytes < 1
        ):
            raise ValueError("max_line_bytes must be a positive integer")
        try:
            if isinstance(line, bytes):
                if len(line) > max_line_bytes:
                    return json.dumps(_error(None, -32600, "Request line too large"))
                decoded = line.decode("utf-8")
            elif isinstance(line, str):
                if len(line.encode("utf-8")) > max_line_bytes:
                    return json.dumps(_error(None, -32600, "Request line too large"))
                decoded = line
            else:
                return json.dumps(_error(None, -32600, "Request line must be text or bytes"))
            request = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return json.dumps(_error(None, -32700, "Parse error"))

        response = self.handle(request)
        return json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _discard_line_remainder(stream: BinaryIO) -> None:
    """Consume the rest of one overlong line without buffering it in memory."""
    while True:
        chunk = stream.readline(8192)
        if not chunk or chunk.endswith(b"\n"):
            return


def serve_stdio(
    reader: EvidenceReader,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES,
) -> None:
    """Serve newline-delimited JSON-RPC on stdin/stdout; diagnostics stay off stdout."""
    if (
        isinstance(max_line_bytes, bool)
        or not isinstance(max_line_bytes, int)
        or max_line_bytes < 1
    ):
        raise ValueError("max_line_bytes must be a positive integer")
    source = stdin if stdin is not None else sys.stdin.buffer
    destination = stdout if stdout is not None else sys.stdout.buffer
    bridge = EvidenceBridge(reader)

    while True:
        line = source.readline(max_line_bytes + 1)
        if not line:
            return
        if len(line) > max_line_bytes:
            if not line.endswith(b"\n"):
                _discard_line_remainder(source)
            encoded = json.dumps(
                _error(None, -32600, "Request line too large"),
                separators=(",", ":"),
            ).encode("utf-8")
        else:
            encoded = bridge.handle_line(
                line,
                max_line_bytes=max_line_bytes,
            ).encode("utf-8")
        destination.write(encoded + b"\n")
        destination.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Evidence Engine JSON-RPC stdio bridge")
    parser.add_argument("--db", required=True, help="path to an existing Evidence Engine SQLite database")
    parser.add_argument(
        "--max-line-bytes",
        type=int,
        default=_DEFAULT_MAX_LINE_BYTES,
        help="maximum size of one incoming JSON request (default: 1048576)",
    )
    arguments = parser.parse_args(argv)
    try:
        reader = EvidenceReader(arguments.db)
        serve_stdio(reader, max_line_bytes=arguments.max_line_bytes)
    except (OSError, ValueError) as exc:
        print(f"evidence bridge: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
