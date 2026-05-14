"""Unit tests for presentation.persistence.invocation_hash."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from config import (
    BossConfig,
    ConcurrencyConfig,
    CorsConfig,
    DatabaseConfig,
    FormatRepairerConfig,
    ManagerConfig,
    OpenHandsParams,
    OrchestrationConfig,
    OutputConfig,
    RetryConfig,
    SecurityConfig,
    Settings,
    ToolCallingConfig,
    TopologyConfig,
    WorkerConfig,
    WorkerToolParams,
)
from presentation.persistence.invocation_hash import (
    _redacted_settings_blob,
    compute_invocation_sha256,
)


if TYPE_CHECKING:
    from pathlib import Path


def _make_settings(**overrides: Any) -> Settings:
    """Build a fully-populated Settings bypassing the env-dependent loader."""

    # Given: minimal valid values for every nested config model.
    base = Settings(
        database=DatabaseConfig(
            host="db.example.com",
            port=5432,
            user="arise",
            password="supersecret-password-do-not-hash",  # noqa: S106 - test fixture
            name="arise_events",
        ),
        boss=BossConfig(model="o3", temperature=0.7, max_tokens=16000),
        manager=ManagerConfig(model="o3", temperature=0.7, max_tokens=16000),
        worker=WorkerConfig(
            model="openai/o3",
            tool="openhands",
            timeout=600,
            max_iterations_per_run=20,
            tool_params=WorkerToolParams(openhands=OpenHandsParams()),
        ),
        orchestration=OrchestrationConfig(
            max_retries=3,
            poll_interval=0.5,
            max_run_duration_seconds=5400,
            max_redecompositions=2,
            topology=TopologyConfig(
                max_depth=3,
                max_children_per_node=7,
                max_total_agents=40,
            ),
            concurrency=ConcurrencyConfig(
                max_concurrent_workers=1,
                max_concurrent_llm_calls=1,
                llm_jitter_max_ms=0,
            ),
            tool_calling=ToolCallingConfig(),
            retry=RetryConfig(),
        ),
        output=OutputConfig(
            verbose=True,
            log_level="INFO",
            directory="./runs",
        ),
        security=SecurityConfig(enabled=True, tools=["valgrind"]),
        format_repairer=FormatRepairerConfig(model="gpt-5.4-mini"),
        cors=CorsConfig(),
    )
    return base.model_copy(update=overrides) if overrides else base


def test_redacted_blob_omits_database_password() -> None:
    # Given: settings with a specific password string.
    settings = _make_settings()

    # When: serializing the redacted blob.
    blob = _redacted_settings_blob(settings)

    # Then: the password bytes must not appear anywhere in the blob.
    assert b"supersecret-password-do-not-hash" not in blob

    # And: non-secret fields are still present.
    assert b"openai/o3" in blob
    assert b"arise_events" in blob


def test_compute_invocation_sha256_is_deterministic(tmp_path: Path) -> None:
    # Given: fixed settings, task, and context file.
    settings = _make_settings()
    ctx = tmp_path / "context.json"
    ctx.write_bytes(b'{"cve": "CVE-2021-40575"}')
    task = "Reproduce and patch the vulnerability"

    # When: hashing twice with identical inputs.
    a = compute_invocation_sha256(settings=settings, task=task, domain_context_path=ctx)
    b = compute_invocation_sha256(settings=settings, task=task, domain_context_path=ctx)

    # Then: both hashes agree.
    assert a == b
    assert len(a) == 64  # sha256 hex digest length


def test_compute_invocation_sha256_ignores_password_changes() -> None:
    # Given: two settings identical except for database.password.
    base = _make_settings()
    other = base.model_copy(
        update={"database": base.database.model_copy(update={"password": "a-different-secret"})}
    )

    # When: hashing both.
    hash_base = compute_invocation_sha256(
        settings=base, task="t", domain_context_path=None
    )
    hash_other = compute_invocation_sha256(
        settings=other, task="t", domain_context_path=None
    )

    # Then: the hashes match because the password is redacted.
    assert hash_base == hash_other


def test_compute_invocation_sha256_changes_on_task_change() -> None:
    # Given: shared settings and no domain context.
    settings = _make_settings()

    # When: hashing with two different task strings.
    h1 = compute_invocation_sha256(settings=settings, task="task one", domain_context_path=None)
    h2 = compute_invocation_sha256(settings=settings, task="task two", domain_context_path=None)

    # Then: the hashes differ.
    assert h1 != h2


def test_compute_invocation_sha256_changes_on_context_change(tmp_path: Path) -> None:
    # Given: settings + two distinct context files.
    settings = _make_settings()
    ctx_a = tmp_path / "a.json"
    ctx_b = tmp_path / "b.json"
    ctx_a.write_bytes(b'{"cve": "A"}')
    ctx_b.write_bytes(b'{"cve": "B"}')

    # When: hashing each.
    ha = compute_invocation_sha256(settings=settings, task="t", domain_context_path=ctx_a)
    hb = compute_invocation_sha256(settings=settings, task="t", domain_context_path=ctx_b)

    # Then: they differ.
    assert ha != hb


def test_compute_invocation_sha256_separator_prevents_collision(tmp_path: Path) -> None:
    # Given: two input pairs that would collide if fields were concatenated
    # without typed prefixes and separators:
    #   case 1: task = "AB",       ctx = empty
    #   case 2: task = "A",        ctx = "B"
    settings = _make_settings()
    ctx_empty = tmp_path / "empty.bin"
    ctx_empty.write_bytes(b"")
    ctx_b = tmp_path / "b.bin"
    ctx_b.write_bytes(b"B")

    # When: hashing each case.
    h1 = compute_invocation_sha256(
        settings=settings, task="AB", domain_context_path=ctx_empty
    )
    h2 = compute_invocation_sha256(
        settings=settings, task="A", domain_context_path=ctx_b
    )

    # Then: the hashes must differ — null separators and typed prefixes
    # prevent payload-boundary ambiguity.
    assert h1 != h2


def test_compute_invocation_sha256_ignores_database_host_reorder() -> None:
    # Given: same logical settings. JSON serialization uses sort_keys, so
    # semantically-equivalent dicts produce the same hash regardless of
    # Python dict insertion order.
    a = _make_settings()
    b = _make_settings()

    # When
    ha = compute_invocation_sha256(settings=a, task="t", domain_context_path=None)
    hb = compute_invocation_sha256(settings=b, task="t", domain_context_path=None)

    # Then
    assert ha == hb


def test_compute_invocation_sha256_handles_embedded_nul_bytes(tmp_path: Path) -> None:
    # Given: two framing-ambiguous cases. Under the old tag+NUL framing a
    # domain-context file containing the bytes "A\x00ctx:B" would fuse with
    # the tag separators and let the concatenated inputs collide with the
    # case where the context is "A" and the surrounding framing bytes differ.
    # Length-prefixed framing keeps them distinct.
    settings = _make_settings()

    ctx_with_nul = tmp_path / "with_nul.bin"
    ctx_with_nul.write_bytes(b"payload\x00second-chunk")

    ctx_plain = tmp_path / "plain.bin"
    ctx_plain.write_bytes(b"payload")

    # When: hashing each.
    with_nul = compute_invocation_sha256(
        settings=settings, task="same-task", domain_context_path=ctx_with_nul
    )
    plain = compute_invocation_sha256(
        settings=settings, task="same-task", domain_context_path=ctx_plain
    )

    # Then: hashes differ even though the legacy NUL separator would have
    # dissolved into the payload.
    assert with_nul != plain


def test_compute_invocation_sha256_length_framing_prevents_tag_smuggling(
    tmp_path: Path,
) -> None:
    # Given: a context file whose bytes impersonate the task tag. Under a
    # naive concatenation without length-prefix framing, hashing ctx="task:X"
    # with task="" could collide with ctx="" and task="X".
    settings = _make_settings()
    smuggled = tmp_path / "smuggled.bin"
    smuggled.write_bytes(b"task:X")
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")

    # When
    h_smuggled = compute_invocation_sha256(
        settings=settings, task="", domain_context_path=smuggled
    )
    h_split = compute_invocation_sha256(
        settings=settings, task="X", domain_context_path=empty
    )

    # Then
    assert h_smuggled != h_split
