# N1 handoff

Run from the cloned repository root.

## 1. Environment and OpenAI key

```bash
python3 experiments/n1-secbench-full/setup.py
```

The committed `experiments/n1-secbench-full/.env.example` is the template. Setup creates
the ignored, study-only `experiments/n1-secbench-full/.env` and asks once for
`OPENAI_API_KEY`.

## 2. Change the model (optional)

Edit `experiments/n1-secbench-full/configs/N1-openhands-linear.yaml`. Change only
`worker.model`.

## 3. Run

```bash
python3 experiments/n1-secbench-full/run.py --parallel 2 --batch-size 30
```

This command prepares, runs, or resumes the experiment. Adjust `--parallel` and
`--batch-size` for the host.

Smoke:

```bash
python3 experiments/n1-secbench-full/run.py --smoke --parallel 2 --batch-size 2
```

## 4. Export

After the run finishes:

```bash
python3 experiments/n1-secbench-full/export.py
```

The finished export is written to `~/n1-secbench-full-export`.
