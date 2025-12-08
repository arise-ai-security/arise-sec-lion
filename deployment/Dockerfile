# =============================================================================
# Multi-stage Dockerfile for Arise Multi-Agent System
# =============================================================================
# This Dockerfile supports both worker tools:
# - Claude Code CLI (default): Requires ANTHROPIC_API_KEY at runtime
# - OpenHands SDK: Requires OPENAI_API_KEY (or other LLM provider key)
#
# The composite worker adapter routes to the correct tool based on agent config.
# Parent agents specify which tool their children should use via config.tool.

# -----------------------------------------------------------------------------
# Stage 1: Builder - Install Python dependencies
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (better layer caching)
COPY pyproject.toml uv.lock ./

# Create virtual environment and install dependencies
RUN uv sync --frozen --no-dev --no-install-project

# -----------------------------------------------------------------------------
# Stage 2: Development - With dev dependencies and hot reload
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS development

WORKDIR /app

# Install system dependencies including Node.js for Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install ALL dependencies (including dev)
RUN uv sync --frozen --no-install-project

# Copy application code
COPY . .

# Install the project itself
RUN uv sync --frozen

# Set PATH to use virtual environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Default command for development
CMD ["python", "main.py"]

# -----------------------------------------------------------------------------
# Stage 3: Production - Minimal image
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS production

WORKDIR /app

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY --chown=appuser:appuser . .

# Set PATH and environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER appuser

CMD ["python", "main.py"]
