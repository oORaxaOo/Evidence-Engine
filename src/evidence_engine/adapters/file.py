"""Allowlisted file inspection that records hashes and metadata, never contents."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..engine import EvidenceEngine, TaskStatus


_MAX_FILE_SIZE_BYTES = 16 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024
_SOURCE = "evidence_engine.file_adapter"


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@dataclass(frozen=True, slots=True)
class FilePolicy:
    """Operator-owned filesystem boundary for relative file inspections."""

    allowed_roots: tuple[Path, ...]
    max_file_size_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        roots: list[Path] = []
        for item in tuple(self.allowed_roots):
            try:
                root = Path(item).expanduser().resolve(strict=True)
            except (OSError, TypeError) as exc:
                raise ValueError(f"allowed root is not available: {exc}") from exc
            if not root.is_dir():
                raise ValueError("every allowed root must be an existing directory")
            if root not in roots:
                roots.append(root)
        if not roots:
            raise ValueError("at least one allowed root is required")
        object.__setattr__(self, "allowed_roots", tuple(roots))

        if (
            isinstance(self.max_file_size_bytes, bool)
            or not isinstance(self.max_file_size_bytes, int)
            or not 1 <= self.max_file_size_bytes <= _MAX_FILE_SIZE_BYTES
        ):
            raise ValueError(
                f"max_file_size_bytes must be between 1 and {_MAX_FILE_SIZE_BYTES}"
            )

    def resolve(self, relative_path: str) -> tuple[Path, Path]:
        """Resolve a relative target and reject traversal or escaped symlinks."""
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("relative_path must be a non-empty string")
        if "\\x00" in relative_path or "\\\\" in relative_path:
            raise ValueError("relative_path contains an invalid character")
        candidate_path = Path(relative_path)
        if candidate_path.is_absolute() or ".." in candidate_path.parts:
            raise ValueError("absolute paths and parent traversal are not allowed")

        for root in self.allowed_roots:
            candidate = root / candidate_path
            try:
                resolved = candidate.resolve(strict=False)
            except OSError:
                continue
            if resolved.is_relative_to(root):
                return root, resolved
        raise PermissionError("requested path is outside all allowed roots")


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Result of a bounded file inspection."""

    relative_path: str
    resolved_path: str
    root: str
    exists: bool
    is_file: bool | None
    size_bytes: int | None
    modified_at_ns: int | None
    sha256: str | None
    stable_read: bool | None
    inspection_error: str | None
    evidence_id: str


class FileAdapter:
    """Inspect only paths inside an operator-configured set of filesystem roots."""

    def __init__(self, policy: FilePolicy):
        if not isinstance(policy, FilePolicy):
            raise TypeError("policy must be a FilePolicy")
        self.policy = policy

    def inspect(
        self,
        engine: EvidenceEngine,
        task_id: str,
        relative_path: str,
    ) -> FileSnapshot:
        """Record existence, file metadata, and a stable SHA-256 digest if safe."""
        task = engine.get_task(task_id)
        if task.status is not TaskStatus.RUNNING:
            raise ValueError("files can only be inspected for a running task")

        root, resolved = self.policy.resolve(relative_path)
        exists = resolved.exists()
        is_file: bool | None = None
        size_bytes: int | None = None
        modified_at_ns: int | None = None
        digest_value: str | None = None
        stable_read: bool | None = None
        inspection_error: str | None = None

        if exists:
            try:
                before_path = resolved.stat()
                is_file = stat.S_ISREG(before_path.st_mode)
                size_bytes = before_path.st_size
                modified_at_ns = before_path.st_mtime_ns

                if is_file:
                    if before_path.st_size > self.policy.max_file_size_bytes:
                        stable_read = False
                        inspection_error = (
                            "file exceeds configured size limit; content was not read"
                        )
                    else:
                        digest = hashlib.sha256()
                        bytes_read = 0
                        with resolved.open("rb") as handle:
                            before_fd = os.fstat(handle.fileno())
                            if _stat_identity(before_fd) != _stat_identity(before_path):
                                stable_read = False
                                inspection_error = "file changed before reading began"
                            else:
                                while True:
                                    chunk = handle.read(_CHUNK_SIZE)
                                    if not chunk:
                                        break
                                    bytes_read += len(chunk)
                                    if bytes_read > self.policy.max_file_size_bytes:
                                        stable_read = False
                                        inspection_error = (
                                            "file exceeded configured size limit while reading"
                                        )
                                        break
                                    digest.update(chunk)

                                after_fd = os.fstat(handle.fileno())

                        if stable_read is not False:
                            try:
                                after_path = resolved.stat()
                                stable_read = (
                                    _stat_identity(before_fd) == _stat_identity(after_fd)
                                    and _stat_identity(after_fd) == _stat_identity(after_path)
                                    and bytes_read == after_fd.st_size
                                    and resolved.is_relative_to(root)
                                )
                            except OSError:
                                stable_read = False

                            if stable_read:
                                digest_value = digest.hexdigest()
                            else:
                                inspection_error = "file changed during inspection; digest withheld"
                else:
                    stable_read = None
            except FileNotFoundError:
                exists = False
                is_file = None
                size_bytes = None
                modified_at_ns = None
            except OSError as exc:
                stable_read = False
                inspection_error = f"{type(exc).__name__}: {exc}"

        payload = {
            "relative_path": relative_path,
            "resolved_path": str(resolved),
            "root": str(root),
            "exists": exists,
            "is_file": is_file,
            "size_bytes": size_bytes,
            "modified_at_ns": modified_at_ns,
            "sha256": digest_value,
            "stable_read": stable_read,
            "inspection_error": inspection_error,
            "content_stored": False,
        }
        evidence_id = engine.record_evidence(
            task_id,
            kind="file_snapshot",
            source=_SOURCE,
            payload=payload,
        )
        return FileSnapshot(
            relative_path=relative_path,
            resolved_path=str(resolved),
            root=str(root),
            exists=exists,
            is_file=is_file,
            size_bytes=size_bytes,
            modified_at_ns=modified_at_ns,
            sha256=digest_value,
            stable_read=stable_read,
            inspection_error=inspection_error,
            evidence_id=evidence_id,
        )
