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

- `x-run-tests` - Build and run xahaud tests (conan, ccache, coverage, lldb)
- `x-testnet` - Launch and manage local test networks
- `x-get-job` - Fetch GitHub Actions job details and logs
- `x-build-jshooks-header` - Build JS hooks header file
- `x-build-test-hooks` - Extract and compile WASM test hooks from C++ source
- `x-format-changed` - Format changed files in git (C++, Python, shell, CMake)
- `xr-build` - Build xrpld with coverage, patches, and cmake presets
- `xr-coverage-diff` - Show uncovered lines from existing coverage data

## Development

```bash
uv run ruff check    # lint
uv run ruff format   # format
uv run mypy          # type check
uv run pytest        # test
```

## License

MIT
