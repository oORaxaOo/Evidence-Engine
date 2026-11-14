# Architecture

## Design boundary

Evidence Engine is the system of record for task state and structured evidence. A language model may request an approved action and summarize a report, but it is not the authority that decides whether its own actions succeeded.

```text
Trusted operator / host configuration
          |
          v
   Immutable task contract
          |
          v
 Registered command policies -----> Approved process
          |                              |
          | command result               |
          v                              |
 File policy + file snapshots            |
          |                              |
          v                              |
 JUnit parser <---- command evidence ----+
          |
          v
 SQLite evidence ledger
          |
          v
 Deterministic verification
          |
          v
 Read-only result for assistant / user
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

Criteria are supplied by trusted host code or the operator when a task is created. An LLM must not be allowed to weaken or rewrite them after seeing an outcome.

### Evidence ledger

Evidence is structured JSON with a timestamp, kind, descriptive source label, and task identifier. Records are appended to SQLite and linked using SHA-256 hashes.

The evidence hash chain detects changed records or broken links in the chain. SQLite triggers reject ordinary updates/deletes to evidence and verification rows. The chain is tamper-evident, not tamper-proof; direct database access can bypass triggers and rewrite the chain.

### Task states and verification

Task states are `planned`, `running`, `passed`, `failed`, and `unknown`.

Verification supports:
- all criteria supported -> `supported`;
- any criterion contradicted -> `contradicted`;
- missing or invalid evidence -> `inconclusive`;
- broken evidence chain -> `invalid`.

A verification record stores the highest evidence sequence for that task that it evaluated. SQLite prevents a terminal task state unless the latest verification matches the latest evidence sequence. Evidence writes, verification, and finalization use SQLite write transactions so a new observation cannot slip between the final check and the state transition.

No public method accepts a model-provided success flag.

## Trusted adapters

### Command adapter

The operator registers complete `CommandSpec` objects. The caller may select only a registered command ID; it cannot append arguments or change the working directory, timeout, output bound, or environment. The executable path must be absolute and executable. The adapter invokes the process with `shell=False`, blocks common shell executable names, uses an isolated process group on POSIX, enforces a timeout, and monitors combined stdout/stderr against a byte limit.

The ledger stores the command request, exit code, timings, byte counts, and SHA-256 hashes. It does not store raw stdout/stderr. Sensitive argv positions can be explicitly redacted; secret values should preferably be supplied through a trusted environment configuration and never through model-controlled arguments.

This adapter is not a sandbox. A pre-approved program still runs with the operating-system permissions of its host process.

### File adapter

The operator defines one or more allowed filesystem roots and a maximum file size. The adapter accepts relative paths only, rejects parent traversal and escaped symlinks, and records file metadata plus SHA-256 only when a bounded read is stable. File contents are never stored.

### JUnit report adapter

The adapter requires a command-result evidence ID from the same task. The command's registered arguments must contain exactly one `--junitxml` output path matching the requested report file. The report's modification time must be between the command start and finish times.

DTD/entity declarations are rejected. Test counts are recomputed from individual `testcase` elements; declared root summary values are checked for consistency. The raw XML is not stored—only its hash, parsed counts, command reference, and validation outcome.

For a reliable task contract, require separate criteria for:
- command exit code equals zero;
- report parse status is valid;
- test failures equal zero;
- test errors equal zero;
- total tests meet the expected minimum.

The JUnit adapter verifies the report artifact and its relationship to a recorded command. It does not prove that the test suite itself is complete or meaningful.

## Trust boundaries

1. **Operator configuration is trusted.** Task criteria, command policies, and file roots must be created by host-controlled code or an operator.
2. **Tool outputs are observations, not instructions.** stdout/stderr, files, and XML are untrusted data.
3. **The model is not a source of execution truth.** Free-form claims must never be recorded as exit codes, file hashes, test counts, or delivery receipts.
4. **Reports are derived.** The model may summarize a report but must not change its status or evidence.
5. **Write access stays host-side.** A future Hermes integration should expose a separate read-only API to the model, never a raw database handle or unrestricted evidence-writing method.

## Planned work

- Dedicated read-only API for assistant access.
- Stronger OS-level process isolation and resource limits.
- External signed checkpoints for the evidence chain and verification state.
- Hermes bridge with host-controlled task contracts and command/file policies.
