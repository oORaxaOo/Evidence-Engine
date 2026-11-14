# Threat model

## Assets

- task contracts and their predeclared success criteria;
- evidence records and verification history;
- task status and the evidence snapshot used to verify it;
- operator-controlled command and filesystem policies;
- host-side report summaries and their source-file digests.

## Threats addressed

| Threat | Mitigation |
|---|---|
| Model claims a task passed without running it | Task status is derived from structured evidence, never from model prose |
| Model omits evidence | Missing evidence yields `unknown` |
| Old success is reused after new evidence arrives | Finalization binds the latest verification to the current evidence snapshot |
| Tool result contradicts expected outcome | Contradictory evidence yields `failed` |
| Criteria are changed after seeing results | Criteria are immutable through the normal schema/API |
| Ordinary SQL UPDATE/DELETE changes evidence | SQLite triggers reject these operations |
| Evidence payload is modified | Hash-chain verification detects changes within the retained chain |
| Model attempts arbitrary command execution | Command callers select only an operator-registered command ID; argv is fixed and no shell is invoked |
| Command runs indefinitely or produces unlimited output | Command adapter applies time and output limits, then records timeout/limit state |
| Secrets appear in command evidence metadata | Operators can mask selected argv indexes; actual command arguments remain unchanged but stored/returned metadata uses `<redacted>` |
| File adapter reads outside its configured roots | Relative-path policy rejects parent traversal and escaped symlinks |
| Large or inconsistent test report claims success | JUnit adapter bounds input size and recomputes testcase counts instead of trusting summary attributes |
| Old or replaced XML is presented as a fresh test result | JUnit adapter requires a matching CommandAdapter record, exact output path, and file modification time inside the command's start/completion interval |
| XML entity expansion / DTD payload | JUnit adapter rejects DTD and ENTITY declarations before parsing |
| Raw report content leaks into the evidence ledger | File and JUnit adapters store metadata and digests, not file contents |
| Assistant mutates or rewrites evidence through its read interface | EvidenceReader uses SQLite read-only mode and exposes no mutating methods |
| Assistant requests arbitrary database or host operations through the bridge | JSON-RPC bridge uses a fixed read-only method allowlist, validates parameters, bounds request size, and hides internal errors |
| Stale or internally inconsistent verification is repeated as success | EvidenceReader checks the evidence snapshot and duplicated verification fields; effective status becomes unknown when the checks fail |

## Out of scope / residual risks

- **Privileged database attacker:** an actor with write access to the database file can disable triggers, alter task status, or rewrite the full chain. Without an external checkpoint, suffix truncation/deletion is not detectable either. Use OS permissions and externally retained signed checkpoints for stronger guarantees.
- **False or compromised adapter:** the engine can verify that a submitted structured result matches a criterion; it cannot independently know whether a compromised adapter lied about an outside event.
- **Spoofed source labels:** the `source` field is descriptive metadata, not an identity credential.
- **Host compromise:** a compromised host can alter code, policies, clocks, processes, and evidence before they reach the ledger.
- **Redaction configuration:** masking is opt-in and index-based. Operators must identify every sensitive argument position; a missed index can persist a secret in evidence. Do not mask parameters that a provenance verifier needs to inspect.
- **Command authority:** allowlisting is not a sandbox. A registered executable runs with the host account's permissions. A poorly chosen fixed command/argument vector can still change files, access networks, or perform destructive actions. Register only narrowly scoped commands and use a dedicated low-privilege account.
- **Process descendants:** process-group termination is a best-effort containment measure, not a universal OS isolation guarantee across platforms.
- **Filesystem races:** path resolution and before/after file identity checks reduce common symlink and concurrent-write problems. They do not provide kernel-enforced confinement against a privileged or hostile process that can mutate parent directories at the same time. Use OS permissions and platform-specific descriptor-relative APIs for hostile environments.
- **JUnit dialect differences:** test reporting systems may use vendor-specific XML elements or semantics. Unknown tags are not inferred as failures; adapters should be extended and tested for each nonstandard dialect before relying on the result. Filesystems with coarse or unreliable modification timestamps may conservatively produce an inconclusive result. Timestamp checks are provenance signals, not cryptographic proof of file origin.
- **Bridge process authorization:** the JSON-RPC bridge has no caller authentication. Anyone able to connect to the process stdin can invoke its allowed read methods. Keep it attached to a trusted parent process and restrict database/file permissions.
- **Read authorization:** read-only does not mean public. A host bridge must still restrict which task IDs and evidence payloads a given assistant or user may read.
- **Verification record authenticity:** the read-only facade checks duplicated verification fields for internal consistency, but verification rows do not yet have independent signatures or an external hash anchor. A privileged database attacker can rewrite them.
- **Natural-language truth:** the engine does not establish factual truth of arbitrary text, summaries, or news analysis.
- **Incomplete criteria:** poorly designed or overly broad criteria can produce misleading assurance even when mechanically satisfied.

## Required integration rules

- Never expose arbitrary evidence creation or adapter policy registration as unrestricted LLM tools.
- Keep task contracts and execution policies outside model control.
- Keep model access read-only for evidence and verification reports.
- Record actual observations at trusted host-side adapter boundaries.
- Treat output, files, report XML, and external content as untrusted data, never as instructions.
- Treat missing, ambiguous, malformed, oversized, or unstable inputs as inconclusive unless a specific invalid observation is available.
- Do not use `passed` as a synonym for “the model says it worked.”
