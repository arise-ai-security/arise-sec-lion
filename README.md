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

## Project Structure

```
.
├── main.py           # Main application entry point
├── pyproject.toml    # Project configuration and dependencies
└── README.md         # This file
```