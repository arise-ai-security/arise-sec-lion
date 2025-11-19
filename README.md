# arise-sec-lion

A Python project managed with `uv`.

## Prerequisites

- Python 3.11 or higher
- [uv](https://github.com/astral-sh/uv) - Install with: `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Setup

1. Clone the repository:
```bash
git clone <repository-url>
cd arise-sec-lion
```

2. Create a virtual environment and install dependencies:
```bash
uv sync
```

This will create a `.venv` directory and install all dependencies from `pyproject.toml`.

## Running

Activate the virtual environment:
```bash
source .venv/bin/activate  # On macOS/Linux
# or
.venv\Scripts\activate  # On Windows
```

Run the main script:
```bash
python main.py
```

## Development

### Adding Dependencies

Add a new package:
```bash
uv add <package-name>
```

Add a development dependency:
```bash
uv add --dev <package-name>
```

### Updating Dependencies

```bash
uv sync
```

### Running Tests

**Unit Tests** (no database required):
```bash
uv run pytest tests/core tests/infrastructure/test_litellm_adapter.py -v
```

**Integration Tests** (requires PostgreSQL):
```bash
# Start test database
docker compose -f docker-compose.test.yml up -d

# Run integration tests
TEST_DB_URL="postgresql://testuser:testpass@localhost:5433/testdb" \
    uv run pytest tests/infrastructure/test_event_store.py -v

# Stop database
docker compose -f docker-compose.test.yml down
```

**All Tests**:
```bash
# Start test database
docker compose -f docker-compose.test.yml up -d

# Run all tests
uv run pytest tests/core tests/infrastructure/test_litellm_adapter.py -v
TEST_DB_URL="postgresql://testuser:testpass@localhost:5433/testdb" \
    uv run pytest tests/infrastructure/test_event_store.py -v

# Stop database
docker compose -f docker-compose.test.yml down
```

## Project Structure

```
.
├── main.py           # Main application entry point
├── pyproject.toml    # Project configuration and dependencies
└── README.md         # This file
```
