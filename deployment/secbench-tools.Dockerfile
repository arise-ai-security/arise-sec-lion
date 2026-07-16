# syntax=docker/dockerfile:1.10@sha256:865e5dd094beca432e8c0a1d5e1c465db5f998dca4e439981029b3b81fb39ed5

# Thin N1 image assembly. Every CVE retains its own upstream filesystem and
# runtime metadata; only the CVE-independent shared tool payload is linked in.
ARG BASE_IMAGE=hwiwonlee/secb.eval.x86_64.openjpeg.cve-2024-56827@sha256:d17f6788f2915d31b21c8af1b3fa9b68ee71447247a1f97d4cec1217a06b0c5a
ARG SHARED_TOOLS_IMAGE=cheshire0814/secb-tools:payload-focal-amd64-v2
ARG SHARED_TOOLS_GENERATION=payload-focal-amd64-v2

FROM ${SHARED_TOOLS_IMAGE} AS shared_tools

# Repository-owned MCP source remains local. It is added as one tiny reusable
# layer during each thin CVE assembly and is never published in the payload.
FROM scratch AS local_mcp_server
COPY --chmod=755 deployment/secbench-mcp-python \
    /opt/arise-mcp/venv/bin/python
COPY plugins/security/mcp/__init__.py \
    /opt/arise-mcp/plugins/security/mcp/__init__.py
COPY plugins/security/mcp/security_tools_server.py \
    /opt/arise-mcp/plugins/security/mcp/security_tools_server.py

FROM ${BASE_IMAGE}

ARG SHARED_TOOLS_GENERATION
USER root
COPY --link --from=shared_tools / /
COPY --link --from=local_mcp_server /opt/arise-mcp /opt/arise-mcp
ENV PATH="/opt/arise-node/bin:${PATH}"
LABEL io.arise.secbench.tools="${SHARED_TOOLS_GENERATION}"
