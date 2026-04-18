"""NullPromptStrategy returns None from every extend_*_prompt hook."""

from uuid import uuid4

from core.application.services import PromptBuilder
from core.application.services.prompt.prompt_strategy import PromptContext
from core.domain.values.enums import AgentRole
from plugins.security.null_prompt_strategy import NullPromptStrategy
from plugins.security.plugin import SecurityDomainPlugin
from plugins.security.prompt_strategy import SecBenchPromptStrategy
from plugins.security.tests.test_prompt_building import PROMPTS_DIR


def _make_context() -> PromptContext:
    return PromptContext(
        task_description="any",
        agent_id=uuid4(),
        agent_role=AgentRole.WORKER,
        default_tool="claude_code",
    )


def _make_chain() -> object:
    # Reuse PromptBuilder.chain() to obtain a real TemplateChain (same idiom as
    # plugins/security/tests/test_plugin_tool_injection.py).
    builder = PromptBuilder(
        template_dir=PROMPTS_DIR,
        default_tool="claude_code",
        strategy=SecBenchPromptStrategy(),
    )
    return builder.chain()


def test_null_strategy_returns_none_from_every_extension_hook() -> None:
    """All extend_* methods on NullPromptStrategy return None for any context."""
    # Given: a NullPromptStrategy and a real TemplateChain + PromptContext
    strategy = NullPromptStrategy()
    chain = _make_chain()
    ctx = _make_context()

    # When: each extend_*_prompt method is called
    # Then: all return None (defer to defaults)
    assert strategy.extend_assessment_prompt(chain, ctx) is None  # type: ignore[arg-type]
    assert strategy.extend_boss_prompt(chain, ctx) is None  # type: ignore[arg-type]
    assert strategy.extend_manager_prompt(chain, ctx) is None  # type: ignore[arg-type]
    assert strategy.extend_worker_prompt(chain, ctx) is None  # type: ignore[arg-type]


def test_plugin_returns_null_strategy_when_settings_flag_set() -> None:
    """SecurityDomainPlugin.get_prompt_strategy returns NullPromptStrategy when use_null_prompt_strategy=True."""
    # Given: a plugin configured to use NullPromptStrategy
    plugin = SecurityDomainPlugin(
        enabled_tools=["valgrind"],
        use_null_prompt_strategy=True,
    )

    # When: plugin is asked for its prompt strategy
    strategy = plugin.get_prompt_strategy()

    # Then: it returns a NullPromptStrategy instance
    assert isinstance(strategy, NullPromptStrategy)


def test_plugin_returns_secbench_strategy_when_flag_unset() -> None:
    """Default plugin returns SecBenchPromptStrategy (regression test)."""
    # Given: a plugin with default prompt-strategy selection
    plugin = SecurityDomainPlugin(enabled_tools=["valgrind"])

    # When: plugin is asked for its prompt strategy
    strategy = plugin.get_prompt_strategy()

    # Then: it returns SecBenchPromptStrategy (legacy behavior preserved)
    assert isinstance(strategy, SecBenchPromptStrategy)
