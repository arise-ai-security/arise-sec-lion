# Arise Sec Lion
# Two-stage build: development (default) and production

# --- Development (default) ---
FROM python:3.12-slim AS development

WORKDIR /app

# System deps + Node.js for Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Copy app and install
COPY . .
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

CMD ["python", "main.py"]

# --- Production ---
FROM python:3.12-slim AS production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home appuser

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=appuser:appuser . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER appuser

CMD ["python", "main.py"]
