# Trusted adapters

Adapters turn real host-side observations into structured evidence. They are not intended to be exposed as unrestricted tools to a language model.

## Command adapter

A `CommandSpec` is an operator-owned policy. The complete argv, working directory, timeout, output limit, and environment are fixed when the adapter is constructed. The caller can select a registered `command_id`, but cannot append arguments or change execution settings.

```python
from pathlib import Path
from evidence_engine.adapters import CommandAdapter, CommandSpec

spec = CommandSpec(
    command_id="pytest",
    argv=(
        "/absolute/path/to/pytest",
        "-q",
        "--junitxml=artifacts/junit.xml",
        "tests/",
    ),
    cwd=Path("/srv/project"),
    timeout_seconds=45,
    max_output_bytes=256 * 1024,
)

command_adapter = CommandAdapter([spec])
result = command_adapter.run(engine, task_id, "pytest")
```

The executable path must be absolute and executable. Common shell executable names are rejected and the process is launched with `shell=False`. The process receives only the environment explicitly declared in the `CommandSpec`; host environment variables are not inherited automatically.

The adapter stores the command request and final process result: request ID, registered command ID, argv (with configured argument indexes redacted), working directory, start/finish timestamps, duration, exit code, timeout/output-limit flags, stdout/stderr byte counts, and SHA-256 digests. Raw stdout and stderr are never stored.

Sensitive command-line values should be avoided. Prefer a trusted, explicitly configured environment variable where possible. When an argument must contain a sensitive value, set `redacted_argument_indexes` to its zero-based argv index. Do not redact the `--junitxml` path when the JUnit adapter needs to correlate it.

**Important:** this is an allowlist, not a sandbox. A registered executable runs with the host process's operating-system permissions. Review every command specification as privileged configuration. Do not construct `CommandSpec` objects from model output.

## File adapter

A `FilePolicy` defines the filesystem roots and maximum file size that may be inspected.

```python
from pathlib import Path
from evidence_engine.adapters import FileAdapter, FilePolicy

file_adapter = FileAdapter(
    FilePolicy(
        allowed_roots=(Path("/srv/project"),),
        max_file_size_bytes=1024 * 1024,
    )
)
snapshot = file_adapter.inspect(engine, task_id, "artifacts/junit.xml")
```

The requested path must be relative. Parent traversal, absolute paths, NULs, backslashes, and symlinks resolving outside an allowed root are rejected. The adapter records existence, file type, size, modification time, and a SHA-256 digest only when a bounded read is stable. It does not store file contents.

A missing file is recorded as a missing observation. A file that is too large, unreadable, or unstable is recorded without a digest, so a success criterion requiring a digest cannot pass.

## JUnit report adapter

The JUnit adapter is deliberately stricter than a generic XML parser. It requires the evidence ID of a command result from the same task. It then verifies:
1. the command actually started and did not time out or exceed the output limit;
2. the command argv contains exactly one `--junitxml` output path;
3. that path matches the requested report path inside the allowlisted root;
4. the report modification time falls between the command's recorded start and finish;
5. the file is stable and within the configured size limit;
6. the XML has a supported root, contains test cases, and contains no DTD/entity declarations;
7. testcase outcomes agree with any count attributes declared on the XML root.

```python
from pathlib import Path
from evidence_engine.adapters import FilePolicy, JUnitReportAdapter

junit_adapter = JUnitReportAdapter(
    FilePolicy(allowed_roots=(Path("/srv/project"),))
)
report = junit_adapter.inspect(
    engine,
    task_id,
    "artifacts/junit.xml",
    command_evidence_id=result.evidence_id,
)
```

The adapter recomputes `tests_total`, `tests_passed`, `tests_failed`, `tests_errors`, and `tests_skipped` from testcase elements. It stores only the XML SHA-256 digest, the structured counts, the command evidence reference, and validation metadata—not the raw XML.

A report that predates the command or is modified after command completion is recorded as inconclusive. Malformed XML, unsupported roots, DTD/entity declarations, or contradictory declared counts are invalid. A valid report is not equivalent to a passing test run: the command exit code and test counts must be separate task criteria.

For a pytest task, useful criteria include:

- `command_result.return_code == 0` from the expected command source;
- `junit_report.parse_status == "valid"`;
- `junit_report.tests_failed == 0`;
- `junit_report.tests_errors == 0`;
- `junit_report.tests_total >= 1`.

The JUnit adapter proves that a structured report was present at the expected path during the recorded command interval and that its counts are internally consistent. It cannot prove that the test suite is complete, meaningful, or free from deliberate test manipulation.

## Host/model boundary

The trusted host should own:
- task creation and success criteria;
- command and file policies;
- actual command execution and evidence writing;
- database access and schema migrations.

A language model should receive only a read-only report interface. Do not expose raw database connections, `record_evidence`, adapter construction, or policy mutation as model tools.


## Read-only assistant access

The `EvidenceReader` façade opens an existing database with SQLite `mode=ro` and `query_only`. It exposes:
- `get_task(task_id)`;
- `list_evidence(task_id)`;
- `get_latest_verification(task_id)`;
- `verify_integrity()`.

It has no task creation, status transition, evidence submission, or finalization methods. A host integration should wrap these methods in narrow read-only tools rather than exposing the reader's private connection method or any `EvidenceEngine` writer object.


## Read-only JSON-RPC stdio bridge

The generic bridge reads one JSON-RPC 2.0 request per input line and writes one JSON response per output line. It exposes only:
- `get_task`;
- `list_evidence`;
- `get_latest_verification`;
- `verify_integrity`;
- `get_task_report` (a combined read-only snapshot).

Example request:

```json
{"jsonrpc":"2.0","id":1,"method":"get_task_report","params":{"task_id":"TASK_ID"}}
```

Start it from the repository root:

```bash
PYTHONPATH=src python -m evidence_engine.bridge --db /path/to/evidence.sqlite3
```

The default maximum input line is 1 MiB. Oversized lines are rejected and discarded before the next request is read. The bridge does not support notifications, arbitrary SQL, command execution, policy changes, or any write method. It keeps protocol responses on stdout; startup diagnostics go to stderr.

This is a generic stdio transport, not a Hermes plugin or a complete MCP server. The host must decide which process can launch it, which database it can read, and whether the assistant is allowed to invoke it. Do not expose the underlying EvidenceEngine writer or raw database path as another tool.
