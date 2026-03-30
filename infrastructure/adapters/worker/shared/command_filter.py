"""Config-driven command deny-list for worker shell commands."""

import logging
import re
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandFilter:
    """Rejects shell commands matching any deny pattern.

    Patterns are compiled once at construction. A denied command raises
    CommandDeniedError so callers can surface a clear message to the worker.
    """

    _compiled: tuple[re.Pattern[str], ...] = field(default=())

    @classmethod
    def from_patterns(cls, patterns: list[str]) -> "CommandFilter":
        compiled = tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        return cls(_compiled=compiled)

    @classmethod
    def disabled(cls) -> "CommandFilter":
        """A filter that allows everything."""
        return cls(_compiled=())

    @property
    def is_active(self) -> bool:
        return len(self._compiled) > 0

    def check(self, command: str) -> None:
        """Raise CommandDeniedError if command matches any deny pattern."""
        stripped = command.strip()
        if not stripped:
            return
        for pattern in self._compiled:
            if pattern.search(stripped):
                logger.warning(
                    "Command denied by filter: pattern=%s command=%s",
                    pattern.pattern,
                    stripped[:200],
                )
                raise CommandDeniedError(stripped, pattern.pattern)


class CommandDeniedError(Exception):
    """Raised when a shell command matches a deny-list pattern."""

    def __init__(self, command: str, pattern: str) -> None:
        self.command = command
        self.pattern = pattern
        super().__init__(
            f"Command denied: matches deny pattern '{pattern}'. Command: {command[:200]}"
        )
