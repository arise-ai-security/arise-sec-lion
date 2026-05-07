#!/usr/bin/env python3
"""
Batch runner for newly-created SEC-bench fixtures (not yet committed).

Runs the specified fixture instances with configurable batch size,
monitors for under-decomposition, retries up to --max-retries times,
and records results to the specified CSV (default ~/new_fixtures.csv).

Usage:
    python scripts/batch_new_fixtures.py --batch-size 2 --max-retries 3
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_new_fixtures")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "plugins" / "security" / "tests" / "fixtures"
RUNS_DIR = PROJECT_ROOT / "runs"
CSV_PATH = Path.home() / "new_fixtures.csv"

# Under-decomposition thresholds
UNDER_DECOMP_THRESHOLD = 8
MIN_BOSS_MANAGERS = 3
DECOMP_GRACE_SEC = 90
STALL_SEC = 450
POLL_INTERVAL_SEC = 15
RUN_TIMEOUT_SEC = 10800  # 3 hours per instance

# The 8 new fixtures to run
NEW_FIXTURES = [
    "carbon-lang.issue-7162",
    "flatbuffers.issue-9041",
    "flatbuffers.issue-9074",
    "libheif.issue-1746",
    "spirv-tools.issue-6663",
    "spirv-tools.issue-6664",
    "tidy-html5.issue-1175",
    "vorbis.issue-125",
]

CSV_FIELDS = [
    "instance_id",
    "status",
    "boss_id",
    "testcase_path",
    "num_agents",
    "duration_seconds",
    "tries",
    "error_message",
    "started_at",
    "completed_at",
]


@dataclass
class RunResult:
    instance_id: str
    status: str = "pending"
    boss_id: str = ""
    testcase_path: str = ""
    num_agents: int = 0
    duration_seconds: float = 0.0
    tries: int = 0
    error_message: str = ""
    started_at: str = ""
    completed_at: str = ""

    def as_dict(self) -> dict[str, str]:
        return {f: str(getattr(self, f)) for f in CSV_FIELDS}


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def load_completed() -> dict[str, RunResult]:
    done: dict[str, RunResult] = {}
    if not CSV_PATH.exists():
        return done
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            status = row.get("status", "")
            if status in ("success", "image_error"):
                iid = row["instance_id"]
                done[iid] = RunResult(**{k: row.get(k, "") for k in CSV_FIELDS})
    return done


def upsert_csv(result: RunResult) -> None:
    rows: list[dict[str, str]] = []
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="") as f:
            rows = list(csv.DictReader(f))

    updated = False
    for i, row in enumerate(rows):
        if row.get("instance_id") == result.instance_id:
            rows[i] = result.as_dict()
            updated = True
            break
    if not updated:
        rows.append(result.as_dict())

    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# DB helpers (via docker exec psql)
# ---------------------------------------------------------------------------
DB_CMD_PREFIX = [
    "docker", "exec", "arise-db",
    "psql", "-U", "arise", "-d", "arise_events", "-t", "-A", "-c",
]


async def db_query(sql: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *DB_CMD_PREFIX, sql,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode(errors="replace").strip()


async def db_boss_id(instance_id: str, after: str) -> str | None:
    sql = (
        f"SELECT aggregate_id FROM events "
        f"WHERE event_type='RunStarted' "
        f"AND payload->>'instance_id' = '{instance_id}' "
        f"AND occurred_at > '{after}' "
        f"ORDER BY occurred_at DESC LIMIT 1;"
    )
    val = await db_query(sql)
    return val if val else None


async def db_agent_count(boss_id: str) -> int:
    sql = (
        f"SELECT count(*) FROM events "
        f"WHERE event_type='AgentCreated' "
        f"AND (aggregate_id::text = '{boss_id}' "
        f"     OR payload->>'root_id' = '{boss_id}');"
    )
    val = await db_query(sql)
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


async def db_boss_children_count(boss_id: str) -> int:
    sql = (
        f"SELECT count(*) FROM events "
        f"WHERE event_type='AgentCreated' "
        f"AND payload->>'parent_id' = '{boss_id}';"
    )
    val = await db_query(sql)
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


async def db_run_completed(boss_id: str) -> str | None:
    sql = (
        f"SELECT payload->>'final_status' FROM events "
        f"WHERE event_type='RunCompleted' "
        f"AND aggregate_id::text = '{boss_id}' LIMIT 1;"
    )
    val = await db_query(sql)
    return val if val else None


async def db_last_event_time(boss_id: str) -> str | None:
    sql = (
        f"SELECT MAX(occurred_at) FROM events "
        f"WHERE aggregate_id::text = '{boss_id}' "
        f"OR payload->>'root_id' = '{boss_id}';"
    )
    val = await db_query(sql)
    return val if val else None


# ---------------------------------------------------------------------------
# Docker image build
# ---------------------------------------------------------------------------
async def build_image(instance_id: str) -> bool:
    fixture_path = FIXTURE_DIR / f"{instance_id}.json"
    with open(fixture_path) as f:
        fixture = json.load(f)

    base_image = fixture.get("docker_image_override", f"hwiwonlee/secb.eval.x86_64.{instance_id.replace('.', '.')}:patch")
    tools_image = f"secb-tools:{instance_id}-patch"

    # Check if tools image already exists
    check = await asyncio.create_subprocess_exec(
        "docker", "image", "inspect", tools_image,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await check.wait()
    if check.returncode == 0:
        logger.info("[%s] tools image exists", instance_id)
        return True

    # Pull base image
    logger.info("[%s] pulling base image %s", instance_id, base_image)
    pull = await asyncio.create_subprocess_exec(
        "docker", "pull", base_image,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await pull.communicate()
    if pull.returncode != 0:
        logger.error("[%s] pull failed: %s", instance_id, stderr.decode()[-200:])
        return False

    # Build tools layer
    logger.info("[%s] building tools image", instance_id)
    build = await asyncio.create_subprocess_exec(
        "docker", "build",
        "-f", str(PROJECT_ROOT / "deployment" / "secbench-tools.Dockerfile"),
        "--build-arg", f"BASE_IMAGE={base_image}",
        "-t", tools_image,
        str(PROJECT_ROOT),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await build.communicate()
    if build.returncode != 0:
        logger.error("[%s] build failed: %s", instance_id, stderr.decode()[-200:])
        return False

    logger.info("[%s] tools image ready", instance_id)
    return True


async def build_images_parallel(ids: list[str]) -> dict[str, bool]:
    sem = asyncio.Semaphore(3)

    async def _build(iid: str) -> tuple[str, bool]:
        async with sem:
            ok = await build_image(iid)
            return iid, ok

    results = await asyncio.gather(*[_build(iid) for iid in ids])
    return dict(results)


# ---------------------------------------------------------------------------
# Kill helpers
# ---------------------------------------------------------------------------
async def _kill_run(proc: asyncio.subprocess.Process, instance_id: str, boss_id: str | None) -> None:
    logger.warning("[%s] killing run", instance_id)
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass

    # Kill any leftover secb containers for this run
    if boss_id:
        kill_cmd = await asyncio.create_subprocess_shell(
            f"docker ps -q --filter label=arise.root_id={boss_id} | xargs -r docker kill",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await kill_cmd.wait()


# ---------------------------------------------------------------------------
# Single-instance runner
# ---------------------------------------------------------------------------
async def run_instance(instance_id: str, attempt: int) -> RunResult:
    result = RunResult(instance_id=instance_id, tries=attempt)
    fixture_path = FIXTURE_DIR / f"{instance_id}.json"

    with open(fixture_path) as f:
        fixture = json.load(f)

    project = fixture["project_name"]
    cve_id = instance_id.split(".", 1)[1] if "." in instance_id else instance_id
    bug_short = fixture["bug_description"][:120].replace("'", "")
    task = (
        f"Analyze {cve_id} in {project}: {bug_short}, "
        "perform static analysis, build the project, "
        "reproduce the bug with the PoC, develop and verify a patch"
    )

    started = datetime.now(timezone.utc)
    result.started_at = started.isoformat()

    logger.info("[%s] attempt %d — launching", instance_id, attempt)

    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "arise-app",
        "python", "main.py", "run",
        "--domain-context-file", f"plugins/security/tests/fixtures/{instance_id}.json",
        "--domain", "security",
        task,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    # Wait for boss_id (up to 90s)
    boss_id: str | None = None
    for _ in range(90):
        if proc.returncode is not None:
            break
        await asyncio.sleep(1)
        boss_id = await db_boss_id(instance_id, result.started_at)
        if boss_id:
            break

    if not boss_id:
        try:
            stdout_bytes = await asyncio.wait_for(proc.stdout.read(2000), timeout=5)
            err_tail = stdout_bytes.decode(errors="replace")[-200:]
        except Exception:
            err_tail = ""
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            await _kill_run(proc, instance_id, None)
        result.status = "error"
        result.error_message = f"No RunStarted event. {err_tail}".strip()
        result.completed_at = datetime.now(timezone.utc).isoformat()
        logger.warning("[%s] no boss_id found", instance_id)
        return result

    result.boss_id = boss_id
    result.testcase_path = str(RUNS_DIR / boss_id / "testcase")

    # Monitor loop
    while proc.returncode is None:
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()

        # Hard timeout
        if elapsed > RUN_TIMEOUT_SEC:
            logger.warning("[%s] TIMEOUT after %.0fs", instance_id, elapsed)
            await _kill_run(proc, instance_id, boss_id)
            result.status = "timeout"
            result.duration_seconds = elapsed
            result.num_agents = await db_agent_count(boss_id)
            result.completed_at = datetime.now(timezone.utc).isoformat()
            upsert_csv(result)
            return result

        # Under-decomposition check (after grace period)
        if elapsed > DECOMP_GRACE_SEC:
            boss_children = await db_boss_children_count(boss_id)
            if boss_children < MIN_BOSS_MANAGERS:
                logger.warning("[%s] under-decomposed (%d boss children)", instance_id, boss_children)
                await _kill_run(proc, instance_id, boss_id)
                result.status = "under_decomposed"
                result.duration_seconds = elapsed
                result.num_agents = await db_agent_count(boss_id)
                result.completed_at = datetime.now(timezone.utc).isoformat()
                upsert_csv(result)
                return result

        # Stall detection
        last_event = await db_last_event_time(boss_id)
        if last_event:
            try:
                last_dt = datetime.fromisoformat(last_event.replace("+00", "+00:00"))
                stall_secs = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if stall_secs > STALL_SEC:
                    logger.warning("[%s] STALLED (%.0fs no events)", instance_id, stall_secs)
                    await _kill_run(proc, instance_id, boss_id)
                    result.status = "timeout"
                    result.error_message = f"Stalled {stall_secs:.0f}s"
                    result.duration_seconds = elapsed
                    result.num_agents = await db_agent_count(boss_id)
                    result.completed_at = datetime.now(timezone.utc).isoformat()
                    upsert_csv(result)
                    return result
            except (ValueError, TypeError):
                pass

        # Check if run completed via DB
        final_status = await db_run_completed(boss_id)
        if final_status:
            result.status = "success" if final_status == "success" else "failed"
            result.duration_seconds = elapsed
            result.num_agents = await db_agent_count(boss_id)
            result.completed_at = datetime.now(timezone.utc).isoformat()
            if final_status != "success":
                result.error_message = f"final_status={final_status}"
            logger.info("[%s] completed: %s (%.0fs, %d agents)",
                        instance_id, result.status, elapsed, result.num_agents)
            upsert_csv(result)
            # Let the process finish naturally
            try:
                await asyncio.wait_for(proc.wait(), timeout=30)
            except asyncio.TimeoutError:
                await _kill_run(proc, instance_id, boss_id)
            return result

        await asyncio.sleep(POLL_INTERVAL_SEC)

    # Process exited without RunCompleted event
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    result.duration_seconds = elapsed
    result.num_agents = await db_agent_count(boss_id)
    result.completed_at = datetime.now(timezone.utc).isoformat()

    final_status = await db_run_completed(boss_id)
    if final_status:
        result.status = "success" if final_status == "success" else "failed"
        if final_status != "success":
            result.error_message = f"final_status={final_status}"
    else:
        result.status = "error"
        result.error_message = "Process exited without RunCompleted"

    logger.info("[%s] done: %s (%.0fs)", instance_id, result.status, elapsed)
    upsert_csv(result)
    return result


async def _run_with_retry(instance_id: str, sem: asyncio.Semaphore, max_retries: int, stagger_delay: float = 0) -> RunResult:
    if stagger_delay > 0:
        await asyncio.sleep(stagger_delay)

    for attempt in range(1, max_retries + 1):
        async with sem:
            result = await run_instance(instance_id, attempt)

        if result.status in ("success", "image_error", "error"):
            return result
        if result.status == "under_decomposed" and attempt < max_retries:
            logger.info("[%s] retrying (attempt %d/%d)", instance_id, attempt + 1, max_retries)
            await asyncio.sleep(5)
            continue
        break

    upsert_csv(result)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def async_main(args: argparse.Namespace) -> None:
    # Load resume state
    completed = load_completed()
    remaining = [iid for iid in NEW_FIXTURES if iid not in completed]
    logger.info(
        "%d total · %d already done · %d remaining",
        len(NEW_FIXTURES), len(completed), len(remaining),
    )
    if not remaining:
        logger.info("All instances already processed. Nothing to do.")
        return

    # Build all images first
    logger.info("═══ Phase 1: Build Docker images ═══")
    img_ok = await build_images_parallel(remaining)
    runnable: list[str] = []
    for iid in remaining:
        if img_ok.get(iid):
            runnable.append(iid)
        else:
            r = RunResult(
                instance_id=iid,
                status="image_error",
                error_message="Docker image build/pull failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            upsert_csv(r)
            logger.warning("[%s] image unavailable — skipped", iid)

    if not runnable:
        logger.info("No runnable instances.")
        return

    # Run instances concurrently (bounded by batch size)
    logger.info(
        "═══ Phase 2: Running %d instances (%d concurrent) ═══",
        len(runnable), args.batch_size,
    )
    sem = asyncio.Semaphore(args.batch_size)
    tasks = [
        asyncio.create_task(
            _run_with_retry(iid, sem, args.max_retries, stagger_delay=i * 3),
        )
        for i, iid in enumerate(runnable)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Summary
    status_counts: dict[str, int] = {}
    for r in results:
        if isinstance(r, RunResult):
            status_counts[r.status] = status_counts.get(r.status, 0) + 1
        else:
            status_counts["error"] = status_counts.get("error", 0) + 1
            logger.error("Exception: %s", r)

    logger.info("═══ All instances complete ═══")
    for s, cnt in sorted(status_counts.items()):
        logger.info("  %-20s %d", s, cnt)
    logger.info("Results: %s", CSV_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch runner for new SEC-bench fixtures")
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
