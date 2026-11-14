"""Bounded JSON-RPC 2.0 stdio transport for read-only evidence queries.

This bridge is a transport adapter, not an authorization boundary. The host
must launch it with access only to the intended database and must not expose
EvidenceEngine writers, adapter policies, or database write access to a model.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, BinaryIO

from .reader import EvidenceReader


_DEFAULT_MAX_LINE_BYTES = 1024 * 1024
_ALLOWED_METHODS = frozenset(
    {
        "get_task",
        "list_evidence",
        "get_latest_verification",
        "verify_integrity",
        "get_task_report",
    }
)


def _error(request_id: str | int | None, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _response(request_id: str | int, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _strict_json_loads(value: str) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-standard JSON constant: {constant}")

    return json.loads(value, parse_constant=reject_constant)


class EvidenceBridge:
    """Dispatch a fixed allowlist of read-only JSON-RPC methods."""

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
            request.get("jsonrpc") != "2.0"
            or isinstance(request_id, bool)
            or not isinstance(request_id, (str, int))
            or not isinstance(request.get("method"), str)
            or not request["method"].strip()
        ):
            raise ValueError(
                "request requires jsonrpc='2.0', a string/integer id, and a method"
            )

        params = request.get("params", {})
        if not isinstance(params, dict):
            raise TypeError("params must be a JSON object")
        return request_id, request["method"], params

    @staticmethod
    def _task_id(params: dict[str, Any], allowed: set[str]) -> str:
        if not set(params).issubset(allowed) or "task_id" not in params:
            raise TypeError("params contain missing or unexpected fields")
        task_id = params.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise TypeError("task_id must be a non-empty string")
        return task_id

    @staticmethod
    def _page_value(
        params: dict[str, Any],
        name: str,
        default: int,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        value = params.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value < minimum or (maximum is not None and value > maximum):
            if maximum is None:
                raise TypeError(f"{name} must be at least {minimum}")
            raise TypeError(f"{name} must be between {minimum} and {maximum}")
        return value

    def handle(self, request: Any) -> dict[str, Any]:
        """Handle one already-decoded JSON-RPC request."""
        try:
            request_id, method, params = self._validate_request(request)
        except (TypeError, ValueError) as exc:
            request_id = request.get("id") if isinstance(request, dict) else None
            if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                request_id = None
            return _error(request_id, -32600, str(exc))

        if method not in _ALLOWED_METHODS:
            return _error(request_id, -32601, "Method not found")

        try:
            if method == "get_task":
                task_id = self._task_id(params, {"task_id"})
                result = self._reader.get_task(task_id)
                return _response(request_id, result)

            if method == "list_evidence":
                task_id = self._task_id(params, {"task_id", "limit", "offset"})
                limit = self._page_value(params, "limit", 100, minimum=1, maximum=500)
                offset = self._page_value(params, "offset", 0, minimum=0)
                result = self._reader.list_evidence(
                    task_id,
                    limit=limit,
                    offset=offset,
                )
                return _response(request_id, list(result))

            if method == "get_latest_verification":
                task_id = self._task_id(params, {"task_id"})
                result = self._reader.get_latest_verification(task_id)
                return _response(request_id, result)

            if method == "verify_integrity":
                if params:
                    raise TypeError("verify_integrity does not accept parameters")
                return _response(request_id, {"valid": self._reader.verify_integrity()})

            if method == "get_task_report":
                task_id = self._task_id(
                    params,
                    {"task_id", "evidence_limit", "evidence_offset"},
                )
                evidence_limit = self._page_value(
                    params,
                    "evidence_limit",
                    100,
                    minimum=1,
                    maximum=500,
                )
                evidence_offset = self._page_value(
                    params,
                    "evidence_offset",
                    0,
                    minimum=0,
                )
                result = self._reader.get_task_report(
                    task_id,
                    evidence_limit=evidence_limit,
                    evidence_offset=evidence_offset,
                )
                return _response(request_id, result)

        except TypeError as exc:
            return _error(request_id, -32602, str(exc))
        except KeyError:
            return _error(request_id, -32004, "Task not found")
        except Exception:
            # Do not disclose SQL, stack traces, database paths, or host details.
            return _error(request_id, -32603, "Internal error")

        return _error(request_id, -32601, "Method not found")

    def handle_line(
        self,
        line: str | bytes,
        *,
        max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES,
    ) -> str:
        """Decode and handle one bounded newline-delimited request."""
        if (
            isinstance(max_line_bytes, bool)
            or not isinstance(max_line_bytes, int)
            or max_line_bytes < 1
        ):
            raise ValueError("max_line_bytes must be a positive integer")

        try:
            if isinstance(line, bytes):
                if len(line) > max_line_bytes:
                    return json.dumps(
                        _error(None, -32600, "Request line too large"),
                        separators=(",", ":"),
                    )
                decoded = line.decode("utf-8")
            elif isinstance(line, str):
                if len(line.encode("utf-8")) > max_line_bytes:
                    return json.dumps(
                        _error(None, -32600, "Request line too large"),
                        separators=(",", ":"),
                    )
                decoded = line
            else:
                return json.dumps(
                    _error(None, -32600, "Request line must be text or bytes"),
                    separators=(",", ":"),
                )
            request = _strict_json_loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return json.dumps(
                _error(None, -32700, "Parse error"),
                separators=(",", ":"),
            )

        response = self.handle(request)
        try:
            return json.dumps(
                response,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return json.dumps(
                _error(None, -32603, "Internal error"),
                separators=(",", ":"),
            )


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
    """Serve one JSON-RPC response per input line; never write diagnostics to stdout."""
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
    parser = argparse.ArgumentParser(
        description="Read-only Evidence Engine JSON-RPC stdio bridge"
    )
    parser.add_argument(
        "--db",
        required=True,
        help="path to an existing Evidence Engine SQLite database",
    )
    parser.add_argument(
        "--max-line-bytes",
        type=int,
        default=_DEFAULT_MAX_LINE_BYTES,
        help="maximum request-line size (default: 1048576)",
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
