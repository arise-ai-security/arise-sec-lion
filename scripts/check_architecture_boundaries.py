"""Guard against architecture boundary regressions."""

from collections.abc import Iterable
from pathlib import Path
import re
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = REPO_ROOT / "core"


FORBIDDEN_CORE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^\s*from\s+infrastructure\b", re.MULTILINE),
        "core must not import from infrastructure",
    ),
    (
        re.compile(r"^\s*import\s+infrastructure\b", re.MULTILINE),
        "core must not import infrastructure modules",
    ),
    (
        re.compile(r"^\s*from\s+openhands\b", re.MULTILINE),
        "OpenHands SDK imports belong in infrastructure adapters",
    ),
    (
        re.compile(r"^\s*import\s+openhands\b", re.MULTILINE),
        "OpenHands SDK imports belong in infrastructure adapters",
    ),
    (
        re.compile(r"CmdOutputMetadata\("),
        "OpenHands SDK metadata parsing belongs in infrastructure adapters",
    ),
    (
        re.compile(r"Observation:\s*kind="),
        "OpenHands SDK repr parsing belongs in infrastructure adapters",
    ),
)


def _iter_core_python_files(core_root: Path = CORE_ROOT) -> Iterable[Path]:
    return sorted(
        path for path in core_root.rglob("*.py") if "tests" not in path.parts
    )


def find_boundary_violations(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return human-readable architecture boundary violations."""
    violations: list[str] = []
    core_root = repo_root / "core"

    for path in _iter_core_python_files(core_root):
        content = path.read_text(encoding="utf-8")
        rel_path = path.relative_to(repo_root)
        for pattern, message in FORBIDDEN_CORE_PATTERNS:
            match = pattern.search(content)
            if match is None:
                continue
            line = content.count("\n", 0, match.start()) + 1
            violations.append(f"{rel_path}:{line}: {message}")

    for path in sorted(repo_root.glob("*.domain-fixture.*")):
        violations.append(
            f"{path.relative_to(repo_root)}:1: "
            "Domain fixture files must live under plugins/ or tests/"
        )

    return violations


def main() -> int:
    """Print violations and exit non-zero when any boundary check fails."""
    violations = find_boundary_violations()
    if not violations:
        return 0

    print("Architecture boundary violations detected:", file=sys.stderr)
    for violation in violations:
        print(f" - {violation}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
