# Findings

Independent peer-review reports from spawned research and QA agents. Each file is the output of one focused agent. Findings here are the **canonical source of "what the code actually does"** — when a doc upstream of this folder disagrees with a finding, the finding wins until the upstream doc is updated.

## File naming

- `scientific-review-<topic>.md` — Scientific / methodology review from a domain-expert agent (PhD-level).
- `code-review-<topic>.md` — Engineering / correctness review of the experiment harness and configs.
- `repo-audit-<repo>.md` — Direct read of an external repo (SEC-Bench, SecVerifier) to ground claims.

Each finding starts with a one-paragraph TL;DR and a verdict line: `PASS / FAIL / NEEDS-WORK`.
