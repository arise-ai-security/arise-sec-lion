# Claude Code Skills (checked-in copies)

`.claude/` is gitignored in this repo, so the **source of truth** for our two Claude Code slash-command skills lives here and is committed to git. When a teammate clones the repo and wants to use the skills in their own Claude Code session, they copy (or symlink) them into `.claude/commands/` in their working tree.

## What's here

| File | Slash command | Purpose |
|------|---------------|---------|
| `secbench-run.md` | `/secbench-run` | Optional shortcut that chains together pre-flight, image build, CLI launch, live DB monitoring, issue checklist, and post-run analysis for a SEC-bench CVE instance |
| `secbench-fixture.md` | `/secbench-fixture` | Optional shortcut that automates authoring a brand-new CVE fixture (research → Dockerfile → image build → DockerHub push → fixture JSON) |

Both skills are **optional**. The primary workflow is always "run by hand" — see README.md §4 Path A for the manual flow. These skills exist to bundle the same steps into a single invocation when you want the shortcut.

## Install

The skill files need to live at `.claude/commands/<name>.md` for Claude Code to pick them up. Two options:

### Option 1 — Symlink (recommended; one-way sync with git)

```bash
mkdir -p .claude/commands
ln -sf ../../skills/secbench-run.md     .claude/commands/secbench-run.md
ln -sf ../../skills/secbench-fixture.md .claude/commands/secbench-fixture.md
```

With symlinks, any `git pull` that updates the files under `skills/` is immediately picked up by Claude Code. No copy step, no drift.

### Option 2 — Copy

```bash
mkdir -p .claude/commands
cp skills/secbench-run.md     .claude/commands/secbench-run.md
cp skills/secbench-fixture.md .claude/commands/secbench-fixture.md
```

Use this if your environment doesn't play well with symlinks. You'll need to re-copy after future `git pull`s that touch `skills/*.md`.

## Updating the skills

Edit the copies under `skills/` (the checked-in source of truth) and commit. If you used symlinks, you're done. If you used copies, re-run the copy step above.

Do not edit `.claude/commands/*.md` directly — those changes are gitignored and will be overwritten on the next sync.

## Related

- `README.md` §4 — manual run workflow (primary)
- `README.md` §6 — manual fixture authoring workflow (primary)
- `README.md` §10 — reviewing results (dashboard + CLI primary; skill is optional shortcut)
