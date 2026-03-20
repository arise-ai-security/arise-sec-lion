"""Docker image resolver for SEC-bench tool-enriched containers.

Maps base SEC-bench image names to their tool-enriched counterparts.
Kept in the security plugin because image naming policy is domain-specific.
"""


def resolve_secbench_image(
    base_image: str,
    security_tools_enabled: bool = False,
    image_prefix: str = "secb-tools",
) -> str:
    """Resolve a SEC-bench base image to its tool-enriched variant.

    Args:
        base_image: Base SEC-bench image (e.g., "hwiwonlee/secb.eval.x86_64.gpac.cve-2023-2838").
        security_tools_enabled: Whether security tools are enabled in config.
        image_prefix: Prefix for tool-enriched image tags.

    Returns:
        Tool-enriched image name if enabled, otherwise the base image unchanged.
    """
    if not security_tools_enabled:
        return base_image

    # Extract the tag portion after the last registry/repo prefix
    # hwiwonlee/secb.eval.x86_64.gpac.cve-2023-2838 → secb-tools:gpac.cve-2023-2838
    parts = base_image.split(".")
    if len(parts) >= 5:
        # secb.eval.x86_64.<project>.<cve> → <project>.<cve>
        tag = ".".join(parts[3:])
    else:
        tag = base_image.replace("/", "-")

    return f"{image_prefix}:{tag}"
