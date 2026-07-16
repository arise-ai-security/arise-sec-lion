# syntax=docker/dockerfile:1.10@sha256:865e5dd094beca432e8c0a1d5e1c465db5f998dca4e439981029b3b81fb39ed5

# CVE-independent N1 tool payload. The final image is scratch-based and contains
# only third-party runtime artifacts, never repository-owned source or a
# reference CVE workspace.
ARG NODE_IMAGE=node:20.20.2-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0
ARG PYTHON_RUNTIME_IMAGE=hwiwonlee/secb.eval.x86_64.openjpeg.cve-2024-56827@sha256:d17f6788f2915d31b21c8af1b3fa9b68ee71447247a1f97d4cec1217a06b0c5a
ARG NATIVE_TOOLS_IMAGE=ubuntu:20.04@sha256:8feb4d8ca5354def3d8fce243717141ce31e2c428701f6682bd2fafe15388214

FROM ${NODE_IMAGE} AS node_builder
ARG CLAUDE_CODE_VERSION=2.1.173
RUN mkdir -p /opt/arise-node/bin \
    && cp /usr/local/bin/node /opt/arise-node/bin/node \
    && npm install --global --prefix /opt/arise-node \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && /opt/arise-node/bin/node --version \
    && PATH="/opt/arise-node/bin:${PATH}" claude --version

FROM ${PYTHON_RUNTIME_IMAGE} AS mcp_builder
ARG MCP_VERSION=1.27.2
RUN mkdir -p /opt/arise-python/bin /opt/arise-python/lib \
    && cp /usr/local/bin/python3.12 /opt/arise-python/bin/python3.12 \
    && cp -a /usr/local/lib/python3.12 /opt/arise-python/lib/ \
    && rm -rf \
        /opt/arise-python/lib/python3.12/site-packages \
        /opt/arise-python/lib/python3.12/test \
        /opt/arise-python/lib/python3.12/idlelib \
        /opt/arise-python/lib/python3.12/tkinter \
        /opt/arise-python/lib/python3.12/turtledemo \
    && /usr/local/bin/python3.12 -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.12 \
    --abi cp312 \
    --target /opt/arise-mcp/site-packages \
    "mcp==${MCP_VERSION}" \
    && PYTHONHOME=/opt/arise-python \
        PYTHONPATH=/opt/arise-mcp/site-packages \
        /opt/arise-python/bin/python3.12 -c \
        'from mcp.server.fastmcp import FastMCP; print("MCP payload OK")'

FROM ${NATIVE_TOOLS_IMAGE} AS native_builder
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --download-only -y --no-install-recommends \
        valgrind \
        gdb \
        cppcheck \
        strace \
        ltrace \
        cflow \
        jq \
        libasan5 \
        libubsan1 \
    && mkdir -p /toolroot \
    && for package in /var/cache/apt/archives/*.deb; do \
        dpkg-deb --extract "$package" /toolroot; \
    done \
    && for directory in bin sbin lib lib64; do \
        if [ -d "/toolroot/$directory" ]; then \
            mkdir -p "/toolroot/usr/$directory"; \
            cp -a "/toolroot/$directory/." "/toolroot/usr/$directory/"; \
            rm -rf "/toolroot/$directory"; \
        fi; \
    done \
    && rm -rf \
        /toolroot/usr/share/doc \
        /toolroot/usr/share/info \
        /toolroot/usr/share/lintian \
        /toolroot/usr/share/locale \
        /toolroot/usr/share/man

FROM scratch AS payload_root
COPY --from=native_builder /toolroot/ /payload-root/
COPY --from=node_builder /opt/arise-node /payload-root/opt/arise-node
COPY --from=mcp_builder /opt/arise-python /payload-root/opt/arise-python
COPY --from=mcp_builder /opt/arise-mcp/site-packages \
    /payload-root/opt/arise-mcp/site-packages

FROM scratch
LABEL org.opencontainers.image.title="Arise SEC-bench N1 shared tools payload"
LABEL io.arise.secbench.payload.abi="ubuntu-20.04-amd64-python-3.12"
COPY --link --from=payload_root /payload-root/ /
