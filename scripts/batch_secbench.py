#!/usr/bin/env python3
"""
Batch runner for SEC-bench CVE instances.

Downloads 200 CVE instances from the HuggingFace SEC-bench dataset,
generates fixture JSONs, builds Docker images, runs instances in
configurable batch sizes, monitors for under-decomposition, retries
up to --max-retries times, and records results to ~/secbench-all.csv.

Supports resumption: reads ~/secbench-all.csv on startup and skips
instances already marked 'success' or 'image_error'.

Usage:
    python scripts/batch_secbench.py [--batch-size 15] [--max-retries 3]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import shlex
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_secbench")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "plugins" / "security" / "tests" / "fixtures"
RUNS_DIR = PROJECT_ROOT / "runs"
CSV_PATH = Path.home() / "secbench-all.csv"

# Under-decomposition: if a run finishes (or has been running > DECOMP_GRACE_SEC)
# with fewer than this many agents, treat it as under-decomposed.
UNDER_DECOMP_THRESHOLD = 8
# Structural check: boss must spawn at least this many manager children.
# Set to 2 to tolerate Builder executing as worker (common with Qwen —
# Builder build tasks work fine as single workers). Exploiter + Fixer
# as managers is the minimum for a viable security analysis run.
MIN_BOSS_MANAGERS = 2
DECOMP_GRACE_SEC = 300  # 5 min grace before structural under-decomp checks
# Count-based under-decomposition is noisier than structural checks and can
# false-fire while pending children are still being assessed. Defer it.
UNDER_DECOMP_COUNT_GRACE_SEC = 900
STALL_SEC = 1200  # 20 min with no new events = stalled
CHILD_RESCUE_SEC = 600  # pre-kill rescue threshold for forever-analyzing child
CHILD_RESCUE_GRACE_SEC = 120  # wait for fresh events after child rescue
POLL_INTERVAL_SEC = 15  # poll frequently for early underdecomp detection
RUN_TIMEOUT_SEC = 10800  # 3 hours hard cap per instance (1.5x for Qwen)

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


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
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
    """Load instances that should NOT be re-run from the CSV."""
    done: dict[str, RunResult] = {}
    if not CSV_PATH.exists():
        return done
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("status") in ("success", "image_error"):
                r = RunResult(instance_id=row["instance_id"])
                for k in CSV_FIELDS:
                    setattr(r, k, row.get(k, ""))
                try:
                    r.tries = int(r.tries)  # type: ignore[arg-type]
                    r.num_agents = int(r.num_agents)  # type: ignore[arg-type]
                    r.duration_seconds = float(r.duration_seconds)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    pass
                done[r.instance_id] = r
    return done


def upsert_csv(result: RunResult) -> None:
    """Insert or update a single result row in the CSV."""
    rows: list[dict[str, str]] = []
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="") as f:
            rows = list(csv.DictReader(f))

    updated = False
    for i, row in enumerate(rows):
        if row["instance_id"] == result.instance_id:
            rows[i] = result.as_dict()
            updated = True
            break
    if not updated:
        rows.append(result.as_dict())

    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Fixture generation
# ---------------------------------------------------------------------------
def generate_fixtures() -> list[str]:
    """Download SEC-bench CVE split and write missing fixture JSONs.

    Returns the ordered list of 200 CVE instance_ids.
    """
    try:
        from datasets import load_dataset
    except ImportError:
        logger.error("Install the 'datasets' package: pip install datasets")
        sys.exit(1)

    logger.info("Loading SEC-bench dataset …")
    ds = load_dataset("SEC-bench/SEC-bench", split="eval")

    ids: list[str] = []
    created = 0
    for i, row in enumerate(ds):
        if i >= 200:  # CVE split is rows 0-199
            break
        iid: str = row["instance_id"]
        ids.append(iid)

        path = FIXTURE_DIR / f"{iid}.json"
        if path.exists():
            continue

        fixture = {
            "instance_id": iid,
            "repo": row["repo"],
            "project_name": row["project_name"],
            "lang": row["lang"],
            "work_dir": row["work_dir"],
            "sanitizer": row["sanitizer"],
            "bug_description": row["bug_description"],
            "base_commit": row["base_commit"],
            "build_sh": row.get("build_sh") or "",
            "secb_sh": row.get("secb_sh") or "",
            "dockerfile": row.get("dockerfile") or "",
            "patch": row.get("patch") or "",
            "exit_code": row.get("exit_code") or 0,
            "sanitizer_report": row.get("sanitizer_report") or "",
            "bug_report": row.get("bug_report") or "",
        }
        path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
        created += 1

    logger.info("Fixtures: %d total, %d newly created", len(ids), created)
    return ids


# ---------------------------------------------------------------------------
# Docker image resolution & build (no uv/venv dependency)
# ---------------------------------------------------------------------------
def _resolve_images(instance_id: str) -> tuple[str, str]:
    """Resolve base and tools image names from fixture JSON.

    Returns (base_image, target_image).
    """
    fixture_path = FIXTURE_DIR / f"{instance_id}.json"
    with open(fixture_path, encoding="utf-8") as f:
        data = json.load(f)

    override = data.get("docker_image_override", "")
    if override:
        base = override
    else:
        project = data["project_name"]
        cve_id = instance_id.split(".", 1)[1] if "." in instance_id else instance_id
        base = f"hwiwonlee/secb.eval.x86_64.{project}.{cve_id}:patch"

    # Mirror the logic from plugins/security/image_resolver.py
    if ":" in base:
        name, docker_tag = base.rsplit(":", 1)
    else:
        name, docker_tag = base, ""
    parts = name.split(".")
    suffix = ".".join(parts[3:]) if len(parts) >= 5 else name.replace("/", "-")
    tag = f"{suffix}-{docker_tag}" if docker_tag else suffix
    target = f"secb-tools:{tag}"

    return base, target


async def _run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _kill_run(
    proc: asyncio.subprocess.Process,
    instance_id: str,
    boss_id: str | None,
) -> None:
    """Kill a run: inner process inside arise-app, docker exec wrapper, and worker containers."""
    # 1. Kill the python process inside arise-app (docker exec kill doesn't propagate)
    await _run_cmd([
        "docker", "exec", "arise-app", "bash", "-c",
        f"for p in /proc/[0-9]*/cmdline; do "
        f"if grep -q '{instance_id}' \"$p\" 2>/dev/null; then "
        f"kill $(dirname $p | xargs basename) 2>/dev/null; fi; done",
    ])

    # 2. Kill the docker exec wrapper
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass

    # 3. Remove worker containers for this run
    if boss_id:
        rc, stdout, _ = await _run_cmd([
            "docker", "ps", "-q",
            "--filter", f"label=arise.root_id={boss_id}",
        ])
        if rc == 0 and stdout.strip():
            for cid in stdout.strip().split("\n"):
                await _run_cmd(["docker", "rm", "-f", cid.strip()])


async def build_image(instance_id: str) -> bool:
    """Pull base image and build secb-tools image. No uv/venv required."""
    base, target = _resolve_images(instance_id)

    # Skip if target already exists
    rc, _, _ = await _run_cmd(["docker", "image", "inspect", target])
    if rc == 0:
        logger.debug("[%s] image exists: %s", instance_id, target)
        return True

    # Pull base image
    rc, _, err = await _run_cmd(["docker", "pull", base])
    if rc != 0:
        logger.warning("[%s] pull FAILED (%s): %s", instance_id, base, err.strip()[:120])
        return False

    # Build tools layer
    dockerfile = str(PROJECT_ROOT / "deployment" / "secbench-tools.Dockerfile")
    rc, _, err = await _run_cmd([
        "docker", "build",
        "-f", dockerfile,
        "--build-arg", f"BASE_IMAGE={base}",
        "-t", target,
        str(PROJECT_ROOT),
    ])
    if rc != 0:
        logger.warning("[%s] build FAILED: %s", instance_id, err.strip()[:120])
        return False

    logger.info("[%s] image built: %s", instance_id, target)
    return True


async def build_images_parallel(ids: list[str], concurrency: int = 5) -> dict[str, bool]:
    """Build images with bounded parallelism."""
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, bool] = {}

    async def _build(iid: str) -> None:
        async with sem:
            results[iid] = await build_image(iid)

    await asyncio.gather(*[_build(iid) for iid in ids])
    return results


# ---------------------------------------------------------------------------
# DB helpers (via docker exec arise-db psql — no Python driver needed)
# ---------------------------------------------------------------------------
async def _psql(query: str) -> str:
    """Execute a SQL query and return raw stdout."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "arise-db",
        "psql", "-U", "arise", "-d", "arise_events", "-t", "-A", "-c", query,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


