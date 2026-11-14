"""Trusted host-side adapters for collecting structured evidence."""

from .command import CommandAdapter, CommandExecution, CommandSpec
from .file import FileAdapter, FilePolicy, FileSnapshot
from .junit import JUnitReportAdapter, JUnitReportResult

__all__ = [
    "CommandAdapter",
    "CommandExecution",
    "CommandSpec",
    "FileAdapter",
    "FilePolicy",
    "FileSnapshot",
    "JUnitReportAdapter",
    "JUnitReportResult",
]
