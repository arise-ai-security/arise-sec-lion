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

# Valgrind — dynamic analysis (memory errors, stack traces)
# ~50MB, works on compiled binaries directly
RUN apt-get update && apt-get install -y --no-install-recommends \
    valgrind \
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

# Node.js + Claude Code CLI
# Required by the flat-CLI baseline arm (experiments/baselines/flat_cli_harness.py)
# which invokes ``claude --print --output-format stream-json`` inside the container.
# Uses NodeSource's current LTS (Node 20) for the @anthropic-ai/claude-code package.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && node --version && claude --version

# Verify installations
RUN valgrind --version && { klee --version 2>/dev/null || echo "KLEE not installed"; }
