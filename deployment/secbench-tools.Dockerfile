# SEC-bench Tools Layer
#
# Extends a SEC-bench base image with security analysis tools (Valgrind, KLEE).
# Workers use these tools during vulnerability analysis.
#
# Usage:
#   docker build -f deployment/secbench-tools.Dockerfile \
#     --build-arg BASE_IMAGE=hwiwonlee/secb.eval.x86_64.<project>.<cve> \
#     -t secb-tools:<project>.<cve> .
#
# Adding a new tool:
#   1. Add install commands below
#   2. Add registry entry in plugins/security/security_tool.py
#   3. Optionally add prompt guidance in prompts/domains/secbench/tools.j2

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root

# Core analysis tools — pre-installed so workers skip apt-get at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    valgrind \
    gdb \
    cppcheck \
    strace \
    ltrace \
    cflow \
    jq \
    && (apt-get install -y --no-install-recommends libasan5 libubsan1 2>/dev/null \
        || apt-get install -y --no-install-recommends libasan6 libubsan1 2>/dev/null \
        || echo "ASan runtime not available via apt — builder will handle") \
    && rm -rf /var/lib/apt/lists/*

# KLEE — symbolic execution engine (optional, not packaged in all base images)
# Requires LLVM/Clang for bitcode compilation + KLEE runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    klee \
    klee-runtime \
    llvm \
    clang \
    && rm -rf /var/lib/apt/lists/* \
    || { echo "KLEE not available in base image repos — skipping"; rm -rf /var/lib/apt/lists/*; }

# Verify installations
RUN valgrind --version && { klee --version 2>/dev/null || echo "KLEE not installed"; }

# Claude Code CLI — required so the agent process itself runs inside this
# container (Cell A flat-mode) instead of on the host. Mirrors the install
# recipe in deployment/Dockerfile so both images use the same Node/npm path.
ARG CLAUDE_CODE_VERSION=latest
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && node --version && claude --version

# Security-tools MCP server — Cell A's claude-in-container needs the same
# valgrind_run/klee_run MCP tools that B/C get on the host. Install Python +
# the mcp package, then bake the server module under /opt/arise-mcp/. The
# server detects in-container mode (no ARISE_SECBENCH_HELPER_SCRIPT) and
# shells commands directly via bash -lc instead of docker exec.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    && python3 -m venv /opt/arise-mcp/venv \
    && /opt/arise-mcp/venv/bin/pip install --no-cache-dir "mcp[server]>=1.9.0" \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && /opt/arise-mcp/venv/bin/python -c "from mcp.server.fastmcp import FastMCP"
COPY plugins/security/mcp/__init__.py /opt/arise-mcp/plugins/security/mcp/__init__.py
COPY plugins/security/mcp/security_tools_server.py /opt/arise-mcp/plugins/security/mcp/security_tools_server.py
RUN touch /opt/arise-mcp/plugins/__init__.py /opt/arise-mcp/plugins/security/__init__.py
