#!/usr/bin/env python3
"""Fetch SEC-bench instances from HuggingFace and save as individual JSON fixtures.

Usage:
    # Fetch all instances from all splits
    python fetch_secbench.py

    # Fetch a specific split only
    python fetch_secbench.py --split cve

    # Fetch a single instance by ID
    python fetch_secbench.py --instance-id imagemagick.cve-2018-5247

    # Dry run — just list what would be created
    python fetch_secbench.py --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

from datasets import load_dataset

DATASET = "SEC-bench/SEC-bench"
SPLITS = ["cve", "eval", "oss"]
OUTPUT_DIR = Path(__file__).parent

# Fields to include, matching the existing imagemagick fixture format (no 'patch')
FIELDS = [
    "instance_id",
    "repo",
    "project_name",
    "lang",
    "work_dir",
    "sanitizer",
    "bug_description",
    "base_commit",
    "build_sh",
    "secb_sh",
    "dockerfile",
    "exit_code",
    "sanitizer_report",
    "bug_report",
]


def make_filename(instance_id: str) -> str:
    """Convert instance_id like 'njs.cve-2022-32414' to 'njs.cve-2022-32414.json'."""
    return f"{instance_id}.json"


def row_to_dict(row: dict) -> dict:
    """Extract the relevant fields from a dataset row."""
    return {field: row[field] for field in FIELDS}


def fetch_and_save(
    splits: list[str],
    instance_id: str | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
) -> int:
    saved = 0

    for split in splits:
        print(f"Loading split '{split}' from {DATASET}...")
        ds = load_dataset(DATASET, split=split)

        for row in ds:
            rid = row["instance_id"]

            if instance_id and rid != instance_id:
                continue

            filename = make_filename(rid)
            filepath = OUTPUT_DIR / filename

            if filepath.exists() and not overwrite:
                print(f"  SKIP {filename} (exists, use --overwrite to replace)")
                continue

            if dry_run:
                print(f"  WOULD CREATE {filename}")
            else:
                data = row_to_dict(row)
                filepath.write_text(json.dumps(data, indent=2) + "\n")
                print(f"  SAVED {filename}")

            saved += 1

            if instance_id:
                return saved

    return saved


def main():
    parser = argparse.ArgumentParser(description="Fetch SEC-bench instances as JSON fixtures")
    parser.add_argument(
        "--split",
        choices=SPLITS,
        help="Only fetch from this split (default: all splits)",
    )
    parser.add_argument(
        "--instance-id",
        help="Fetch a single instance by its ID (e.g. njs.cve-2022-32414)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be created without writing them",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files",
    )
    args = parser.parse_args()

    splits = [args.split] if args.split else SPLITS

    count = fetch_and_save(
        splits=splits,
        instance_id=args.instance_id,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )

    print(f"\n{'Would create' if args.dry_run else 'Saved'} {count} file(s) to {OUTPUT_DIR}")
    if count == 0 and args.instance_id:
        print(f"Instance '{args.instance_id}' not found in splits: {splits}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
