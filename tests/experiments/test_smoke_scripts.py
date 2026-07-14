"""Regression tests for experiment smoke entrypoints."""

from pathlib import Path

from config.overlay import resolve_overlay


REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPTS = (
    REPO_ROOT / "experiments" / "n1-openhands-linear" / "smoke.sh",
    REPO_ROOT / "experiments" / "b3-direct-compact" / "smoke.sh",
    REPO_ROOT / "experiments" / "b4-boss-manager-worker" / "smoke.sh",
)
SMOKE_CONFIGS = (
    REPO_ROOT
    / "experiments"
    / "n1-openhands-linear"
    / "configs"
    / "N1-openhands-linear.yaml",
    REPO_ROOT
    / "experiments"
    / "b3-direct-compact"
    / "configs"
    / "B3-direct-compact.yaml",
    REPO_ROOT
    / "experiments"
    / "b4-boss-manager-worker"
    / "configs"
    / "B4-boss-manager-worker.yaml",
)


def test_smoke_scripts_use_process_environment_for_secrets() -> None:
    """Smoke scripts must not source secret managers or plaintext env files."""
    # Given: The curated smoke scripts operators use for N1/B3/B4.
    scripts = {path: path.read_text(encoding="utf-8") for path in SMOKE_SCRIPTS}

    # When: Inspecting their credential bootstrap behavior.
    combined = "\n".join(scripts.values())

    # Then: Provider and database secrets are required from the existing process env.
    assert "OPENAI_API_KEY must be set in the process environment" in combined
    assert "POSTGRES_PASSWORD must be set in the process environment" in combined

    # And: The scripts do not depend on Bitwarden or plaintext deployment env files.
    for path, text in scripts.items():
        assert "bw " not in text, f"{path} must not call Bitwarden"
        assert "bitwarden" not in text.lower(), f"{path} must not reference Bitwarden"
        assert "source deployment/.env" not in text, f"{path} must not source deployment/.env"


def test_smoke_step_watchdog_covers_worker_timeout() -> None:
    """Outer orchestration watchdog must not fail long valid worker calls first."""
    for path in SMOKE_CONFIGS:
        config = resolve_overlay(path, repo_root=REPO_ROOT)
        worker_timeout = config["worker"]["timeout"]
        step_timeout = config["orchestration"]["concurrency"]["max_agent_step_seconds"]
        assert step_timeout >= worker_timeout, (
            f"{path} has worker.timeout={worker_timeout} but "
            f"orchestration.concurrency.max_agent_step_seconds={step_timeout}"
        )
