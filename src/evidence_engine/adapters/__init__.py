"""Trusted host-side adapters for collecting structured evidence."""

from .command import CommandAdapter, CommandPolicy, CommandResult

__all__ = ["CommandAdapter", "CommandPolicy", "CommandResult"]
