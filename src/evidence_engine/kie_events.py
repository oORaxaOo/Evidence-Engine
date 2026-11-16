"""Publish privacy-bounded Evidence Engine health failures to KIE."""
from __future__ import annotations

import json
from pathlib import Path
import socket
from uuid import uuid4

_MAX_MESSAGE_BYTES = 64 * 1024
_ALLOWED_FAILURE_CODES = frozenset({
    "configuration_missing",
    "policy_invalid",
    "verification_exception",
})


def publish_verification_unavailable(socket_path: str | Path | None, failure_code: str) -> bool:
    """Publish only a stable failure code; no paths, exception text, or user data."""
    if not isinstance(socket_path, (str, Path)) or not str(socket_path).strip():
        return False
    if not isinstance(failure_code, str) or failure_code not in _ALLOWED_FAILURE_CODES:
        return False
    path = Path(socket_path).expanduser()
    if not path.is_absolute():
        return False
    message = {
        "event_id": str(uuid4()),
        "engine_id": "evidence",
        "event_type": "evidence.verification_unavailable",
        "payload": {"failure_code": failure_code},
    }
    try:
        encoded = json.dumps(
            message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_MESSAGE_BYTES:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.15)
            client.connect(str(path))
            client.sendall(encoded + b"\n")
            response = client.makefile("rb").readline(1024)
        return json.loads(response.decode("utf-8")).get("ok") is True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


__all__ = ["publish_verification_unavailable"]
