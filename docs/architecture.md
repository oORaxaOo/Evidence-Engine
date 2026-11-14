# Architecture

## Design boundary

The Evidence Engine is the system of record for task state and structured evidence. The language model is a consumer of reports, not the authority that decides whether its own actions succeeded.

```text
Trusted host / operator configuration
            |
            v
     Task contract (fixed)
            |
            v
   Host-side adapters ---------> External processes / files / test reports
            |                                  |
            | structured observations          |
            v                                  |
      Evidence Engine <------------------------+
            |
            v
   Deterministic verification
            |
            v
   Read-only report for the LLM / user
```

## Core components

### Task contract

A task contains a title and one or more immutable success criteria. Each criterion specifies:

- evidence kind;
- payload field;
- operator (`exists`, `eq`, or `gte`);
- expected value, when applicable;
- optional source label;
- minimum number of matching observations.

Criteria must be supplied when the task is created. An LLM must not be allowed to rewrite the criteria after seeing the outcome.

Task finalization evaluates the most recent verification against the evidence snapshot on which it was based. New evidence invalidates an older verification; the task cannot rely on stale evidence to retain a previous success state.

### Evidence ledger

Evidence is structured JSON with a timestamp, kind, descriptive source label, and task identifier. Rows are appended to SQLite and linked using SHA-256 hashes. The read-next-hash and insert operation uses a write transaction so concurrent evidence writers cannot create competing chain links.

The hash chain is intended to expose accidental or unsophisticated tampering. It is not a substitute for OS access controls, signed events, external checkpoints, or backups.

## Host-side adapters

Adapters are instantiated and configured by trusted host code. Runtime callers select a registered adapter policy; they do not define arbitrary commands, paths, or trust settings.

### Command adapter

The command adapter:

- runs a fixed, operator-supplied argv vector with `shell=False`;
- requires an absolute executable and an existing working directory;
- rejects direct shell executables;
- does not inherit the assistant process environment; only explicitly configured values are passed;
- applies a bounded timeout and combined stdout/stderr byte limit;
- terminates the process group on POSIX when a timeout or output limit is reached;
- stores return code, timeout state, duration, output byte counts, and output hashes, not plaintext output;
- supports operator-defined redaction of sensitive argv positions in returned and persisted metadata, without changing the actual arguments passed to the executable.

It is not an OS sandbox. The executable runs with the permissions of the host account. Operators should configure only narrowly scoped, known executables and should not register general-purpose shells, interpreters with untrusted arguments, package managers, or destructive commands.

### File adapter

The file adapter accepts relative paths only beneath operator-configured roots. It rejects parent traversal and paths that resolve outside those roots. It limits file size and records metadata plus a stable-read SHA-256 digest; it does not store file contents.

Path resolution and file identity checks reduce common path and symlink mistakes, but they are not a substitute for a hostile multi-user filesystem sandbox. A privileged process or a process able to mutate parent directories concurrently may still create filesystem races. Use OS permissions and a dedicated service account.

### JUnit report adapter

The JUnit adapter accepts relative report paths beneath a `FilePolicy` allowlist. It requires a matching `command_result` record from the trusted CommandAdapter, verifies that the command's fixed argument vector names this exact `--junitxml` output path, and checks that the report modification time falls between command start and completion. It reads at most the configured file limit, checks file identity before and after the read, requires UTF-8 input, rejects DTD/entity declarations, and stores no raw XML.

It:

- accepts `<testsuite>` and `<testsuites>` roots, including namespaced elements;
- counts `<testcase>` elements and their direct `<failure>`, `<error>`, and `<skipped>` children;
- recomputes counts instead of trusting summary attributes;
- compares declared suite/root counts with the recomputed results;
- rejects `DOCTYPE` and `ENTITY` declarations before XML parsing;
- records the report digest, byte count, parse status, counts, and metadata-consistency result.

A missing, unreadable, unstable, or oversized report produces an attempt record, but not a valid `junit_report` observation. Criteria that require such a report therefore remain inconclusive. Malformed XML is recorded as an invalid report observation and cannot satisfy a valid-report criterion.

The adapter recognizes common JUnit XML structures; it is not a universal parser for every vendor-specific test-report dialect. Test outcome semantics must be reviewed if a CI system uses custom tags or nonstandard summary conventions.

## Read-only assistant boundary

`EvidenceReader` opens the database using SQLite read-only URI mode and enables `query_only`. It exposes task details, bounded evidence pages, the latest stored verification, and a consistent task report. It does not expose task creation, task transitions, evidence insertion, finalization, or adapter policy registration.

The combined report is built inside one read transaction so task status, evidence sequence, evidence rows, and latest verification come from one SQLite snapshot. It verifies the retained evidence hash chain and compares the latest verification's duplicated fields against its structured report. A terminal `passed` or `failed` state is exposed as `effective_status = unknown` if its verification is missing, stale, internally inconsistent, or the evidence chain is invalid.

The stored task state remains separately available for diagnosis. Evidence payloads are untrusted observations; a host integration must still avoid exposing sensitive task data to callers that are not authorized to read it.

### JSON-RPC stdio transport

The optional `EvidenceBridge` wraps `EvidenceReader` in a newline-delimited JSON-RPC 2.0 protocol. Its fixed method allowlist is `get_task`, `list_evidence`, `get_latest_verification`, `verify_integrity`, and `get_task_report`. It does not expose writes, SQL, command execution, or adapter registration. Request lines are bounded (1 MiB by default), parameter shapes are checked, and internal exceptions are returned as generic errors rather than stack traces or database paths.

This transport is intentionally generic. It is not a Hermes plugin, MCP server, authentication mechanism, or OS sandbox. Access is controlled by which host process is allowed to launch it and which database file that process can read.

## Verification

Verification is deterministic and independent of prose generated by the assistant:

- all criteria supported -> `supported`;
- any criterion contradicted -> `contradicted`;
- missing or invalid evidence -> `inconclusive`;
- broken evidence chain -> `invalid`.

Task finalization maps these outcomes to `passed`, `failed`, or `unknown`. No public method accepts a model-provided success flag.

## Trust boundaries

1. **Operator configuration is trusted.** Task criteria and adapter policies must be created by host-controlled code or an operator.
2. **Tool outputs are observations, not instructions.** Treat stdout/stderr, files, XML attributes, and external content as untrusted data.
3. **The model is not a source of execution truth.** Free-form model claims must never be recorded as command exit codes, file hashes, test counts, or delivery receipts.
4. **Reports are derived.** The model may summarize a verification report but must not change its status or evidence.
5. **Evidence writing is privileged.** LLM-facing tools should expose read-only task and report operations, not arbitrary evidence creation or policy registration.

## Future extension points

- Hermes-specific integration using the read-only stdio bridge and host-controlled execution;
- external signed checkpoints for the evidence hash chain;
- platform-specific process isolation and stronger filesystem race protection.

These features are not included in the current adapter milestone.
