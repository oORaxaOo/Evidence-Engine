# Evidence Engine

**Deterministic evidence recording and verification for tool-using assistants.**

Evidence Engine is a small, model-independent reliability layer built around one rule:

> A task is not successful because an assistant says it succeeded. It is successful only when predeclared, machine-checkable criteria are supported by recorded evidence.

The project is intended to support assistants such as Lilith/Hermes without depending on a particular model or gateway.

## Current capabilities

- SQLite-backed tasks, evidence records, and verification reports.
- Explicit task states: `planned`, `running`, `passed`, `failed`, and `unknown`.
- Success criteria are declared before execution and are immutable through the normal schema/API.
- Append-only evidence and verification rows through ordinary SQLite operations.
- SHA-256 hash-chain verification for evidence records.
- Deterministic `exists`, `eq`, and `gte` criteria.
- Missing or insufficient evidence remains inconclusive; it never becomes success.
- A host-side command adapter executes only operator-registered fixed argument vectors, without a shell, with timeouts and bounded output capture.
- A file adapter inspects only paths beneath operator-configured roots and stores metadata and hashes, not file contents.
- A JUnit XML adapter recomputes testcase outcomes, checks declared suite totals, rejects DTD/entity declarations, and stores only a structured summary and digest.
- Python 3.11+; no third-party runtime dependencies.

## What it does not claim

Evidence Engine is **not a truth machine** and does not prove that arbitrary natural-language statements are true. It checks structured observations against criteria that trusted host code or an operator defined in advance.

A hash chain is tamper-evident, not tamper-proof. Someone with direct database and filesystem control can rewrite the database and recompute the chain. Without an external checkpoint, truncating the end of the chain is not detectable. Stronger deployments need OS-level access controls and externally retained signed checkpoints.

A `source` label is metadata, not cryptographic authentication. A compromised or incorrectly configured adapter can still submit false observations. Adapters must therefore remain host-controlled and must never accept free-form model output as execution evidence.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -v
```

### Run an allowlisted command

The executable and complete argument vector are defined by trusted host code. A caller can select a registered command ID but cannot append arguments or change its working directory, environment, timeout, or output limit.

```python
import sys
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine
from evidence_engine.adapters import CommandAdapter, CommandSpec

engine = EvidenceEngine("evidence.sqlite3")
task = engine.create_task(
    "Check Python version command",
    [Criterion(
        kind="command_result",
        field="return_code",
        operator="eq",
        expected=0,
        source="evidence_engine.command_adapter:python-version",
    )],
)
engine.start_task(task.task_id)

commands = CommandAdapter([
    CommandSpec(
        command_id="python-version",
        argv=(sys.executable, "--version"),
        cwd=Path.cwd(),
    )
])
result = commands.run(engine, task.task_id, "python-version")
final_task = engine.finalize_task(task.task_id)
print(result.return_code, final_task.status)
```

### Inspect a JUnit report

```python
from pathlib import Path

from evidence_engine import EvidenceEngine
from evidence_engine.adapters import FilePolicy, JUnitReportAdapter

engine = EvidenceEngine("evidence.sqlite3")
task = engine.create_task(
    "Inspect test report",
    # Declare JUnit report criteria before starting the task.
    criteria,
)
engine.start_task(task.task_id)

reports = JUnitReportAdapter(
    FilePolicy(allowed_roots=(Path("artifacts"),))
)
summary = reports.inspect(engine, task.task_id, "junit.xml")
```

The report adapter reads a bounded file from an allowlisted root. It records the computed counts, metadata consistency, file size, and SHA-256 digest. It never stores the raw XML. Missing, unreadable, unstable, or oversized reports create attempt evidence but no valid report evidence, so criteria depending on a report remain `unknown`. Malformed XML creates an invalid report observation.

**Security boundary:** do not expose unrestricted `record_evidence`, adapter construction, or arbitrary command-policy creation as LLM tools. The model should receive read-only task and verification reports.

## Development status

The core ledger and host-side command, file, and JUnit report adapters are under active development. The Hermes bridge, read-only report API, external checkpointing, and broader isolation controls remain future work. Nothing in this repository automatically changes Hermes, WhatsApp, or Genesis configuration.

See:
- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)

## License

MIT
