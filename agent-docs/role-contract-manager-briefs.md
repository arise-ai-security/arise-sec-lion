# Role Contracts and Manager Briefs

## Goal

Make B4 workers easier to steer without letting the manager redefine correctness.

The worker prompt should contain two separate inputs:

1. **Static role contract**: deterministic, code-owned instructions for the resolved
   catalog role.
2. **Manager brief**: task-specific context written by the strong manager for exactly one
   child worker.

The manager may specialize the plan using evidence. It must not invent success criteria,
required deliverables, topology rules, or sibling responsibilities.

## Static Role Contract

Static role contracts are the source of truth for mechanical behavior.

They should be keyed by catalog role name and rendered only for the worker whose
`task_description` resolves to that role.

Each contract should define:

- objective
- allowed inputs
- ordered steps
- files the role must write
- files the role must not write
- stop/fail conditions
- handoff payload

Example:

```text
ROLE: Build-Setup

OBJECTIVE:
Discover how this project should be built and where build outputs should appear.

READ:
- Source-tree build files
- Existing SEC-bench build conventions
- Parent task context

DO:
1. Identify the project root.
2. Identify the canonical build command.
3. Identify expected binary names and likely output paths.
4. Write /testcase/build_setup_findings.txt.

DO NOT:
- Run the full compile.
- Edit source.
- Add instrumentation probes.
- Write binary_paths.txt.
- Validate sanitizer behavior.

SUCCESS:
- build_setup_findings.txt names the source root, build command, and expected binaries.

FAIL:
- Cannot identify the build system or expected target binary.
```

## Manager Brief

The manager brief is not a contract. It is per-child routing and task-specific evidence.

The manager should put it in the child subtask's `justification`, which is already copied
to that child's `Briefing.subtask_justification`.

Recommended keys:

- `task_specific_context`
- `evidence_to_read`
- `concrete_starting_point`
- `handoff_expectation`
- `out_of_scope`

Example for only the Build-Setup child:

```json
{
  "description": "[Build-Setup] Discover OpenEXR build system and expected artifacts.",
  "justification": {
    "task_specific_context": "Project is OpenEXR under /src/openexr.",
    "evidence_to_read": "Inspect build files and existing SEC-bench build wrapper behavior.",
    "concrete_starting_point": "Find where exrmakepreview is built.",
    "handoff_expectation": "Write build_setup_findings.txt for Build-Compiler.",
    "out_of_scope": "Do not compile, edit source, or write binary_paths.txt."
  },
  "depends_on": []
}
```

The Build-Compiler child should receive a different brief. Build-Compiler instructions
must not be injected into Build-Setup.

## Prompt Assembly

For each worker, render:

```text
STATIC CONTRACT for resolved role only
+
MANAGER BRIEF for this child only
+
UPSTREAM ARTIFACTS from declared depends_on only
```

If `worker_role` is absent, fall back to the existing whole-phase prompt used by flat or
non-role workers.

## Enforcement Rules

The topology gate remains mandatory:

- every required role must be present
- every child leaf must carry exactly one catalog role
- merged-role leaves must be rejected or repaired before spawning
- injected leaves must not inherit sibling-specific manager briefs
- injected leaves should appear in catalog execution order
- surviving `depends_on` indices must be remapped after removals

Static role contracts improve worker execution. They do not replace the decomposition
validator.

## Tests

Minimum prompt tests:

- `[Build-Setup]` prompt contains the Build-Setup contract.
- `[Build-Setup]` prompt does not contain Build-Compiler-only instructions such as
  running the full compile or writing `binary_paths.txt`.
- `[Build-Compiler]` prompt contains compile responsibilities and uses
  `build_setup_findings.txt` as input.
- `[Build-Verifier]` prompt contains binary verification responsibilities, not setup
  discovery steps.
- A child-specific manager brief appears only in that child's prompt.
- A merged-role leaf is caught by the decomposition validator before worker prompt
  rendering.

## Implementation Order

1. Add Builder role contracts and prompt tests.
2. Render the resolved worker role's static contract.
3. Tighten manager decomposition guidance so each child `justification` is role-local.
4. Extend the same pattern to Exploiter and Fixer after Builder prompt isolation is proven.
5. Rerun affected B4 instances and measure strict pass, not only `cve_reproduced`.