async def db_boss_id(instance_id: str, after_ts: str) -> str | None:
    """Find the boss aggregate_id for a recent run of this instance."""
    q = (
        "SELECT aggregate_id FROM events "
        "WHERE event_type='RunStarted' "
        f"AND payload->'domain_metadata'->>'instance_id' = '{instance_id}' "
        f"AND occurred_at > '{after_ts}'::timestamptz "
        "ORDER BY occurred_at DESC LIMIT 1"
    )
    val = await _psql(q)
    return val if val else None


async def db_agent_count(boss_id: str) -> int:
    """Count agents in the run hierarchy via recursive CTE."""
    q = (
        "WITH RECURSIVE h AS ("
        f"  SELECT '{boss_id}'::uuid AS aid "
        "  UNION "
        "  SELECT (e.payload->>'child_id')::uuid "
        "  FROM events e JOIN h ON e.aggregate_id = h.aid "
        "  WHERE e.event_type = 'ChildSpawned'"
        ") SELECT count(*) FROM h"
    )
    val = await _psql(q)
    return int(val) if val else 0


async def db_boss_manager_count(boss_id: str) -> tuple[int, int]:
    """Count how many direct children of the boss were evaluated as managers.

    Returns (total_children_evaluated, manager_count).
    """
    # Total boss children that have been evaluated
    q_total = (
        "SELECT count(*) FROM events ce "
        "JOIN events se ON (se.payload->>'child_id')::uuid = ce.aggregate_id "
        f"WHERE se.aggregate_id = '{boss_id}'::uuid "
        "AND se.event_type = 'ChildSpawned' "
        "AND ce.event_type = 'ComplexityEvaluated'"
    )
    # Of those, how many are managers
    q_mgr = (
        "SELECT count(*) FROM events ce "
        "JOIN events se ON (se.payload->>'child_id')::uuid = ce.aggregate_id "
        f"WHERE se.aggregate_id = '{boss_id}'::uuid "
        "AND se.event_type = 'ChildSpawned' "
        "AND ce.event_type = 'ComplexityEvaluated' "
        "AND ce.payload->>'determined_role' = 'manager'"
    )
    total_str = await _psql(q_total)
    mgr_str = await _psql(q_mgr)
    total = int(total_str) if total_str else 0
    mgr = int(mgr_str) if mgr_str else 0
    return total, mgr


