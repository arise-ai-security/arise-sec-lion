# syntax=docker/dockerfile:1.10@sha256:865e5dd094beca432e8c0a1d5e1c465db5f998dca4e439981029b3b81fb39ed5

# CVE-independent N1 tool payload. The final image is scratch-based and contains
# only Arise-owned files under /opt, so it cannot leak a reference CVE workspace.
ARG NODE_IMAGE=node:20.20.2-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0
ARG PYTHON_IMAGE=python:3.12.5-slim-bookworm@sha256:c24c34b502635f1f7c4e99dc09a2cbd85d480b7dcfd077198c6b5af138906390

FROM ${NODE_IMAGE} AS node_builder
ARG CLAUDE_CODE_VERSION=2.1.173
RUN mkdir -p /opt/arise-node/bin \
    && cp /usr/local/bin/node /opt/arise-node/bin/node \
    && npm install --global --prefix /opt/arise-node \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && /opt/arise-node/bin/node --version \
    && PATH="/opt/arise-node/bin:${PATH}" claude --version

FROM ${PYTHON_IMAGE} AS mcp_builder
ARG MCP_VERSION=1.27.2
RUN python3.12 -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.12 \
    --abi cp312 \
    --target /opt/arise-mcp/site-packages \
    "mcp==${MCP_VERSION}"
COPY --chmod=755 deployment/secbench-mcp-python \
    /opt/arise-mcp/venv/bin/python
COPY plugins/security/mcp/__init__.py \
    /opt/arise-mcp/plugins/security/mcp/__init__.py
COPY plugins/security/mcp/security_tools_server.py \
    /opt/arise-mcp/plugins/security/mcp/security_tools_server.py
RUN touch /opt/arise-mcp/plugins/__init__.py \
    /opt/arise-mcp/plugins/security/__init__.py \
    && /opt/arise-mcp/venv/bin/python -c \
        'from mcp.server.fastmcp import FastMCP; print("MCP payload OK")'

FROM scratch
LABEL org.opencontainers.image.title="Arise SEC-bench N1 shared tools payload"
LABEL io.arise.secbench.payload.abi="ubuntu-20.04-amd64-python-3.12"
COPY --link --from=node_builder /opt/arise-node /opt/arise-node
COPY --link --from=mcp_builder /opt/arise-mcp /opt/arise-mcp
