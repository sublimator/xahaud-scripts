# Xahaud Scripts

Developer tooling for xahaud and xrpld worktrees. Python 3.13+, Click CLIs, Rich
output. Install once, then run the commands from the checkout you mean to
affect.

## Installation

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
uv sync --dev
uv tool install --force --editable .
```

The second command puts every console script on `PATH`. Most commands expect to
be run from inside a xahaud (or xrpld) git checkout.

## Which command

| If you want to… | Use |
| --- | --- |
| Build and run tests in a **xahaud** tree | `x-run-tests` (not `xr-build`) |
| Build and run tests in an **xrpld** tree | `xr-build` (refuses a xahaud checkout) |
| Follow a live `x-run-tests` build log | `x-run-tests-tail` |
| Coverage from artifacts you already have | `x-coverage-diff` / `x-coverage-report` (xahaud) or `xr-coverage-diff` (xrpld) |
| Patch coverage on a public GitHub PR | `x-codecov` |
| Local multi-node network / scenario suite | `x-testnet` |
| Live mainnet/testnet/XRPL without a local node | `x-inspect-net`, `x-binary-features` |
| Syntax-check dirty C++ without linking | `x-quick-check` |
| Format dirty files, compile hook fixtures, CI logs, timings | `x-format-changed`, `x-build-test-hooks`, `x-get-job`, `x-run-stats` |

## Command catalogue

Every entry in `[project.scripts]` (`pyproject.toml`). `xr-quick-check` is the
same program as `x-quick-check`.

| Command | Workflow | What it does | Typical invocation |
| --- | --- | --- | --- |
| `x-run-tests` | Build & test (xahaud) | Always builds, then runs the trailing unittest filter. Conan/ccache/coverage/lldb are opt-in; `--no-build` does not exist. `--fith` is a heuristic quick-link, not an ordinary binary; `--save-binary @name` needs `--no-fith`. | `x-run-tests -- ripple.app.Import` |
| `x-run-tests-tail` | Build & test (xahaud) | Follows this worktree's tee log under `~/.config/xahaud-scripts/outputs/`. Waits for the file; run it from the xahaud checkout. | `x-run-tests-tail --build-type debug` |
| `xr-build` | Build & test (xrpld) | Builds xrpld with optional coverage, sanitizers, and bundled cmake patches. Refuses to run inside a xahaud tree (hooks trees) so it cannot pollute that checkout. | `xr-build --coverage --test ripple.app.Import` |
| `x-coverage-diff` | Coverage (xahaud) | Uncovered lines in the git diff from existing `.gcda` / `.profraw` — no rebuild. Default `--since origin/dev`. Choose `--coverage-impl gcov` or `llvm-injected`. | `x-coverage-diff --since origin/dev` |
| `x-coverage-report` | Coverage (xahaud) | Full JSON/HTML (gcov) or json/lcov/summary (llvm) from artifacts already on disk. | `x-coverage-report --coverage-impl gcov` |
| `xr-coverage-diff` | Coverage (xrpld) | Uncovered diff lines from a previous `xr-build --coverage` `coverage.json`. Default `--since origin/develop`. | `xr-coverage-diff --since origin/develop` |
| `x-codecov` | Coverage (PR) | Public Codecov API only (no auth): PR summary, uncovered added lines, gap to the patch target, and ranked clusters. | `x-codecov pull 1234` |
| `x-testnet` | Local network | Generate, launch, and inspect a distinct-IP local xahaud net; `suite` is the scenario runner. `run` does not run scenarios. macOS needs `setup-aliases` first. | `x-testnet suite .testnet/scenarios/suite.yml` |
| `x-inspect-net` | Live networks | Amendment tables, overlay version crawl, and zombie/stale-build check against a local xahaud source tree. XRPL has no anonymous amendment table, so it is crawl-only. | `x-inspect-net amendments` |
| `x-binary-features` | Live networks | Reads amendment declarations from git refs/tags in a xahaud checkout (what that source knew how to register/vote). Pair with `x-inspect-net zombies`. | `x-binary-features --observed-xahau` |
| `x-build-test-hooks` | Hooks | Thin shim over `hookz build-test-hooks`. Default input is `SetHook_test.cpp` when you are in a xahaud tree. JS/TS fixtures need `jshookz` as well. | `x-build-test-hooks -j 4` |
| `x-format-changed` | Maintenance | Formats dirty C++, Python, shell, and CMake (clang-format 18, ruff, shfmt, cmake-format). `--since` for a branch delta; `--stage` git-adds the result. | `x-format-changed --since origin/dev` |
| `x-quick-check` | Maintenance | Syntax-checks dirty C/C++ TUs via `compile_commands.json`. No link, no tests. `--tu` adds a translation unit for header-only edits. | `x-quick-check --dry-run` |
| `xr-quick-check` | Maintenance | Alias of `x-quick-check` (same entry point). | `xr-quick-check --since origin/dev` |
| `x-get-job` | CI | Fetches GitHub Actions job steps and logs. Public repos work without a token; `"<clip>"` reads the URL from the clipboard. | `x-get-job "<clip>"` |
| `x-run-stats` | CI | Build/test timings from the local runs database (`~/.xahaud-scripts/runs.db`). Telemetry, not correctness evidence. | `x-run-stats -d 7` |

### `x-run-tests` and FITH

```bash
x-run-tests -- ripple.app.Import
x-run-tests --times=0
x-run-tests --fith -- unit_test_hook
x-run-tests --no-fith --times=0 --save-binary @rng-ce
x-run-tests --coverage --diff-cover -- unit_test_hook
x-run-tests-tail
```

Always builds first: an incremental no-op is cheap, and a stale binary used to
present greens for code that never ran. Builds take `build*/.x-build-lock` and
recompact ninja after success — do not run raw `ninja` / `cmake -B` against a
shared build dir.

`--fith` calls `cppt beta fith`: compile the current diff's slice and quick-link
the target. Default base is the dirty worktree (`--fith-base HEAD`); use
`--fith-base origin/dev` for the full branch. Incomplete graph evidence warns;
`--fith-strict` refuses. `XAHAU_SCRIPTS_FITH_BETA=1` is a legacy opt-in;
`--no-fith` overrides it. FITH binaries cannot be `--save-binary`'d.

### `xr-build`

```bash
xr-build --coverage --test ripple.app.Import
xr-build --ccache --release
xr-build --skip-test
```

Use this only in an xrpld checkout. `--patches` applies xr-build's bundled
patches, not `patches/disconnect-rpc-ip-only.patch`.

### `x-testnet`

```bash
x-testnet setup-aliases -n 7
x-testnet suite .testnet/scenarios/suite.yml
x-testnet generate --node-count 5 && x-testnet run
x-testnet --rippled-path @rng-ce run
x-testnet check 56B241D7A43D40354D02A9DC4C8DF5C7A1F930D92A9035C4E12291B3CA3E1C2B
```

`suite` is the scenario entry point (`--scenario-script` is gone). Each suite
test gets a fresh net; teardown keeps node dirs so `debug.log` survives.
`run` launches and monitors only. `check` requires an amendment hash.

Peer addressing is distinct-IP everywhere (`127.0.0.<id+1>`). Reusing
`127.0.0.1` collapses the mesh. On macOS, `setup-aliases` must run first; a
missing alias fails late as `actual=[]` in a topology diff.

`x-testnet` finds the repo with `git rev-parse --show-toplevel` from CWD and
does **not** honor `XAHAUD_ROOT`.

## Hook fixtures

`x-run-tests --compile-hooks FILE` and `x-build-test-hooks` delegate to
`hookz build-test-hooks`. hookz owns C/WAT extraction and compilation;
`jshookz compile-hook` owns JavaScript/TypeScript. Install both on `PATH` when
a test contains `[test.tshook]`, `[test.jshook]`, or a `.ts`/`.js` reference.
`JSHOOKZ_HOOK_COMPILER` may select another jshookz command; `QJS_HOOK_COMPILER`
remains a compatibility fallback.

## Saved test binaries

`@name` is always a registry alias. Non-`@` paths keep their old meaning.

```bash
x-run-tests --no-fith --times=0 --save-binary @rng-ce
x-testnet --rippled-path @rng-ce run
x-testnet run --node-binary n0:@old --node-binary n1:@new
```

Copies land in `~/.cache/xahaud-scripts/binaries/`; metadata in
`~/.config/xahaud-scripts/binaries.json` (best-effort evidence, not a package
manager).

## Optional xahaud disconnect RPC patch

`patches/disconnect-rpc-ip-only.patch` is a manual, xahaud testnet-only patch.
It was derived from xahaud commit `6fc14f398d754283b5dee6576edb59dc2656eaaa`
with port parsing and matching removed, and targets the compatible source shape
before that commit added its port-aware handler. A tree that already contains
that handler should fail the check rather than receive the patch twice.

From anywhere, use absolute checkout paths and check before applying:

```bash
git -C /path/to/xahaud apply --check \
  /path/to/xahaud-scripts/patches/disconnect-rpc-ip-only.patch
git -C /path/to/xahaud apply \
  /path/to/xahaud-scripts/patches/disconnect-rpc-ip-only.patch
```

The patch registers an admin RPC unconditionally; “testnet-only” is an
operational policy, not a compile-time guard. Keep admin RPC bound to trusted
local callers. A request disconnects every active peer with the supplied IP,
so this IP-only variant is for distinct-IP local testnets and is unsafe as a
public/NAT gateway control where several peers may share an address.

`xr-build --patches` deliberately does not apply this patch.

## Development

```bash
uv run ruff check    # lint
uv run ruff format   # format
uv run mypy          # type check
uv run pytest        # test
```

## License

MIT