async def db_boss_finished_spawning(boss_id: str) -> bool:
    """Check if the boss has finished its initial execution (spawned children)."""
    q = (
        "SELECT count(*) FROM events "
        f"WHERE aggregate_id = '{boss_id}'::uuid "
        "AND event_type = 'AgentExecutionFinished'"
    )
    val = await _psql(q)
    return int(val) > 0 if val else False


async def _cleanup_db(boss_id: str) -> None:
    """Delete all events for a run hierarchy (boss + children) to keep the DB clean."""
    q = (
        "WITH RECURSIVE h AS ("
        f"  SELECT '{boss_id}'::uuid AS aid "
        "  UNION "
        "  SELECT (e.payload->>'child_id')::uuid "
        "  FROM events e JOIN h ON e.aggregate_id = h.aid "
        "  WHERE e.event_type = 'ChildSpawned'"
        ") DELETE FROM events WHERE aggregate_id IN (SELECT aid FROM h)"
    )
    result = await _psql(q)
    logger.debug("[%s] cleaned up DB: %s", boss_id, result)


async def db_last_event_snapshot(boss_id: str) -> tuple[float, str, str]:
    """Return (seconds_since_last_event, agent_id, event_type) for run hierarchy."""
    q = (
        "WITH RECURSIVE h AS ("
        f"  SELECT '{boss_id}'::uuid AS aid "
        "  UNION "
        "  SELECT (e.payload->>'child_id')::uuid "
        "  FROM events e JOIN h ON e.aggregate_id = h.aid "
        "  WHERE e.event_type = 'ChildSpawned'"
        "), last AS ("
        "  SELECT e.aggregate_id, e.event_type, e.occurred_at "
        "  FROM events e JOIN h ON e.aggregate_id = h.aid "
        "  ORDER BY e.occurred_at DESC "
        "  LIMIT 1"
        ") SELECT "
        "COALESCE(EXTRACT(epoch FROM (now() - occurred_at)), 0), "
        "COALESCE(aggregate_id::text, ''), "
        "COALESCE(event_type, '') "
        "FROM last"
    )
    val = await _psql(q)
    if not val:
        return 0.0, "", ""
    parts = val.split("|")
    if len(parts) != 3:
        return 0.0, "", ""
    age_s, agent_id, event_type = parts
    try:
        age = float(age_s)
    except (ValueError, TypeError):
        age = 0.0
    return age, agent_id, event_type


