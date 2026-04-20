#!/usr/bin/env bash
# Pillar B reproducibility harness.
#
# From a fresh checkout with the dataset tarball in place, this script:
#   1. Extracts dataset-v1.0-20260419.tar.zst if dataset/ is empty.
#   2. Re-runs the B-cell patch-extraction retrofit (mutates
#      dataset/INDEX.jsonl and dataset/runs/*/B*/*/mechanical.json).
#   3. Runs A-cell sensitivity (emits corrected_mechanical.json alongside
#      each A-cell run; does not touch mechanical.json or INDEX).
#   4. Recomputes all tables (docs/pillar_b/tables/*).
#   5. Regenerates all figures (docs/pillar_b/figures/*.png).
#   6. Regenerates the 4 narrated log excerpts (docs/pillar_b/examples/*).
#
# Prerequisites:
#   - uv (python package manager).
#   - Docker Desktop (or Docker Engine) with secb-tools:<cve>-patch images
#     available locally. The 10 locked CVEs are enumerated in
#     dataset/locked_instances.yaml.
#   - Run from the repo root.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

TARBALL="dataset-v1.0-20260419.tar.zst"
DATASET_DIR="dataset"

echo "==> Pillar B reproducibility run"
echo "==> Repo root: $REPO_ROOT"

if [[ ! -d "$DATASET_DIR/runs" ]]; then
    echo "==> Extracting $TARBALL"
    if [[ ! -f "$TARBALL" ]]; then
        echo "ERROR: $TARBALL not found. Place the Pillar A tarball at repo root."
        exit 1
    fi
    tar --zstd -xf "$TARBALL"
fi

echo "==> Checking docker daemon"
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker daemon is not running. Start Docker Desktop and retry."
    exit 1
fi

echo "==> Installing/refreshing Python deps via uv"
uv sync --frozen >/dev/null
# Ensure analysis deps present (not in base lock by default).
uv pip install --quiet scipy statsmodels matplotlib seaborn tiktoken

echo "==> [1/4] Retrofit — B-cell tree patch extraction + re-evaluation"
uv run python docs/pillar_b/scripts/retrofit_tree_patches.py

echo "==> [2/4] A-cell sensitivity (corrected_mechanical.json; INDEX untouched)"
uv run python docs/pillar_b/scripts/retrofit_tree_patches.py \
    --cells A1,A2,A3,A4 \
    --output-name corrected_mechanical.json \
    --no-index-update

echo "==> [3/4] Compute metrics + emit CSV tables"
uv run python docs/pillar_b/scripts/compute_metrics.py

echo "==> [4/4] Figures + log excerpts"
uv run python docs/pillar_b/scripts/make_figures.py
uv run python docs/pillar_b/scripts/extract_excerpts.py

echo "==> Done. Outputs:"
echo "   docs/pillar_b/tables/     (4 CSVs + retrofit_summary.csv)"
echo "   docs/pillar_b/figures/    (7 PNGs @ 300 dpi)"
echo "   docs/pillar_b/examples/   (4 narrated excerpts)"
echo "   docs/pillar_b/2026-04-19-pillar-b-analysis.md"
