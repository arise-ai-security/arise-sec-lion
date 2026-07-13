"""Regression tests for experiment smoke entrypoints."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPTS = (
    REPO_ROOT / "experiments" / "n1-openhands-linear" / "smoke.sh",
    REPO_ROOT / "experiments" / "b3-direct-compact" / "smoke.sh",
    REPO_ROOT / "experiments" / "b4-boss-manager-worker" / "smoke.sh",
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