async def db_run_completed(boss_id: str) -> str | None:
    """Return final_status if RunCompleted, else None."""
    q = (
        "SELECT payload->>'final_status' FROM events "
        f"WHERE aggregate_id = '{boss_id}'::uuid "
        "AND event_type = 'RunCompleted' "
        "ORDER BY occurred_at DESC LIMIT 1"
    )
    val = await _psql(q)
    return val if val else None


async def db_stale_analyzing_children(
    boss_id: str, stale_seconds: float, limit: int = 5,
) -> list[tuple[str, float, str]]:
    """Return stale analyzing descendants as (agent_id, stale_sec, last_event_type)."""
    q = (
        "WITH RECURSIVE h AS ("
        f"  SELECT '{boss_id}'::uuid AS aid "
        "  UNION "
        "  SELECT (e.payload->>'child_id')::uuid "
        "  FROM events e JOIN h ON e.aggregate_id = h.aid "
        "  WHERE e.event_type = 'ChildSpawned'"
        "), state_events AS ("
        "  SELECT e.aggregate_id, e.sequence_number, e.event_type, e.payload "
        "  FROM events e JOIN h ON e.aggregate_id = h.aid "
        "  WHERE e.event_type IN ('StatusChanged','WorkCompleted','WorkFailed','RetryScheduled')"
        "), latest_state AS ("
        "  SELECT DISTINCT ON (aggregate_id) aggregate_id, event_type, payload "
        "  FROM state_events ORDER BY aggregate_id, sequence_number DESC"
        "), mapped AS ("
        "  SELECT aggregate_id, "
        "    CASE "
        "      WHEN event_type='StatusChanged' THEN COALESCE(payload->>'new_status','unknown') "
        "      WHEN event_type='WorkCompleted' THEN 'completed' "
        "      WHEN event_type='WorkFailed' THEN 'failed' "
        "      WHEN event_type='RetryScheduled' THEN 'analyzing' "
        "      ELSE 'unknown' "
        "    END AS status "
        "  FROM latest_state"
        "), last_event AS ("
        "  SELECT DISTINCT ON (e.aggregate_id) e.aggregate_id, e.event_type, e.occurred_at "
        "  FROM events e JOIN h ON e.aggregate_id = h.aid "
        "  ORDER BY e.aggregate_id, e.sequence_number DESC"
        ") "
        "SELECT le.aggregate_id::text, "
        "       EXTRACT(epoch FROM (now() - le.occurred_at)), "
        "       le.event_type "
        "FROM mapped m "
        "JOIN last_event le ON le.aggregate_id = m.aggregate_id "
        f"WHERE m.status = 'analyzing' AND m.aggregate_id <> '{boss_id}'::uuid "
        f"  AND EXTRACT(epoch FROM (now() - le.occurred_at)) > {int(stale_seconds)} "
        "ORDER BY 2 DESC "
        f"LIMIT {int(limit)}"
    )
    out = await _psql(q)
    if not out:
        return []
    rows: list[tuple[str, float, str]] = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        aid, stale_s, event_type = parts
        try:
            stale = float(stale_s)
        except (ValueError, TypeError):
            continue
        rows.append((aid, stale, event_type))
    return rows


