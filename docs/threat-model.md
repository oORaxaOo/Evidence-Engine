# Threat model

## Assets

- task contracts and their success criteria;
- evidence records and verification history;
- the integrity of task status;
- operator-controlled adapter policies.

## Threats addressed

| Threat | v0.1 mitigation |
|---|---|
| Model claims a task passed without running it | Status is derived from structured evidence, never from model prose |
| Model omits evidence | Missing evidence yields `unknown` |
| Tool result contradicts expected outcome | Contradictory evidence yields `failed` |
| Criteria are changed after seeing results | Criteria are immutable through the normal schema/API |
| Ordinary SQL UPDATE/DELETE changes evidence | SQLite triggers reject these operations |
| Evidence payload is modified | Hash-chain verification detects changes in the chain |

## Out of scope / residual risks

- **Privileged database attacker:** an actor with write access to the database file can disable triggers and rewrite the full chain. Use OS permissions and external signed checkpoints for stronger guarantees.
- **False or compromised adapter:** the engine can verify that a submitted structured result matches a criterion; it cannot independently know whether a malicious adapter lied about the outside world.
- **Spoofed source labels:** the `source` field is descriptive metadata, not an identity credential.
- **Natural-language truth:** the engine does not establish factual truth of arbitrary text, summaries, or news analysis.
- **Host compromise:** a compromised host can alter code, policies, clocks, and evidence before they reach the ledger.
- **Incomplete criteria:** poorly designed or overly broad criteria can produce misleading assurance even when mechanically satisfied.

## Required integration rules

- Never expose arbitrary evidence creation as an unrestricted LLM tool.
- Keep task contracts and execution policies outside model control.
- Keep model access read-only for evidence and verification reports.
- Record actual tool outputs at the trusted adapter boundary.
- Treat missing, ambiguous, or malformed data as inconclusive.
- Do not use `passed` as a synonym for “the model says it worked.”
