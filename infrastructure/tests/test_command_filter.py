import pytest

from infrastructure.adapters.worker.shared.command_filter import (
    CommandDeniedError,
    CommandFilter,
)


class TestCommandFilter:
    def test_allows_safe_commands(self) -> None:
        f = CommandFilter.from_patterns([r"curl\s+", r"git\s+checkout\s+[a-f0-9]{7,}"])
        f.check("ls -la")
        f.check("git diff HEAD")
        f.check("make -j4")

    def test_denies_matching_command(self) -> None:
        f = CommandFilter.from_patterns([r"curl\s+.*github\.com"])
        with pytest.raises(CommandDeniedError):
            f.check("curl https://github.com/repo/commit/abc123.patch")

    def test_denies_git_checkout_hash(self) -> None:
        f = CommandFilter.from_patterns([r"git\s+checkout\s+[a-f0-9]{7,}"])
        with pytest.raises(CommandDeniedError):
            f.check("git checkout abc1234def")

    def test_allows_git_checkout_branch(self) -> None:
        f = CommandFilter.from_patterns([r"git\s+checkout\s+[a-f0-9]{7,}"])
        f.check("git checkout main")
        f.check("git checkout -b feature")

    def test_case_insensitive(self) -> None:
        f = CommandFilter.from_patterns([r"CURL\s+"])
        with pytest.raises(CommandDeniedError):
            f.check("curl http://example.com")

    def test_disabled_allows_everything(self) -> None:
        f = CommandFilter.disabled()
        assert not f.is_active
        f.check("curl http://evil.com")

    def test_empty_patterns_allows_everything(self) -> None:
        f = CommandFilter.from_patterns([])
        assert not f.is_active
        f.check("anything")

    def test_empty_command_allowed(self) -> None:
        f = CommandFilter.from_patterns([r"curl\s+"])
        f.check("")
        f.check("   ")

    def test_denies_cherry_pick(self) -> None:
        f = CommandFilter.from_patterns([r"git\s+cherry-pick"])
        with pytest.raises(CommandDeniedError):
            f.check("git cherry-pick abc123")

    def test_denies_wget_exploit_db(self) -> None:
        f = CommandFilter.from_patterns([r"wget\s+.*exploit-db"])
        with pytest.raises(CommandDeniedError):
            f.check("wget https://www.exploit-db.com/exploits/12345")

    def test_allows_wget_for_poc(self) -> None:
        """wget to non-denied URLs should be allowed."""
        f = CommandFilter.from_patterns(
            [r"wget\s+.*exploit-db", r"wget\s+.*github\.com.*(commit|patch)"]
        )
        f.check("wget --no-check-certificate https://example.com/poc.bin")

    def test_git_diff_allowed(self) -> None:
        f = CommandFilter.from_patterns([r"git\s+checkout\s+[a-f0-9]{7,}", r"git\s+cherry-pick"])
        f.check("git diff --no-color HEAD")
        f.check("git show abc123")
        f.check("git reset --hard abc123")
        f.check("git apply /testcase/model_patch.diff")

    def test_error_contains_pattern_and_command(self) -> None:
        f = CommandFilter.from_patterns([r"curl\s+"])
        with pytest.raises(CommandDeniedError) as exc_info:
            f.check("curl http://evil.com")
        assert "curl" in str(exc_info.value)
        assert "deny pattern" in str(exc_info.value)
