"""Trusted host-side adapters for collecting structured evidence."""

from .command import CommandAdapter, CommandExecution, CommandSpec
from .file import FileAdapter, FilePolicy, FileSnapshot

__all__ = [
    "CommandAdapter",
    "CommandExecution",
    "CommandSpec",
    "FileAdapter",
    "FilePolicy",
    "FileSnapshot",
]