async def _rescue_stale_child_retry(
    *,
    instance_id: str,
    agent_id: str,
    stale_seconds: float,
    last_event_type: str,
) -> bool:
    """Ask the app to fail+retry a stale child before escalating to run kill."""
    reason = (
        f"Batch pre-kill rescue: stale analyzing child "
        f"(idle={stale_seconds:.0f}s, last_event={last_event_type or 'unknown'})"
    )
    logger.warning(
        "[%s] RESCUE child=%s idle=%.0fs last=%s — scheduling fail+retry",
        instance_id,
        agent_id[:8],
        stale_seconds,
        last_event_type or "unknown",
    )
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "arise-app",
        "python", "main.py", "retry-stale-child",
        "--agent-id", agent_id,
        "--reason", reason,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return_code = proc.returncode if proc.returncode is not None else 1
    if return_code != 0:
        logger.warning(
            "[%s] RESCUE failed for child=%s: %s",
            instance_id,
            agent_id[:8],
            out.decode(errors="replace").strip()[-240:],
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Single-instance runner
# ---------------------------------------------------------------------------
async def run_instance(instance_id: str, attempt: int) -> RunResult:
    """Launch one SEC-bench run, monitor it, return the result."""
    result = RunResult(instance_id=instance_id, tries=attempt)
    fixture_path = FIXTURE_DIR / f"{instance_id}.json"

    with open(fixture_path, encoding="utf-8") as f:
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

    # Launch inside arise-app container
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "arise-app",
        "python", "main.py", "run",
        "--domain-context-file", f"plugins/security/tests/fixtures/{instance_id}.json",
        "--domain", "security",
        task,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    # Wait for boss_id to appear (up to 90s)
    boss_id: str | None = None
    for _ in range(90):
        if proc.returncode is not None:
            break
        await asyncio.sleep(1)
        boss_id = await db_boss_id(instance_id, result.started_at)
        if boss_id:
            break

    if not boss_id:
        # Process likely died immediately (missing image, config error, etc.)
        try:
            stdout_bytes = await asyncio.wait_for(proc.stdout.read(2000), timeout=5)  # type: ignore[union-attr]
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
    rescued_children: set[str] = set()
    last_rescue_at_elapsed: float = -1.0

    # ---- Monitor loop ----
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
            return result

        # Under-decomposition checks
        if elapsed > DECOMP_GRACE_SEC:
            agents = await db_agent_count(boss_id)

            # 1. Structural check (aggressive): as soon as boss has finished
            #    spawning, check if enough children are managers. Kill
            #    immediately once it's impossible to reach MIN_BOSS_MANAGERS.
            boss_done = await db_boss_finished_spawning(boss_id)
            if boss_done:
                total_eval, mgr_count = await db_boss_manager_count(boss_id)
                # How many children haven't been evaluated yet?
                q_total_children = (
                    "SELECT count(*) FROM events "
                    f"WHERE aggregate_id = '{boss_id}'::uuid "
                    "AND event_type = 'ChildSpawned'"
                )
                total_children = int(await _psql(q_total_children) or 0)
                unevaluated = total_children - total_eval
                # Best case: all unevaluated become managers
                max_possible_managers = mgr_count + unevaluated

                if total_eval > 0 and max_possible_managers < MIN_BOSS_MANAGERS:
                    status = await db_run_completed(boss_id)
                    if status is not None:
                        break
                    logger.warning(
                        "[%s] UNDER-DECOMPOSED (structural): %d/%d evaluated as managers, "
                        "%d unevaluated, max possible %d < %d needed — killing at %.0fs",
                        instance_id, mgr_count, total_eval,
                        unevaluated, max_possible_managers,
                        MIN_BOSS_MANAGERS, elapsed,
                    )
                    await _kill_run(proc, instance_id, boss_id)
                    result.status = "under_decomposed"
                    result.num_agents = agents
                    result.duration_seconds = elapsed
                    result.error_message = (
                        f"structural: {mgr_count}/{total_eval} managers, "
                        f"max possible {max_possible_managers} < {MIN_BOSS_MANAGERS}"
                    )
                    result.completed_at = datetime.now(timezone.utc).isoformat()
                    return result

            # 2. Raw agent-count fallback (conservative):
            #    Only when boss finished spawning, ALL direct children were
            #    assessed, and an extended grace elapsed. This avoids killing
            #    runs that are still in pending-assessment warmup.
            if boss_done and elapsed > UNDER_DECOMP_COUNT_GRACE_SEC:
                total_eval, _mgr_count = await db_boss_manager_count(boss_id)
                q_total_children = (
                    "SELECT count(*) FROM events "
                    f"WHERE aggregate_id = '{boss_id}'::uuid "
                    "AND event_type = 'ChildSpawned'"
                )
                total_children = int(await _psql(q_total_children) or 0)
                all_children_assessed = total_children > 0 and total_eval >= total_children
                if all_children_assessed and agents < UNDER_DECOMP_THRESHOLD:
                    status = await db_run_completed(boss_id)
                    if status is not None:
                        break
                    logger.warning(
                        "[%s] UNDER-DECOMPOSED (count): %d agents after %.0fs "
                        "(%d/%d children assessed) — killing",
                        instance_id, agents, elapsed, total_eval, total_children,
                    )
                    await _kill_run(proc, instance_id, boss_id)
                    result.status = "under_decomposed"
                    result.num_agents = agents
                    result.duration_seconds = elapsed
                    result.error_message = (
                        f"only {agents} agents (threshold={UNDER_DECOMP_THRESHOLD}, "
                        f"children_assessed={total_eval}/{total_children})"
                    )
                    result.completed_at = datetime.now(timezone.utc).isoformat()
                    return result

        # Stall detection: no new events for STALL_SEC = hanging LLM call
        if elapsed > DECOMP_GRACE_SEC:
            stale_secs, last_agent_id, last_event_type = await db_last_event_snapshot(boss_id)
            # Pre-kill rescue at child granularity (smaller blast radius than run kill).
            if stale_secs > CHILD_RESCUE_SEC:
                candidates = await db_stale_analyzing_children(
                    boss_id, stale_seconds=CHILD_RESCUE_SEC, limit=5,
                )
                candidate = next(
                    ((aid, s, ev) for (aid, s, ev) in candidates if aid not in rescued_children),
                    None,
                )
                if candidate is not None:
                    aid, child_stale, child_last_event = candidate
                    rescued = await _rescue_stale_child_retry(
                        instance_id=instance_id,
                        agent_id=aid,
                        stale_seconds=child_stale,
                        last_event_type=child_last_event,
                    )
                    if rescued:
                        rescued_children.add(aid)
                        last_rescue_at_elapsed = elapsed
                        await asyncio.sleep(CHILD_RESCUE_GRACE_SEC)
                        continue
            if stale_secs > STALL_SEC:
                if (
                    last_rescue_at_elapsed >= 0
                    and (elapsed - last_rescue_at_elapsed) < CHILD_RESCUE_GRACE_SEC
                ):
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue
                agents = await db_agent_count(boss_id)
                logger.warning(
                    "[%s] STALLED: no events for %.0fs (%d agents), last=%s/%s — killing",
                    instance_id,
                    stale_secs,
                    agents,
                    last_agent_id[:8] if last_agent_id else "unknown",
                    last_event_type or "unknown",
                )
                await _kill_run(proc, instance_id, boss_id)
                result.status = "failed"
                result.num_agents = agents
                result.duration_seconds = elapsed
                result.error_message = f"stalled {stale_secs:.0f}s"
                result.completed_at = datetime.now(timezone.utc).isoformat()
                return result

        await asyncio.sleep(POLL_INTERVAL_SEC)

    # ---- Process exited ----
    await proc.wait()
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    result.duration_seconds = round(elapsed, 1)
    result.completed_at = datetime.now(timezone.utc).isoformat()
    result.num_agents = await db_agent_count(boss_id)

    final = await db_run_completed(boss_id)
    if proc.returncode == 0 and final == "success":
        result.status = "success"
    elif elapsed > 120:
        # Structural under-decomposition: check boss manager children first
        total_eval, mgr_count = await db_boss_manager_count(boss_id)
        if total_eval >= 3 and mgr_count < MIN_BOSS_MANAGERS:
            result.status = "under_decomposed"
            result.error_message = (
                f"structural: {mgr_count}/{total_eval} managers "
                f"(need {MIN_BOSS_MANAGERS})"
            )
        elif result.num_agents < UNDER_DECOMP_THRESHOLD:
            result.status = "under_decomposed"
            result.error_message = f"only {result.num_agents} agents"
        elif final:
            result.status = "failed"
            result.error_message = f"final_status={final}"
        else:
            result.status = "failed"
            result.error_message = f"exit_code={proc.returncode}"
    elif final:
        result.status = "failed"
        result.error_message = f"final_status={final}"
    else:
        result.status = "failed"
        result.error_message = f"exit_code={proc.returncode}"

    logger.info(
        "[%s] %s — %d agents, %.0fs",
        instance_id, result.status, result.num_agents, elapsed,
    )
    return result


# ---------------------------------------------------------------------------
# Concurrent execution with retry (semaphore-based)
# ---------------------------------------------------------------------------
async def _remove_image(iid: str) -> None:
    """Remove the secb-tools image for a fixture to free disk space."""
    _, target = _resolve_images(iid)
    rc, _, _ = await _run_cmd(["docker", "rmi", "-f", target])
    if rc == 0:
        logger.debug("[%s] removed image %s", iid, target)


async def _run_with_retry(
    iid: str,
    sem: asyncio.Semaphore,
    max_retries: int,
    stagger_delay: float,
) -> RunResult:
    """Run a single instance with retries, respecting the concurrency semaphore."""
    if stagger_delay > 0:
        await asyncio.sleep(stagger_delay)

    # Build image on-demand (just before running, not all upfront)
    img_ok = await build_image(iid)
    if not img_ok:
        r = RunResult(
            instance_id=iid,
            status="image_error",
            error_message="Docker image build/pull failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        upsert_csv(r)
        return r

    result = RunResult(instance_id=iid)
    for attempt in range(1, max_retries + 1):
        async with sem:
            try:
                result = await run_instance(iid, attempt)
            except Exception as exc:
                result = RunResult(
                    instance_id=iid,
                    status="error",
                    tries=attempt,
                    error_message=str(exc)[:200],
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

        # Retry policy:
        # - Generic failed/error runs retry only after meaningful progress
        #   (late-stage failures), to avoid hot-looping deterministic early crashes.
        # - Hang signatures must be retried even when "early" because they are
        #   often transient provider/runtime stalls.
        early_death = result.duration_seconds < 600  # under 10 min = "early"
        is_hang_signature = (
            result.status == "timeout"
            or (
                result.status == "failed"
                and "stalled" in (result.error_message or "").lower()
            )
        )
        retryable = is_hang_signature or (
            result.status in ("failed", "error") and not early_death
        )
        if not retryable or attempt >= max_retries:
            break
        # NOTE: Do NOT clean up DB events — past run data is irreplaceable.
        # Previously called _cleanup_db(result.boss_id) here, removed to preserve
        # failed-attempt event data for post-mortem analysis.
        logger.info("[%s] will retry (%d/%d)", iid, attempt + 1, max_retries)
        await asyncio.sleep(5)

    result.tries = attempt  # noqa: F821 (loop variable)
    upsert_csv(result)

    # Clean up image after all retries are done to free disk space.
    # Image will be rebuilt if this fixture is re-run in a future batch.
    await _remove_image(iid)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def async_main(args: argparse.Namespace) -> None:
    # 1. Generate fixtures
    logger.info("═══ Phase 1: Generate fixtures ═══")
    all_ids = generate_fixtures()

    # 2. Load resume state
    completed = load_completed()
    remaining = [iid for iid in all_ids if iid not in completed]
    logger.info(
        "%d total · %d already done · %d remaining",
        len(all_ids), len(completed), len(remaining),
    )
    if not remaining:
        logger.info("All instances already processed. Nothing to do.")
        return

    if args.limit > 0:
        remaining = remaining[:args.limit]
        logger.info("Limited to first %d instance(s)", args.limit)

    # Images are now built on-demand inside _run_with_retry (and cleaned up
    # after each run completes). This keeps at most batch_size images on disk
    # instead of pre-building all 200+.
    runnable = remaining

    # 3. Run all instances concurrently, bounded by semaphore
    logger.info(
        "═══ Phase 3: Running %d instances (%d concurrent) ═══",
        len(runnable), args.batch_size,
    )
    sem = asyncio.Semaphore(args.batch_size)
    tasks = [
        asyncio.create_task(
            _run_with_retry(iid, sem, args.max_retries, stagger_delay=i * 2),
        )
        for i, iid in enumerate(runnable)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 5. Summary
    status_counts: dict[str, int] = {}
    for r in results:
        if isinstance(r, RunResult):
            status_counts[r.status] = status_counts.get(r.status, 0) + 1
        else:
            status_counts["error"] = status_counts.get("error", 0) + 1

    logger.info("═══ All instances complete ═══")
    for s, cnt in sorted(status_counts.items()):
        logger.info("  %-20s %d", s, cnt)
    logger.info("Results: %s", CSV_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch SEC-bench runner")
    parser.add_argument("--batch-size", type=int, default=15)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0,
                        help="Process only the first N remaining instances (0=all)")
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
