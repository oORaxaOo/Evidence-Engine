# Hermes coding integration contract

## Purpose and scope

Evidence Engine is used only for software-development work: creating or changing code, scripts, applications, tests, build pipelines, and other software artifacts. It is not a general-purpose Lilith control system.

Hermes must route a task into this workflow only when the task is explicitly classified as coding/build work. Ordinary conversation, research, fact-finding, planning, reminders, appointments, and other future assistant capabilities are outside this workflow. They may receive separate control systems later.

The repository also contains an opt-in native Hermes Agent v0.23.1 plugin. It connects the host-side verifier to Hermes' documented coding-only pre_verify and transform_llm_output hooks. It does not change Hermes core, WhatsApp pairing, or gateway configuration automatically. See [Hermes v0.23.1 integration](hermes-v0231-integration.md) for installation and host-policy setup.

## Authority boundaries

| Component | May do | Must not do |
|---|---|---|
| Lilith / language model | Understand the coding request, propose a plan, write or revise code through approved host tools, interpret verification feedback | Create or weaken success criteria, write evidence, set task status, approve its own completion |
| Hermes host integration | Classify the task, create the immutable task contract, invoke approved adapters, coordinate repair attempts, call the verification gate | Treat model prose or callback return values as execution evidence |
| Trusted adapters | Execute fixed, operator-approved commands and inspect approved artifacts; record bounded observations | Accept unrestricted model-supplied command lines, working directories, or security policy |
| Evidence Engine writer | Create tasks, append structured evidence, verify criteria, finalize task states | Be exposed as a general-purpose model-callable tool |
| EvidenceReader / read-only bridge | Return task, evidence, verification, and ledger-integrity reports | Create tasks, append evidence, run commands, or change statuses |
| Coding verification gate | Release completion only when the independent report passes every required check | Infer success from a natural-language claim |

The host must keep writer objects, SQLite write access, command-policy configuration, and privileged adapters outside the model's callable tool surface. Read-only access is still untrusted data: command output, filenames, test names, and observed values may contain hostile or misleading text.

## Required task lifecycle

1. **Classify.** The Hermes host explicitly identifies the request as coding/build work. If it is not coding work, do not invoke CodingWorkflow.
2. **Define criteria.** Trusted host code or the operator selects concrete, machine-checkable criteria before execution. Examples include a fixed build command returning exit code zero, a required test report with zero failures and errors, and an expected artifact existing with a recorded digest. A model may suggest criteria, but it must not be able to weaken the accepted contract after execution starts.
3. **Create an attempt.** CodingWorkflow creates a new task with the same immutable criteria for that attempt and records the workflow ID, attempt number, and previous task ID.
4. **Execute.** The host callback invokes the approved coding agent and trusted adapters. The callback return value is ignored. Only structured evidence recorded by trusted adapters can satisfy criteria.
5. **Finalize and inspect.** The writer finalizes the task. The separate EvidenceReader then reads a report and the CodingVerificationGate validates it.
6. **Release or repair.** A released gate result is the only success signal. Otherwise, deterministic criterion feedback is passed to the next attempt, while retry limits and terminal integrity failures are enforced.
7. **Escalate.** If the attempt limit is reached, or a non-retryable integrity problem occurs, the host reports the task as incomplete. It must not claim success.

## Verification gate requirements

A task may be released only if all of the following hold:

- the stored task status is passed;
- the reader's effective_status is passed;
- the full evidence hash chain is valid;
- the verification exists, is current, and is internally consistent;
- the verification status and every criterion check are supported;
- every criterion has the required number of evidence references;
- all referenced evidence IDs exist in the complete, non-truncated task snapshot.

Missing, malformed, stale, contradictory, or incomplete evidence never becomes success. The report is a check of the declared criteria, not proof that the code is universally bug-free or that the test suite is comprehensive.

## Retry behavior

CodingWorkflow permits one to five attempts (default: five). Each attempt is a separate Evidence Engine task because a failed task is terminal in the current task state machine. The new task receives exactly the same criteria; it does not inherit a weaker contract.

The next attempt receives:
- the previous task ID and effective status;
- deterministic verification reasons;
- per-criterion status, message, evidence IDs, and observed values.

Observed values originate in tool outputs and must be presented to the model as untrusted data, never as instructions. The model cannot edit the previous ledger record or change the next attempt's success criteria.

A workflow stops unsuccessfully when the attempt limit is reached. Evidence-chain corruption, an inconsistent verification record, or an incomplete verification snapshot is not a normal coding defect and must be escalated rather than hidden behind repeated code changes.

## Coding policy examples

A task-specific policy should be narrow. A small Python change might require:

1. a pre-registered compile command with exit code zero;
2. a pre-registered unit-test command with exit code zero;
3. a JUnit report linked to that exact command, valid and internally consistent, with zero failures and errors;
4. a required output artifact observed by the trusted file adapter.

A larger application may add integration tests, packaging, or a controlled runtime smoke test. Do not accept an empty test suite as meaningful coverage merely because the test command exited successfully. The policy author must define what counts as adequate validation for the requested change.

Do not use model-authored shell strings as command policies. The command adapter is not an operating-system sandbox; approved commands still run with the host process's permissions.

## Current implementation status

Implemented in this branch:

- a coding-only CodingWorkflow host primitive;
- a fail-closed CodingVerificationGate;
- one fresh task per attempt, with unchanged criteria;
- a maximum of five attempts;
- deterministic feedback for the next attempt;
- ignored callback return values;
- type-only recording of executor exceptions;
- tests for success, repair, missing evidence, callback exceptions, and fabricated success claims.

Not implemented yet:

- Hermes task classification and callback wiring;
- Hermes tool registration and permission configuration;
- a production command-policy catalog for Lilith's development environment;
- OS-level sandboxing and resource isolation;
- authenticated remote access or signed external checkpoints;
- durable workflow-resume orchestration after a host crash.

These are explicit remaining steps. The presence of this contract and Python primitive must not be described as a completed Hermes integration.
