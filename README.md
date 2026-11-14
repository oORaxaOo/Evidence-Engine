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
- A JUnit XML adapter recomputes testcase outcomes, checks declared suite totals, rejects unsafe XML encodings and DTD/entity declarations, and stores only a structured summary and digest.
- A read-only EvidenceReader opens SQLite in read-only mode, returns bounded evidence pages and consistent task reports, checks the evidence hash chain, and refuses to present stale or inconsistent verification as effective success.
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

The executable and complete argument vector are defined by trusted host code. A caller can select a registered command ID but cannot append arguments or change its working directory, environment, timeout, or output limit. Operators can designate sensitive argv positions with `redacted_argument_indexes`; the command still receives the original values, but the adapter's result object and persisted evidence contain `<redacted>` at those positions. Indexes include argv[0]. Never redact an argument that a downstream verifier needs to compare, such as the `--junitxml` report path.

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
import sys
from pathlib import Path

from evidence_engine import Criterion, EvidenceEngine
from evidence_engine.adapters import (
    CommandAdapter,
    CommandSpec,
    FilePolicy,
    JUnitReportAdapter,
)

artifacts = Path("artifacts").resolve()
artifacts.mkdir(exist_ok=True)
engine = EvidenceEngine("evidence.sqlite3")
task = engine.create_task(
    "Inspect test report",
    [
        Criterion("command_result", "process_started", "eq", True, "evidence_engine.command_adapter:pytest"),
        Criterion("command_result", "timed_out", "eq", False, "evidence_engine.command_adapter:pytest"),
        Criterion("command_result", "return_code", "eq", 0, "evidence_engine.command_adapter:pytest"),
        Criterion("junit_report", "report_valid", "eq", True, "evidence_engine.junit_report_adapter"),
        Criterion("junit_report", "metadata_consistent", "eq", True, "evidence_engine.junit_report_adapter"),
        Criterion("junit_report", "failures", "eq", 0, "evidence_engine.junit_report_adapter"),
        Criterion("junit_report", "errors", "eq", 0, "evidence_engine.junit_report_adapter"),
    ],
)
engine.start_task(task.task_id)

# This fixed operator-defined command creates the report. Callers cannot
# append arbitrary arguments or change its output path at runtime.
commands = CommandAdapter([
    CommandSpec(
        command_id="pytest",
        argv=(sys.executable, "-m", "pytest", "--junitxml=artifacts/junit.xml"),
        cwd=Path.cwd(),
    )
])
command_result = commands.run(engine, task.task_id, "pytest")

reports = JUnitReportAdapter(FilePolicy(allowed_roots=(artifacts,)))
summary = reports.inspect(
    engine,
    task.task_id,
    "junit.xml",
    command_evidence_id=command_result.evidence_id,
)
final_task = engine.finalize_task(task.task_id)
```

The report adapter reads a bounded UTF-8 XML file from an allowlisted root. It requires a preceding trusted `CommandAdapter` result, an exact match to that command's fixed `--junitxml` output path, and a file modification time between the command's recorded start and completion. It records recomputed counts, metadata consistency, file size, and SHA-256 digest, never raw XML. DTD/entity declarations and non-UTF-8 XML are rejected. Missing, unreadable, unstable, oversized, stale, or post-command-modified reports create attempt evidence but no report observation, so report criteria remain `unknown`. Malformed or unsafe XML creates an invalid report observation.

**Security boundary:** do not expose unrestricted `record_evidence`, adapter construction, or arbitrary command-policy creation as LLM tools. The model should receive read-only task and verification reports.


### Read-only assistant view

The LLM-facing side should receive an `EvidenceReader`, never the writer or unrestricted adapter objects.

```python
from evidence_engine import EvidenceReader

reader = EvidenceReader("evidence.sqlite3")
report = reader.get_task_report(task_id)

print(report["effective_status"])
print(report["verification_current"])
print(report["ledger_integrity"])
```

The returned report is a read-only snapshot of the task, current evidence, and newest verification. Evidence is paginated (maximum 500 records per page). If a verification is stale, inconsistent with its stored report, or the evidence chain is invalid, a terminal success is downgraded to `unknown` in `effective_status`. The original stored task status remains visible separately in `report["task"]["status"]`.

## Development status

The core ledger, host-side command/file/JUnit adapters, and read-only EvidenceReader facade are implemented on the development branch. A Hermes bridge, external signed checkpoints, and broader process/filesystem isolation remain future work. Nothing in this repository automatically changes Hermes, WhatsApp, or Genesis configuration.

See:
- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)

## License

MIT
