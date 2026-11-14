# Hermes Agent v0.23.1 integration

## What this installs

The repository root is a native Hermes plugin (plugin.yaml + __init__.py). It uses two documented Hermes hooks:

- **pre_verify:** after Hermes has edited code and is about to finish a coding turn, the host runs the operator-approved checks. A failed or incomplete check returns a bounded continuation message so Lilith can repair the code.
- **transform_llm_output:** the final response is released unchanged only after the coding verification passed. If it did not pass, the final response is replaced with an explicit **CODING TASK NOT VERIFIED** notice.

The plugin registers no model-callable Evidence Engine writer, database, or command-execution tools. It runs only when Hermes reports coding=True and the turn contains changed paths. Ordinary conversation, research, reminders, and other non-coding tasks do not create Evidence Engine tasks or run project checks.

## Important streaming behavior

Hermes v0.23.1's transform_llm_output supports hold_stream=True. This is required so an unverified coding completion cannot be shown before the final gate rewrites it.

Hermes applies that hold to all final replies in the profile while the plugin is enabled. Non-coding replies pass through unchanged, and Evidence Engine is not invoked for them, but streaming is held until the final response. Enable this plugin only in the Hermes profile used for Lilith if that is the intended scope.

## Install after this pull request is merged

Do not install this branch into the live Hermes profile before review. Once merged:

    hermes plugins install oORaxaOo/Evidence-Engine --enable
    hermes plugins doctor evidence-engine

The plugin is opt-in in Hermes. Installing it does not enable it unless --enable is supplied.

## Configure the retry bound

The plugin defaults to five verification attempts. Hermes independently bounds the number of pre_verify continuations, so its bound must be at least as high.

Merge these keys into the existing ~/.hermes/config.yaml; do not replace the whole file:

    agent:
      max_verify_nudges: 5

    plugins:
      hook_callback_timeout: 300

Keep any existing keys under agent: and plugins:. The callback timeout is raised because a project test suite may take longer than Hermes' default hook timeout.

## Configure the host-owned project policy

Create the policy directory and copy the example:

    mkdir -p ~/.hermes/evidence-engine
    chmod 700 ~/.hermes/evidence-engine
    cp /path/to/Evidence-Engine/examples/hermes-policies.example.json ~/.hermes/evidence-engine/policies.json
    chmod 600 ~/.hermes/evidence-engine/policies.json

Edit policies.json and replace the example project root and interpreter path with absolute paths on the host. The root must exactly match the active Hermes workspace.

Example shape:

    {
      "schema_version": 1,
      "projects": [
        {
          "root": "/absolute/path/to/project",
          "commands": [
            {
              "id": "unit-tests",
              "argv": [
                "/absolute/path/to/project/.venv/bin/python",
                "-m",
                "pytest",
                "-q",
                "--junitxml=junit.xml"
              ],
              "junit_xml": "junit.xml",
              "timeout_seconds": 180,
              "max_output_bytes": 262144
            }
          ]
        }
      ]
    }

The policy is intentionally stored outside the coding workspace and must be owned by the Hermes user, not group/world-writable, and valid UTF-8 JSON. The command executable must be an absolute path. Commands are fixed argument vectors: no shell is used, no model-supplied arguments are appended, and the command runner receives only the explicitly configured environment (empty by default).

Each project must define exactly one JUnit-backed test command. The plugin verifies the actual allowlisted command execution and the JUnit report's validity, metadata consistency, non-empty test count, failures, and errors. Additional fixed build/compile commands may be listed alongside it.

Optional environment variables (set in the Hermes gateway environment or ~/.hermes/.env):

- EVIDENCE_ENGINE_POLICY_FILE — policy file path; defaults to ~/.hermes/evidence-engine/policies.json.
- EVIDENCE_ENGINE_DB — evidence database path; defaults to ~/.hermes/evidence-engine/evidence.sqlite3.
- EVIDENCE_ENGINE_MAX_ATTEMPTS — integer from 1 to 5; defaults to 5.

## Failure behavior

- Missing policy, unsafe policy permissions, unknown workspace, invalid command, missing report, or missing evidence never releases a coding task.
- A normal failed test/build can be retried within the configured attempt limit.
- Ledger-integrity or verification-snapshot failures stop immediately and are reported as unverified.
- At the attempt limit, the final answer is replaced with an incomplete status; it is not reported as successful.
- The database and writer stay inside the Hermes host process. The model cannot write evidence or change success criteria.

## Current limits

- This is a final-state coding gate, not a universal proof that software is bug-free.
- The initial report adapter is JUnit XML. Projects using other test-report formats need a suitable adapter before they can be approved.
- The host command policy is an operator trust boundary, not an OS sandbox. Approved commands run with the gateway user's permissions.
- The plugin uses in-memory per-session state for the active turn. It does not yet provide workflow recovery across a gateway crash.
- The plugin does not modify Hermes core, WhatsApp pairing, or the gateway configuration automatically. Installation and configuration are explicit operator actions.
