"""Tests for manifest-only SEC-bench tool layer publication."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from plugins.security.toolchain_graft import (
    RegctlClient,
    ToolchainGraftError,
    create_graft_plan,
    image_repository,
)


DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
DOCKER_LAYER = "application/vnd.docker.image.rootfs.diff.tar.gzip"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"


def _layer(name: str, media_type: str = DOCKER_LAYER) -> dict[str, object]:
    return {"mediaType": media_type, "digest": f"sha256:{name}", "size": len(name)}


def _manifest(layers: list[dict[str, object]], *, oci: bool = False) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST if oci else DOCKER_MANIFEST,
        "config": {
            "mediaType": OCI_CONFIG if oci else DOCKER_CONFIG,
            "digest": "sha256:old-config",
            "size": 1,
        },
        "layers": layers,
    }


def _config(
    diff_ids: list[str],
    history: list[dict[str, object]],
    *,
    workdir: str,
    env: list[str],
    user: str = "",
) -> dict[str, object]:
    return {
        "architecture": "amd64",
        "os": "linux",
        "created": "2026-01-01T00:00:00Z",
        "config": {
            "Cmd": ["/bin/bash"],
            "Env": env,
            "Labels": {"project": workdir.rsplit("/", 1)[-1]},
            "User": user,
            "WorkingDir": workdir,
        },
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": history,
    }


def _documents() -> dict[str, dict[str, object]]:
    reference_history = [{"created_by": "reference-1"}, {"created_by": "reference-2"}]
    tool_history = [
        *reference_history,
        {"created_by": "USER root", "empty_layer": True},
        {"created_by": "install tools"},
        {"created_by": "copy mcp"},
    ]
    return {
        "base_manifest": _manifest([_layer("target-base")]),
        "base_config": _config(
            ["sha256:target-diff"],
            [{"created_by": "target"}],
            workdir="/src/mruby",
            env=["PROJECT=mruby", "CFLAGS=-O1"],
        ),
        "reference_base_manifest": _manifest([_layer("ref-1"), _layer("ref-2")]),
        "reference_base_config": _config(
            ["sha256:ref-diff-1", "sha256:ref-diff-2"],
            reference_history,
            workdir="/src/openjpeg",
            env=["PROJECT=openjpeg", "CFLAGS=-w"],
        ),
        "tool_manifest": _manifest(
            [
                _layer("ref-1", OCI_LAYER),
                _layer("ref-2", OCI_LAYER),
                _layer("tool-1", OCI_LAYER),
                _layer("tool-2", OCI_LAYER),
            ],
            oci=True,
        ),
        "tool_config": _config(
            [
                "sha256:ref-diff-1",
                "sha256:ref-diff-2",
                "sha256:tool-diff-1",
                "sha256:tool-diff-2",
            ],
            tool_history,
            workdir="/src/openjpeg",
            env=["PROJECT=openjpeg", "CFLAGS=-w"],
            user="root",
        ),
    }


def test_graft_retains_target_runtime_config_and_reuses_tool_layers() -> None:
    # Given: a tool image built on openjpeg and a target-specific mruby base.
    documents = _documents()

    # When: the shared upper layers are grafted onto the target.
    plan = create_graft_plan(**documents)

    # Then: target-specific runtime metadata survives while tool files are appended.
    runtime = plan.config["config"]
    assert runtime["WorkingDir"] == "/src/mruby"
    assert runtime["Env"] == ["PROJECT=mruby", "CFLAGS=-O1"]
    assert runtime["Labels"] == {"project": "mruby"}
    assert runtime["User"] == "root"
    assert [layer["digest"] for layer in plan.manifest["layers"]] == [
        "sha256:target-base",
        "sha256:tool-1",
        "sha256:tool-2",
    ]
    assert all(layer["mediaType"] == DOCKER_LAYER for layer in plan.manifest["layers"])
    assert plan.config["rootfs"]["diff_ids"] == [
        "sha256:target-diff",
        "sha256:tool-diff-1",
        "sha256:tool-diff-2",
    ]
    expected_config_digest = f"sha256:{hashlib.sha256(plan.config_bytes).hexdigest()}"
    assert plan.manifest["config"]["digest"] == expected_config_digest
    assert plan.manifest["config"]["size"] == len(plan.config_bytes)
    assert plan.base_layer_count == 1
    assert plan.tool_layer_count == 2


def test_graft_rejects_wrong_reference_base() -> None:
    # Given: a declared reference whose layer is not the tool image's prefix.
    documents = _documents()
    documents["reference_base_manifest"]["layers"][0]["digest"] = "sha256:wrong"

    # When / Then: grafting fails before any registry mutation.
    with pytest.raises(ToolchainGraftError, match="reference base as a prefix"):
        create_graft_plan(**documents)


def test_graft_rejects_tool_image_that_changes_base_specific_environment() -> None:
    # Given: a tool Dockerfile that changed the reference base environment.
    documents = _documents()
    documents["tool_config"]["config"]["Env"] = ["PROJECT=wrong"]

    # When / Then: the unsafe tool image cannot be applied to other CVEs.
    with pytest.raises(ToolchainGraftError, match="runtime config fields: Env"):
        create_graft_plan(**documents)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("cheshire0814/secb-tools:tag", "cheshire0814/secb-tools"),
        ("cheshire0814/secb-tools@sha256:abc", "cheshire0814/secb-tools"),
        ("localhost:5000/ns/secb-tools:tag", "localhost:5000/ns/secb-tools"),
    ],
)
def test_image_repository_removes_only_tag_or_digest(reference: str, expected: str) -> None:
    assert image_repository(reference) == expected


def test_registry_client_resolves_amd64_index_and_reads_its_config(monkeypatch) -> None:
    # Given: Docker published the tool tag as an OCI index with an amd64 child.
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [],
    }
    child = _manifest([_layer("tool", OCI_LAYER)], oci=True)
    child["config"]["digest"] = "sha256:config"
    config = _config(
        ["sha256:diff"],
        [{"created_by": "tool"}],
        workdir="/src/openjpeg",
        env=["PROJECT=openjpeg"],
    )
    outputs = [index, child, config]
    calls: list[list[str]] = []

    def fake_run(arguments, *, input_data=None, check=True):
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=json.dumps(outputs.pop(0)).encode(),
            stderr=b"",
        )

    client = RegctlClient(Path("/unused/regctl"))
    monkeypatch.setattr(client, "run", fake_run)

    # When: the tag and its config are read.
    manifest = client.read_manifest("registry.example/secb-tools:toolchain")
    actual_config = client.read_config(
        "registry.example/secb-tools:toolchain", manifest
    )

    # Then: registry operations select amd64 and fetch that child's config blob.
    assert manifest == child
    assert actual_config == config
    assert calls[1][0:4] == ["manifest", "get", "--platform", "linux/amd64"]
    assert calls[2] == [
        "blob",
        "get",
        "registry.example/secb-tools",
        "sha256:config",
    ]
