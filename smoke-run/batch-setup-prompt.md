# Batch Setup Prompt — B1 Full Run (311 CVEs, 20 Batches)

## What this prompt does

Creates 20 study directories under `experiments/`, each with a B1 config pointing at
~16 CVEs, then checks which `secb-tools:<instance_id>-patch` Docker images are missing
and builds them. Run this once before any batch-run prompt.

---

## Step 1 — Generate the batch manifest

Run this Python snippet from the repo root. It prints the canonical batch assignment
and writes `smoke-run/batch-manifest.json` for later prompts to consume.

```python
import json, glob, math, pathlib

fixtures = sorted(glob.glob("plugins/security/tests/fixtures/*.json"))
ids = [json.load(open(f))["instance_id"] for f in fixtures]

BATCHES = 20
batch_size = math.ceil(len(ids) / BATCHES)
batches = [ids[i * batch_size:(i + 1) * batch_size] for i in range(BATCHES)]
# Trim empty trailing batches
batches = [b for b in batches if b]

manifest = {
    "total_cves": len(ids),
    "total_batches": len(batches),
    "batch_size_nominal": batch_size,
    "batches": {f"{i+1:02d}": b for i, b in enumerate(batches)},
}
out = pathlib.Path("smoke-run/batch-manifest.json")
out.write_text(json.dumps(manifest, indent=2))
print(f"Wrote {out}: {len(ids)} CVEs in {len(batches)} batches")
for num, batch in manifest["batches"].items():
    print(f"  batch {num}: {len(batch)} CVEs  ({batch[0]} .. {batch[-1]})")
```

Verify the output matches expectations (311 CVEs, 20 batches, last batch ≤ 16).

---

## Step 2 — Create study directories

For each batch `NN` (01–20), create `experiments/2026-05-15-b1-batch-NN/` with:
- `configs/B1.yaml` — extends the canonical B1 config
- `reports/` — empty dir for run_matrix output

```python
import json, pathlib, textwrap

manifest = json.loads(pathlib.Path("smoke-run/batch-manifest.json").read_text())
base_config = "experiments/2026-05-13-my-study/configs/B1-ours-claude-noverifier.yaml"

for num, cves in manifest["batches"].items():
    study_dir = pathlib.Path(f"experiments/2026-05-15-b1-batch-{num}")
    configs_dir = study_dir / "configs"
    reports_dir = study_dir / "reports"
    configs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    # B1 config just re-uses the canonical one; tasks are passed at CLI time
    config_path = configs_dir / "B1.yaml"
    config_path.write_text(
        f"# Auto-generated for batch {num}\n"
        f"# Tasks: {cves[0]} .. {cves[-1]}\n"
        f"extends: ../../../{base_config}\n"
    )
    print(f"Created {study_dir}")
```

After running, confirm with:
```bash
ls experiments/ | grep b1-batch | wc -l   # expect 20
ls experiments/2026-05-15-b1-batch-01/configs/
```

---

## Step 3 — Check and build missing Docker images

Image naming convention: `secb-tools:<instance_id>-patch`

```bash
# List all needed tags
python3 -c "
import json, pathlib
manifest = json.loads(pathlib.Path('smoke-run/batch-manifest.json').read_text())
all_ids = [cve for batch in manifest['batches'].values() for cve in batch]
for id in all_ids:
    print(f'secb-tools:{id}-patch')
" > /tmp/needed-images.txt
wc -l /tmp/needed-images.txt

# Find which are already present
docker images --format "{{.Repository}}:{{.Tag}}" | grep "^secb-tools:" | sort > /tmp/present-images.txt
wc -l /tmp/present-images.txt

# Compute missing
comm -23 <(sort /tmp/needed-images.txt) /tmp/present-images.txt > /tmp/missing-images.txt
echo "Missing images:"
cat /tmp/missing-images.txt
wc -l /tmp/missing-images.txt
```

Build missing images (parallelism 4 is safe on a machine with ≥8 cores and fast internet):

```bash
# Extract instance IDs from missing list and build
sed 's|secb-tools:||;s|-patch||' /tmp/missing-images.txt | \
  xargs -P 4 -I{} bash -c \
    'deployment/build-secbench-tools.sh {} 2>&1 | tee /tmp/build-{}.log && echo "OK: {}" || echo "FAIL: {}"'
```

If any builds fail, re-run individually:
```bash
deployment/build-secbench-tools.sh <instance_id>
```

After building, re-run the missing check to confirm 0 missing images for your target batches.

---

## Step 4 — Verify setup

```bash
# All 20 study dirs exist
ls experiments/ | grep "2026-05-15-b1-batch" | wc -l

# B1 config in each
for d in experiments/2026-05-15-b1-batch-*/; do
  [ -f "$d/configs/B1.yaml" ] || echo "MISSING config in $d"
done

# Image coverage for first 3 batches (spot check)
python3 -c "
import json, pathlib, subprocess
manifest = json.loads(pathlib.Path('smoke-run/batch-manifest.json').read_text())
for num in ['01','02','03']:
    for cve in manifest['batches'][num]:
        tag = f'secb-tools:{cve}-patch'
        r = subprocess.run(['docker','image','inspect',tag],capture_output=True)
        status = 'OK' if r.returncode==0 else 'MISSING'
        print(f'{status}  {tag}')
"
```

Setup is complete when all images are present and all 20 study dirs exist.
Proceed with `batch-run-prompt.md` for each batch.
