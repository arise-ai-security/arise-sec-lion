"""Sample 20 repo-stratified SWE-bench Lite instances for experiment runs.

Guarantees all 12 repos are represented (1 each minimum), then fills
remaining slots proportionally, capped at 3 per repo.
"""

import json
import random
from collections import defaultdict
from pathlib import Path

from datasets import load_dataset


SEED = 42
SAMPLE_SIZE = 20
MAX_PER_REPO = 3
OUTPUT_PATH = Path("output/swebench_instances.jsonl")
FIELDS = ("instance_id", "repo", "base_commit", "problem_statement", "hints_text", "patch")


def _stratified_sample(ds: list[dict], n: int, max_per_repo: int) -> list[dict]:
    """Sample n instances ensuring all repos appear, capped per repo."""
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for i, row in enumerate(ds):
        by_repo[row["repo"]].append({"index": i, **row})

    # Phase 1: one from each repo
    selected: list[dict] = []
    remaining_pools: dict[str, list[dict]] = {}
    for repo, rows in sorted(by_repo.items()):
        pick = random.choice(rows)
        selected.append(pick)
        remaining_pools[repo] = [r for r in rows if r["index"] != pick["index"]]

    # Phase 2: fill remaining slots from repos that still have room
    remaining = n - len(selected)
    repo_counts = {repo: 1 for repo in by_repo}
    eligible = []
    for repo, pool in remaining_pools.items():
        for row in pool:
            if repo_counts[repo] < max_per_repo:
                eligible.append((repo, row))

    random.shuffle(eligible)
    for repo, row in eligible:
        if remaining <= 0:
            break
        if repo_counts[repo] < max_per_repo:
            selected.append(row)
            repo_counts[repo] += 1
            remaining -= 1

    return selected


def main() -> None:
    print("Loading SWE-bench Lite dataset...")
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")

    random.seed(SEED)
    samples = _stratified_sample(list(ds), SAMPLE_SIZE, MAX_PER_REPO)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        for row in samples:
            record = {field: row[field] for field in FIELDS}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(samples)} instances to {OUTPUT_PATH}\n")

    repo_counts: dict[str, int] = defaultdict(int)
    print("Selected instance_ids:")
    for row in samples:
        print(f"  {row['instance_id']}")
        repo_counts[row["repo"]] += 1

    print("\nRepo distribution:")
    for repo, count in sorted(repo_counts.items(), key=lambda x: -x[1]):
        print(f"  {count}x  {repo}")


if __name__ == "__main__":
    main()
