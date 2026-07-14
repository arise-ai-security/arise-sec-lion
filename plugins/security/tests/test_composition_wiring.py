"""Security plugin composition wiring tests."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from bootstrap import composition


class FakeSecurityDomainPlugin:
    """Capture composition arguments without constructing the concrete plugin."""

    def __init__(self, **kwargs: object) -> None:
        self.arguments = kwargs
        self.container_runtime: object | None = None

    def set_container_runtime(self, runtime: object) -> None:
        self.container_runtime = runtime

    def get_prompt_strategy(self) -> None:
        return None


def test_route_policy_version_comes_from_plugin_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = cast(
        Any,
        SimpleNamespace(
            domain_plugins={"security": {"route_policy_version": "catalog-v2"}},
            orchestration=SimpleNamespace(
                shared_code_prefix_first=False,
                treatment_version="arbitrary-provenance",
            ),
        ),
    )
    monkeypatch.setattr(composition, "SecurityDomainPlugin", FakeSecurityDomainPlugin)

    components = composition._build_security_components(settings)

    plugin = cast(FakeSecurityDomainPlugin, components.plugin)
    assert plugin.arguments["route_policy_version"] == "catalog-v2"
    assert "treatment_version" not in plugin.arguments


def test_route_policy_version_has_security_default() -> None:
    settings = cast(Any, SimpleNamespace(domain_plugins={"security": {}}))

    security = composition._security_config(settings)

    assert security.route_policy_version == "secbench-manager-recovery-v1"
