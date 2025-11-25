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

- `x-run-tests` - Run xahaud tests with coverage support
- `x-testnet` - Launch and manage local test networks
- `x-get-job` - Fetch GitHub Actions job details
- `x-build-jshooks-header` - Build JS hooks header file
- `x-format-changed` - Format changed files in git

## Development

```bash
uv run ruff check    # lint
uv run ruff format   # format
uv run mypy          # type check
uv run pytest        # test
```

## License

MIT
