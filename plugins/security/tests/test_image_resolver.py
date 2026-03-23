"""Tests for SEC-bench image tag resolution."""

from plugins.security.image_resolver import resolve_secbench_image


def test_returns_base_image_when_tools_disabled() -> None:
    base = "hwiwonlee/secb.eval.x86_64.gpac.cve-2023-2838"
    assert resolve_secbench_image(base, security_tools_enabled=False) == base


def test_maps_secbench_image_to_tool_tag() -> None:
    base = "hwiwonlee/secb.eval.x86_64.gpac.cve-2023-2838"
    assert resolve_secbench_image(base, security_tools_enabled=True) == "secb-tools:gpac.cve-2023-2838"


def test_fallback_for_nonstandard_image_names() -> None:
    base = "ghcr.io/acme/custom-image"
    assert resolve_secbench_image(base, security_tools_enabled=True) == "secb-tools:ghcr.io-acme-custom-image"
