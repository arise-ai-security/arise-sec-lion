# Research Documentation

This folder tracks research-paper-quality artifacts for the ARISE / SEC-LION project. Engineering docs (architecture, dev guides, etc.) live in `agent-docs/` and `.claude/docs/`. This folder is exclusively about the **paper**: thesis, hypotheses, experimental design, threats to validity, and decisions.

## How to use

When discussing code or content in this repo, if something is paper-relevant — a novelty claim, a design choice with paper implications, an experimental control rationale, an observed phenomenon worth measuring — add it here.

The repo-level `CLAUDE.md` instructs the assistant to ask before adding content here.

## Layout

| File | Purpose |
|---|---|
| `00-paper-vision.md` | One-page thesis: what the paper claims and why. |
| `01-hypothesis.md` | Falsifiable hypotheses H1–H4 and what would falsify each. |
| `02-experimental-design.md` | A/B/C × 1/2 matrix, controlled variables, dependent variables. |
| `03-threats-to-validity.md` | Confounds (TV1–TV9) with controls and residual risk. |
| `04-open-questions.md` | Live questions requiring decision (incl. author's Q1, Q2). |
| `findings/` | Independent peer-review reports from spawned research/QA agents. |

## Conventions

1. **Falsifiability before persuasion.** Every claim states what evidence would refute it. If you cannot state the falsifier, the claim is not yet ready.
2. **Cite or retract.** Code claims cite `path/to/file:line`. External claims cite paper / README. No "I think it does X" without source.
3. **ASSUMPTION / CLAIM / EVIDENCE** labels when a paragraph contains a mix.
4. **Concrete over vague.** "200 CVEs" beats "many CVEs". "Sonnet 4.6" beats "a strong model".
5. **When this doc disagrees with code, code wins** until this doc is updated. Findings in `findings/` are the canonical source of "what the code actually does".
6. **Status markers**: DRAFT / FROZEN / SUPERSEDED at the top of each doc. FROZEN docs cannot be edited without a decision-log entry.
