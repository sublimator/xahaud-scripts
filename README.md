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
- `x-build-test-hooks` - Extract and compile WASM test hooks from C++ source
- `x-format-changed` - Format changed files in git (C++, Python, shell, CMake)
- `x-quick-check` / `xr-quick-check` - Run compiler syntax checks for dirty C/C++ translation units
- `x-inspect-net` - Inspect live amendment status, overlay versions, and stale builds
- `x-binary-features` - Inspect amendment support encoded in xahaud git refs/tags
- `xr-build` - Build xrpld with coverage, patches, and cmake presets
- `xr-coverage-diff` - Show uncovered lines from existing coverage data

### Hook fixtures

`x-run-tests --compile-hooks FILE` and `x-build-test-hooks FILE` delegate to
`hookz build-test-hooks`. hookz owns fixture extraction and C/WAT compilation;
`jshookz compile-hook` owns JavaScript/TypeScript compilation. Install both
commands on `PATH` when a test contains `[test.tshook]`, `[test.jshook]`, or a
`.ts`/`.js` file reference. `JSHOOKZ_HOOK_COMPILER` may select another jshookz
command; `QJS_HOOK_COMPILER` remains a compatibility fallback.

### FITH beta preflight

Pass `--fith` to make `x-run-tests` run `cppt beta fith` before the ordinary
build. By default FITH selects the object fan-out for modified and untracked
files (`--fith-base HEAD`), then the normal target build/link still runs before
tests. Use `--fith-base origin/dev` deliberately for the full branch delta.
`XAHAU_SCRIPTS_FITH_BETA=1` remains a compatibility opt-in and `--no-fith`
overrides it for one run.

## Saved Test Binaries

Use `@name` aliases to keep local mixed-binary testnets readable:

```bash
x-run-tests --times=0 --save-binary @rng-ce
x-testnet --rippled-path @rng-ce run
x-testnet run --node-binary n0:@old --node-binary n1:@new
```

Saved binaries are copied under `~/.cache/xahaud-scripts/binaries/`; metadata is
written to `~/.config/xahaud-scripts/binaries.json`.

## Development

```bash
uv run ruff check    # lint
uv run ruff format   # format
uv run mypy          # type check
uv run pytest        # test
```

## License

MIT
