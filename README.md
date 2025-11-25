# Xahaud Scripts

Scripts for working in xahaud repo

## Installation

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
uv sync --dev
```

### Global Install

To install commands globally (available everywhere):

```bash
uv tool install --force --editable .
```

## Available Commands

- `run-tests` - Run xahaud tests with coverage support
- `testnet` - Launch and manage local test networks
- `get-job` - Fetch GitHub Actions job details
- `build-jshooks-header` - Build JS hooks header file
- `format-changed` - Format changed files in git

## Development

```bash
uv run ruff check    # lint
uv run ruff format   # format
uv run mypy          # type check
uv run pytest        # test
```

## License

MIT
