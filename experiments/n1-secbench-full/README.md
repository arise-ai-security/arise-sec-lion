# N1 full SEC-bench handoff

Prerequisites: branch `experiment/n1-secbench-full`, Docker, `uv`, PostgreSQL CLI tools,
disk for two SEC-bench task image stacks, and your own `OPENAI_API_KEY` and
`POSTGRES_PASSWORD` environment values. The 300 input fixtures are already in the branch.
`prepare_n1_experiment.py` checks the complete toolchain, Docker/storage, configuration,
writable paths, and Git state, then starts PostgreSQL and idempotently initializes its schema.

```bash
uv run python experiments/n1-secbench-full/prepare_n1_experiment.py

uv run python experiments/n1-secbench-full/run_batch.py \
  --batch-size 30 --parallel 2

uv run python experiments/n1-secbench-full/export_data.py
```

For multiple hosts, add one unique `--shard N/H` to each run command. Rerun the same command
to resume. Images are deleted after each two-task wave. Send the entire export directory
from every host; `SHA256SUMS` verifies each transfer.
