"""Unit tests for ``text.bash_classifier`` (design doc §10)."""

from __future__ import annotations

import pytest

from experiments.shared.scripts.analysis.text.bash_classifier import (
    BashSubtype,
    classify_bash_command,
)


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("curl http://x", BashSubtype.WEB_VIA_SHELL),
        ("nmap -sV 127.0.0.1", BashSubtype.RECON),
        ("sqlmap -u target --dbs", BashSubtype.EXPLOIT),
        ("bandit -r .", BashSubtype.SECURITY_SCAN),
        ("gcc -o foo foo.c", BashSubtype.BUILD),
        ("pytest -q", BashSubtype.TEST_EXEC),
        ("git log", BashSubtype.GIT),
        ("cat /etc/passwd", BashSubtype.OTHER_SHELL),
    ],
)
def test_representative_command_per_subtype(cmd, expected):
    # Given: a representative Bash command for each subtype
    # When: it is classified
    actual = classify_bash_command(cmd)
    # Then: the expected subtype is returned
    assert actual == expected


def test_web_wins_over_build_when_curl_piped_into_gcc():
    # Given: a pipeline that downloads then compiles
    cmd = "curl https://x | gcc -"
    # When: classified
    result = classify_bash_command(cmd)
    # Then: WEB_VIA_SHELL wins by priority over BUILD
    assert result == BashSubtype.WEB_VIA_SHELL


def test_recon_wins_over_build_when_nmap_and_make_coexist():
    # Given: a compound command containing both ``nmap`` and ``make``
    cmd = "nmap -sV 10.0.0.1 && make all"
    # When: classified
    result = classify_bash_command(cmd)
    # Then: RECON wins by priority over BUILD
    assert result == BashSubtype.RECON


def test_empty_string_is_other_shell():
    # Given: an empty command
    # When: classified
    result = classify_bash_command("")
    # Then: OTHER_SHELL is returned (defensive early-out)
    assert result == BashSubtype.OTHER_SHELL


def test_whitespace_only_is_other_shell():
    # Given: a command consisting only of whitespace
    # When: classified
    result = classify_bash_command("   \t  \n  ")
    # Then: OTHER_SHELL is returned (no pattern can match)
    assert result == BashSubtype.OTHER_SHELL


def test_uppercase_curl_matches_web_via_shell():
    # Given: a Bash command using uppercase tooling
    cmd = "CURL https://x"
    # When: classified
    result = classify_bash_command(cmd)
    # Then: case-insensitive matching catches it
    assert result == BashSubtype.WEB_VIA_SHELL


def test_git_clone_with_url_is_classified_as_web_via_shell():
    # Given: ``git clone`` with an embedded URL — documented conservative
    # bias from §14.6: the ``https?://`` branch matches anywhere in the
    # command, so URL-bearing commands win over GIT.
    cmd = "git clone https://github.com/x.git"
    # When: classified
    result = classify_bash_command(cmd)
    # Then: WEB_VIA_SHELL wins by priority (acceptable false-positive bias)
    assert result == BashSubtype.WEB_VIA_SHELL
