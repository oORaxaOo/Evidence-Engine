# Threat model

## Assets

- task contracts and success criteria;
- evidence records and verification history;
- terminal task state;
- command specifications and file-root policies;
- integrity of JUnit report summaries.

## Threats addressed

| Threat | Mitigation |
|---|---|
| Model claims a task passed without running it | Status is derived from structured evidence, never from model prose |
| Model omits evidence | Missing evidence yields `unknown` |
| Model adds evidence after a successful check | Finalization is atomic and bound to the latest per-task evidence sequence |
| Model supplies arbitrary command arguments | Callers select only operator-registered command IDs |
| Command output contains secrets | Raw stdout/stderr are not stored; argv indexes can be redacted |
| Command runs too long or emits excessive output | Configured timeout and combined output-byte monitoring |
| File path escapes an approved root | Relative-path-only policy, parent traversal rejection, symlink resolution checks |
| File changes while being hashed | Before/after file identity and metadata are compared; unstable reads withhold the digest |
| JUnit XML contains DTD/entity declarations | Such declarations are rejected before XML parsing |
| JUnit summary attributes lie about testcase content | Counts are recomputed from testcase elements and root summary attributes are checked |
| Ordinary SQL UPDATE/DELETE alters evidence | SQLite triggers reject ordinary changes |
| Evidence payload is modified | SHA-256 hash-chain verification detects a changed record or broken link |

## Out of scope / residual risks

- **Privileged database attacker:** an actor with write access to the database file can disable triggers, alter task/verification rows, rewrite the full evidence chain, or remove its tail. Verification rows are append-only through ordinary triggers but are not yet included in the evidence hash chain. Use OS permissions and external signed checkpoints for stronger guarantees.
- **False or compromised adapter:** the engine can verify that a submitted structured result matches a criterion; it cannot independently know whether a malicious or compromised adapter lied about the outside world.
- **Command policy abuse:** a registered executable runs with the privileges of the host process. The command adapter is not a sandbox; a dangerous or compromised command specification remains dangerous. Process-group termination cannot guarantee that a deliberately detached descendant is stopped.
- **Model-controlled policy:** if an LLM can construct `CommandSpec`, `FilePolicy`, task criteria, or call `record_evidence` directly, the trust boundary is broken. Those capabilities must remain in host-controlled code.
- **Spoofed source labels:** the `source` field is descriptive metadata, not an identity credential.
- **Forged reports by a compromised host:** JUnit report-path and timestamp checks make accidental/stale report reuse harder, but a compromised local process can still fabricate files and timestamps.
- **Natural-language truth:** the engine does not establish factual truth of arbitrary text, summaries, or news analysis.
- **Host compromise:** a compromised host can alter code, policies, clocks, and evidence before they reach the ledger.
- **Incomplete criteria or tests:** poorly designed criteria or a weak test suite can produce misleading assurance even when mechanically satisfied.

## Required integration rules

- Never expose arbitrary evidence creation as an unrestricted LLM tool.
- Keep task contracts and execution policies outside model control.
- Expose only a dedicated read-only view to the model.
- Record actual tool outputs at the trusted adapter boundary.
- Treat missing, ambiguous, malformed, stale, or unstable data as inconclusive or invalid.
- Do not use `passed` as a synonym for “the model says it worked.”
- Do not describe the engine as a truth machine or security sandbox.
