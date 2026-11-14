# Evidence Engine

**Deterministic evidence recording and verification for tool-using assistants.**

Evidence Engine is a small, model-independent foundation for a simple rule:

> A task is not successful because an assistant says it succeeded. It is successful only when predeclared, machine-checkable criteria are supported by recorded evidence.

This project is being developed as a reliability layer for assistants such as Lilith/Hermes. It is deliberately independent of Hermes, WhatsApp, and any particular language model.

## Current scope (v0.1 foundation)

- SQLite-backed task and evidence records.
- Explicit task states: `planned`, `running`, `passed`, `failed`, `unknown`.
- Success criteria are declared before execution and cannot be edited through the normal schema/API after task creation.
- Evidence records are append-only through ordinary SQLite operations.
- A SHA-256 hash chain detects changes to recorded evidence.
- Deterministic verification supports `exists`, `eq`, and `gte` checks.
- Missing or malformed evidence produces `unknown`, not success.
- Contradictory evidence produces failure.
- No runtime dependencies; Python 3.11+.

## What it does not claim

Evidence Engine is **not** a truth machine and does not prove that arbitrary natural-language statements are true. It checks structured observations against criteria that a trusted host has defined in advance.

A hash chain is tamper-evident, not tamper-proof. Someone with direct database and filesystem control can rewrite the entire database and recompute the chain. Stronger deployments should anchor checkpoints outside the database and protect write access.

A `source` label is metadata, not cryptographic authentication. The future Hermes adapter must accept evidence only from trusted host-side adapters, never from free-form model output.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -v
```

Example:

```python
from evidence_engine import Criterion, EvidenceEngine

engine = EvidenceEngine("evidence.sqlite3")
task = engine.create_task(
    "RSS collector integration test",
    [Criterion(
        kind="command_exit",
        field="return_code",
        operator="eq",
        expected=0,
        source="collector-test-runner",
    )],
)
engine.start_task(task.task_id)

# A trusted host-side adapter records the actual process result.
engine.record_evidence(
    task.task_id,
    kind="command_exit",
    source="collector-test-runner",
    payload={"return_code": 0},
)

final_task = engine.finalize_task(task.task_id)
print(final_task.status)  # passed only when the criterion is supported
```

**Security boundary:** the example's `record_evidence` call represents a trusted adapter. Do not expose unrestricted evidence-writing access as an LLM tool. The model should receive read-only task and verification reports.

## Development status

The foundation is intentionally narrow. The first release does not execute shell commands itself, inspect files, parse test reports, or connect to Hermes. Those belong in separately tested host-side adapters and later milestones.

See:
- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)

## License

MIT
