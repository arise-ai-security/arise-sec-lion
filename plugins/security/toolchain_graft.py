"""Publish SEC-bench tool images by grafting one immutable tool layer stack."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

REGCTL_VERSION = "v0.11.5"
REGCTL_ASSETS = {
    ("Darwin", "arm64"): (
        "regctl-darwin-arm64",
        "f4d536d64d0c3cc1db7400902175a1c314675991d22e87e15c319501a2676d3f",
    ),
    ("Darwin", "x86_64"): (
        "regctl-darwin-amd64",
        "c132fdddda68b9c7584ac19f3b40cd17f71916c2bca8182270ebe65b55198a12",
    ),
    ("Linux", "aarch64"): (
        "regctl-linux-arm64",
        "c4cf231e74cda685f1599f3d866b02b03c572e54b79ec8b062f32070b0ba4587",
    ),
    ("Linux", "x86_64"): (
        "regctl-linux-amd64",
        "c93aa7638749f5aaac1a8e01787321889c78f0101809bb2880343478d0ba0467",
    ),
}

SINGLE_MANIFEST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
RUNTIME_CONFIG_FIELDS = (
    "Cmd",
    "Entrypoint",
    "Env",
    "ExposedPorts",
    "Healthcheck",
    "Labels",
    "OnBuild",
    "Shell",
    "StopSignal",
    "Volumes",
    "WorkingDir",
)


class ToolchainGraftError(RuntimeError):
    """Raised when an image cannot safely accept the shared tool layers."""


@dataclass(frozen=True)
class GraftPlan:
    """Complete config and manifest payloads for one target image."""

    config: dict[str, Any]
    manifest: dict[str, Any]
    config_bytes: bytes
    manifest_bytes: bytes
    base_layer_count: int
    tool_layer_count: int

    @property
    def config_digest(self) -> str:
        return _digest(self.config_bytes)

    @property
    def manifest_digest(self) -> str:
        return _digest(self.manifest_bytes)


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _json_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _require_list(document: dict[str, Any], key: str, context: str) -> list[Any]:
    value = document.get(key)
    if not isinstance(value, list):
        raise ToolchainGraftError(f"{context} must contain a {key!r} list")
    return value


def _config_list(document: dict[str, Any], key: str, context: str) -> list[Any]:
    rootfs = document.get("rootfs")
    if not isinstance(rootfs, dict):
        raise ToolchainGraftError(f"{context} must contain a rootfs object")
    return _require_list(rootfs, key, f"{context}.rootfs")


def _descriptor_identity(descriptor: Any) -> tuple[Any, Any]:
    if not isinstance(descriptor, dict):
        raise ToolchainGraftError("image layer descriptors must be objects")
    return descriptor.get("digest"), descriptor.get("size")


def _assert_prefix(prefix: list[Any], values: list[Any], context: str) -> None:
    if len(values) < len(prefix) or values[: len(prefix)] != prefix:
        raise ToolchainGraftError(f"tool image does not have the declared {context} as a prefix")


def _assert_layer_prefix(prefix: list[Any], values: list[Any]) -> None:
    prefix_ids = [_descriptor_identity(item) for item in prefix]
    value_ids = [_descriptor_identity(item) for item in values[: len(prefix)]]
    if len(values) < len(prefix) or value_ids != prefix_ids:
        raise ToolchainGraftError(
            "tool image layers do not have the declared reference base as a prefix"
        )


def _validate_single_manifest(manifest: dict[str, Any], context: str) -> None:
    media_type = manifest.get("mediaType")
    if media_type not in SINGLE_MANIFEST_MEDIA_TYPES:
        raise ToolchainGraftError(
            f"{context} must resolve to one image manifest, got media type {media_type!r}"
        )
    _require_list(manifest, "layers", context)


def _validate_platform(configs: tuple[tuple[str, dict[str, Any]], ...]) -> None:
    platforms = {(config.get("os"), config.get("architecture")) for _, config in configs}
    if len(platforms) != 1:
        detail = ", ".join(
            f"{name}={config.get('os')}/{config.get('architecture')}"
            for name, config in configs
        )
        raise ToolchainGraftError(f"image platforms differ: {detail}")
    if platforms != {("linux", "amd64")}:
        raise ToolchainGraftError(f"only linux/amd64 SEC-bench images are supported, got {platforms}")


def _validate_tool_runtime_config(
    reference_config: dict[str, Any], tool_config: dict[str, Any]
) -> None:
    reference_runtime = reference_config.get("config")
    tool_runtime = tool_config.get("config")
    if not isinstance(reference_runtime, dict) or not isinstance(tool_runtime, dict):
        raise ToolchainGraftError("reference and tool image configs must contain config objects")
    changed = [
        field
        for field in RUNTIME_CONFIG_FIELDS
        if reference_runtime.get(field) != tool_runtime.get(field)
    ]
    if changed:
        fields = ", ".join(changed)
        raise ToolchainGraftError(
            f"tool Dockerfile changes base-specific runtime config fields: {fields}"
        )


def create_graft_plan(
    *,
    base_manifest: dict[str, Any],
    base_config: dict[str, Any],
    reference_base_manifest: dict[str, Any],
    reference_base_config: dict[str, Any],
    tool_manifest: dict[str, Any],
    tool_config: dict[str, Any],
) -> GraftPlan:
    """Append only tool filesystem layers while retaining the target base config."""

    for name, manifest in (
        ("target base manifest", base_manifest),
        ("reference base manifest", reference_base_manifest),
        ("tool manifest", tool_manifest),
    ):
        _validate_single_manifest(manifest, name)
    _validate_platform(
        (
            ("target base", base_config),
            ("reference base", reference_base_config),
            ("tool", tool_config),
        )
    )
    _validate_tool_runtime_config(reference_base_config, tool_config)

    base_layers = _require_list(base_manifest, "layers", "target base manifest")
    reference_layers = _require_list(
        reference_base_manifest, "layers", "reference base manifest"
    )
    tool_layers = _require_list(tool_manifest, "layers", "tool manifest")
    _assert_layer_prefix(reference_layers, tool_layers)

    base_diff_ids = _config_list(base_config, "diff_ids", "target base config")
    reference_diff_ids = _config_list(
        reference_base_config, "diff_ids", "reference base config"
    )
    tool_diff_ids = _config_list(tool_config, "diff_ids", "tool config")
    _assert_prefix(reference_diff_ids, tool_diff_ids, "reference rootfs")

    base_history = _require_list(base_config, "history", "target base config")
    reference_history = _require_list(
        reference_base_config, "history", "reference base config"
    )
    tool_history = _require_list(tool_config, "history", "tool config")
    _assert_prefix(reference_history, tool_history, "reference history")

    upper_layers = tool_layers[len(reference_layers) :]
    upper_diff_ids = tool_diff_ids[len(reference_diff_ids) :]
    upper_history = tool_history[len(reference_history) :]
    nonempty_history = sum(
        1 for item in upper_history if isinstance(item, dict) and not item.get("empty_layer", False)
    )
    if not upper_layers or len(upper_layers) != len(upper_diff_ids):
        raise ToolchainGraftError("tool image has an invalid or empty upper layer stack")
    if nonempty_history != len(upper_layers):
        raise ToolchainGraftError(
            "tool image history does not describe exactly one entry per upper filesystem layer"
        )

    result_config = copy.deepcopy(base_config)
    result_rootfs = result_config["rootfs"]
    result_rootfs["diff_ids"] = [*base_diff_ids, *upper_diff_ids]
    result_config["history"] = [*base_history, *upper_history]
    result_config["created"] = tool_config.get("created", base_config.get("created"))

    base_runtime = result_config.get("config")
    tool_runtime = tool_config.get("config")
    if not isinstance(base_runtime, dict) or not isinstance(tool_runtime, dict):
        raise ToolchainGraftError("target and tool image configs must contain config objects")
    base_runtime["User"] = tool_runtime.get("User", "root")

    if not base_layers:
        raise ToolchainGraftError("target base image has no filesystem layers")
    target_layer_media_type = base_layers[-1].get("mediaType")
    normalized_upper_layers = []
    for layer in upper_layers:
        normalized = copy.deepcopy(layer)
        normalized["mediaType"] = target_layer_media_type
        normalized_upper_layers.append(normalized)

    config_bytes = _json_bytes(result_config)
    result_manifest = copy.deepcopy(base_manifest)
    config_descriptor = copy.deepcopy(result_manifest.get("config"))
    if not isinstance(config_descriptor, dict):
        raise ToolchainGraftError("target base manifest must contain a config descriptor")
    config_descriptor["digest"] = _digest(config_bytes)
    config_descriptor["size"] = len(config_bytes)
    result_manifest["config"] = config_descriptor
    result_manifest["layers"] = [*base_layers, *normalized_upper_layers]
    manifest_bytes = _json_bytes(result_manifest)

    return GraftPlan(
        config=result_config,
        manifest=result_manifest,
        config_bytes=config_bytes,
        manifest_bytes=manifest_bytes,
        base_layer_count=len(base_layers),
        tool_layer_count=len(upper_layers),
    )


def image_repository(reference: str) -> str:
    """Return a registry repository with any digest or tag removed."""

    without_digest = reference.split("@", 1)[0]
    slash = without_digest.rfind("/")
    colon = without_digest.rfind(":")
    if colon > slash:
        return without_digest[:colon]
    return without_digest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_regctl(explicit_path: str | None = None) -> Path:
    """Locate regctl or download the pinned release to temporary storage."""

    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_file():
            raise ToolchainGraftError(f"regctl executable not found: {path}")
        return path

    installed = shutil.which("regctl")
    if installed:
        return Path(installed)

    asset = REGCTL_ASSETS.get((platform.system(), platform.machine()))
    if asset is None:
        raise ToolchainGraftError(
            f"no pinned regctl binary for {platform.system()}/{platform.machine()}"
        )
    asset_name, expected_sha256 = asset
    cache_root = Path(os.environ.get("ARISE_REGCTL_CACHE", tempfile.gettempdir()))
    cache_root.mkdir(parents=True, exist_ok=True)
    destination = cache_root / f"arise-{REGCTL_VERSION}-{asset_name}"
    if destination.is_file() and _file_sha256(destination) == expected_sha256:
        return destination

    url = (
        "https://github.com/regclient/regclient/releases/download/"
        f"{REGCTL_VERSION}/{asset_name}"
    )
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    logger.info("Downloading pinned regctl %s", REGCTL_VERSION)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            temporary.write_bytes(response.read())
    except (OSError, urllib.error.URLError) as error:
        temporary.unlink(missing_ok=True)
        raise ToolchainGraftError(f"failed to download regctl from {url}: {error}") from error
    if _file_sha256(temporary) != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise ToolchainGraftError("downloaded regctl binary failed SHA-256 verification")
    temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR)
    os.replace(temporary, destination)
    return destination


class RegctlClient:
    """Small checked subprocess adapter for regctl registry operations."""

    def __init__(self, executable: Path) -> None:
        self._executable = executable

    def run(
        self, arguments: list[str], *, input_data: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        completed = subprocess.run(  # noqa: S603 - executable is checksum-pinned or explicit
            [str(self._executable), *arguments],
            input=input_data,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ToolchainGraftError(f"regctl {' '.join(arguments[:2])} failed: {stderr}")
        return completed

    def read_manifest(self, reference: str) -> dict[str, Any]:
        completed = self.run(["manifest", "get", "--format", "raw-body", reference])
        manifest = _decode_json(completed.stdout, f"manifest for {reference}")
        if manifest.get("mediaType") not in INDEX_MEDIA_TYPES:
            return manifest
        completed = self.run(
            [
                "manifest",
                "get",
                "--platform",
                "linux/amd64",
                "--format",
                "raw-body",
                reference,
            ]
        )
        return _decode_json(completed.stdout, f"linux/amd64 manifest for {reference}")

    def read_config(
        self, reference: str, manifest: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resolved_manifest = manifest if manifest is not None else self.read_manifest(reference)
        descriptor = resolved_manifest.get("config")
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("digest"), str):
            raise ToolchainGraftError(f"manifest for {reference} has no config digest")
        completed = self.run(
            ["blob", "get", image_repository(reference), descriptor["digest"]]
        )
        return _decode_json(completed.stdout, f"config for {reference}")

    def manifest_exists(self, reference: str) -> bool:
        completed = self.run(["manifest", "head", reference], check=False)
        if completed.returncode == 0:
            return True
        stderr = completed.stderr.decode("utf-8", errors="replace").lower()
        if "not found" in stderr or "manifest unknown" in stderr:
            return False
        raise ToolchainGraftError(f"failed to check {reference}: {stderr.strip()}")


def _decode_json(payload: bytes, context: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ToolchainGraftError(f"invalid JSON in {context}: {error}") from error
    if not isinstance(value, dict):
        raise ToolchainGraftError(f"{context} must be a JSON object")
    return value


def _build_plan_from_registry(
    client: RegctlClient, *, base: str, reference_base: str, tool_image: str
) -> GraftPlan:
    base_manifest = client.read_manifest(base)
    reference_base_manifest = client.read_manifest(reference_base)
    tool_manifest = client.read_manifest(tool_image)
    return create_graft_plan(
        base_manifest=base_manifest,
        base_config=client.read_config(base, base_manifest),
        reference_base_manifest=reference_base_manifest,
        reference_base_config=client.read_config(reference_base, reference_base_manifest),
        tool_manifest=tool_manifest,
        tool_config=client.read_config(tool_image, tool_manifest),
    )


def publish_graft(
    client: RegctlClient,
    *,
    base: str,
    destination: str,
    reference_base: str,
    tool_image: str,
    dry_run: bool,
    force: bool,
) -> dict[str, Any]:
    """Publish one manifest-only SEC-bench tools image."""

    if image_repository(tool_image) != image_repository(destination):
        raise ToolchainGraftError(
            "tool image and destination must use the same repository so upper blobs are reused"
        )
    if client.manifest_exists(destination) and not force:
        return {"status": "skipped", "destination": destination}

    plan = _build_plan_from_registry(
        client, base=base, reference_base=reference_base, tool_image=tool_image
    )
    result = {
        "status": "validated" if dry_run else "published",
        "destination": destination,
        "base_layers": plan.base_layer_count,
        "shared_tool_layers": plan.tool_layer_count,
        "config_digest": plan.config_digest,
        "manifest_digest": plan.manifest_digest,
    }
    if dry_run:
        return result

    client.run(["image", "copy", "--fast", base, destination])
    client.run(
        [
            "blob",
            "put",
            "--digest",
            plan.config_digest,
            image_repository(destination),
        ],
        input_data=plan.config_bytes,
    )
    client.run(
        [
            "manifest",
            "put",
            "--content-type",
            str(plan.manifest["mediaType"]),
            destination,
        ],
        input_data=plan.manifest_bytes,
    )

    published_manifest = client.read_manifest(destination)
    published_config = client.read_config(destination, published_manifest)
    expected_layers = [item["digest"] for item in plan.manifest["layers"]]
    actual_layers = [item["digest"] for item in published_manifest.get("layers", [])]
    if actual_layers != expected_layers or published_config.get("rootfs") != plan.config.get("rootfs"):
        raise ToolchainGraftError(f"registry verification failed for {destination}")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish one SEC-bench image by reusing an immutable shared tool layer stack."
    )
    parser.add_argument("--base", help="Upstream hwiwonlee SEC-bench base image")
    parser.add_argument("--destination", help="Destination secb-tools image")
    parser.add_argument("--tool-image", help="Shared tool image in the destination repository")
    parser.add_argument("--reference-base", help="Immutable base used to build --tool-image")
    parser.add_argument("--regctl", help="Path to regctl; a pinned binary is downloaded if omitted")
    parser.add_argument("--dry-run", action="store_true", help="Validate without registry writes")
    parser.add_argument("--force", action="store_true", help="Replace an existing destination tag")
    parser.add_argument(
        "--print-regctl-path",
        action="store_true",
        help="Resolve the pinned regctl executable and exit",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        executable = resolve_regctl(args.regctl)
        if args.print_regctl_path:
            print(executable)
            return 0
        required = {
            "--base": args.base,
            "--destination": args.destination,
            "--tool-image": args.tool_image,
            "--reference-base": args.reference_base,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ToolchainGraftError(f"missing required arguments: {', '.join(missing)}")
        result = publish_graft(
            RegctlClient(executable),
            base=args.base,
            destination=args.destination,
            reference_base=args.reference_base,
            tool_image=args.tool_image,
            dry_run=args.dry_run,
            force=args.force,
        )
    except ToolchainGraftError as error:
        logger.error("%s", error)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
