# Evidence Engine

**Deterministic evidence recording and verification for tool-using assistants.**

Evidence Engine is a model-independent reliability layer for assistants such as Lilith/Hermes. Its central rule is:

> A task is not successful because an assistant says it succeeded. It is successful only when predeclared, machine-checkable criteria are supported by recorded evidence.

It is deliberately independent of Hermes, WhatsApp, and any particular language model.

## Current scope

- SQLite-backed tasks, structured evidence, and verification history.
- Explicit task states: `planned`, `running`, `passed`, `failed`, and `unknown`.
- Success criteria are fixed when a task is created and protected from normal SQL updates.
- Append-only evidence and verification records through SQLite triggers.
- SHA-256 hash chaining for evidence records.
- Deterministic `exists`, `eq`, and `gte` criteria.
- Atomic verification/finalization tied to the latest evidence sequence; later evidence invalidates an older verification.
- Allowlisted command execution with fixed arguments, no shell invocation, timeouts, bounded output monitoring, and output hashes.
- Allowlisted file snapshots with traversal/symlink checks and stable SHA-256 reads. File contents are not stored.
- JUnit XML parsing tied to a command result, matching `--junitxml` path, and report timestamps bounded by the command run.
- Read-only JSON-RPC 2.0 stdio bridge with a fixed method allowlist and bounded request lines.
- No runtime dependencies; Python 3.11+.

## What it does not claim

Evidence Engine is **not** a truth machine. It does not prove arbitrary natural-language statements, the quality of a test suite, or the correctness of a program. It checks structured observations against criteria defined by trusted host-side code or an operator.

A hash chain is tamper-evident, not tamper-proof. Someone with direct database and filesystem control can disable triggers, rewrite records, recompute the chain, or remove its tail. The verification table and task metadata are protected by ordinary SQLite triggers but are not part of the evidence hash chain. Stronger deployments need OS-level access controls and externally anchored, signed checkpoints.

A `source` label is descriptive metadata, not cryptographic authentication. Command and file policies must be created by trusted host code. Do not expose unrestricted `record_evidence`, `create_task`, adapter configuration, or database access as LLM tools. The language model should receive a separate read-only view of verification results.

The command adapter is **not an OS sandbox**. A registered command can still do anything permitted to its operating-system user. The allowlist is only trustworthy when command specifications are operator-controlled and never constructed from model-supplied arguments.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -v
```

Example:

```python
import sys
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine
from evidence_engine.adapters import CommandAdapter, CommandSpec

engine = EvidenceEngine("evidence.sqlite3")
task = engine.create_task(
    "Small local verification",
    [
        Criterion(
            kind="command_result",
            field="return_code",
            operator="eq",
            expected=0,
            source="evidence_engine.command_adapter:version-check",
        )
    ],
)
engine.start_task(task.task_id)

# The complete command is fixed by trusted host configuration.
spec = CommandSpec(
    command_id="version-check",
    argv=(sys.executable, "--version"),
    cwd=Path.cwd(),
)
result = CommandAdapter([spec]).run(engine, task.task_id, "version-check")
final_task = engine.finalize_task(task.task_id)

print(result.return_code)
print(final_task.status)  # passed only if recorded evidence supports the criterion
```

## Development status

The adapters and generic read-only stdio bridge are local host-side components. The bridge is not yet wired into Hermes. OS sandboxing and external signed checkpoints are not implemented. No WhatsApp or Hermes configuration is changed by this repository.

See:
- [Architecture](docs/architecture.md)
- [Adapters](docs/adapters.md)
- [Threat model](docs/threat-model.md)

## License

Not selected yet.
