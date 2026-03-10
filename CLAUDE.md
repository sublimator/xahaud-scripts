# xahaud-scripts

Developer tooling for xahaud development. Python 3.13+, Click CLI, Rich output.

```bash
uv tool install --force --editable .
```

## Commands

### x-run-tests

Build and run xahaud tests with conan, ccache, coverage, and lldb support.

```bash
x-run-tests -- ripple.app.Import                          # build + run test
x-run-tests --no-build -- ripple.app.Import               # skip build
x-run-tests --times 5 -- ripple.app.Import                # repeat 5x
x-run-tests --times=0                                     # build only
x-run-tests --compile-hooks src/test/app/Export_test.cpp -- ripple.app.Export
x-run-tests --lldb -- ripple.app.Import                   # debug with lldb
x-run-tests --ccache --build-type Release -- unit_test_hook
x-run-tests --coverage --diff-cover -- unit_test_hook      # coverage + diff
x-run-tests --dry-run --reconfigure-build                 # preview commands
```

Key options:
- `--build/--no-build` - Build before running (default: build)
- `--reconfigure-build` - Force CMake reconfiguration
- `--conan/--no-conan` - Use conan (default: enabled)
- `--ccache/--no-ccache` - ccache with worktree cache sharing
- `--build-type Debug|Release|Coverage`
- `--target rippled|xrpld`
- `--coverage` - Enable coverage instrumentation
- `--coverage-version v1|v2|auto` - v1=llvm-cov, v2=gcovr
- `--diff-cover` - Show uncovered lines in git diff
- `--lldb` - Run under lldb debugger
- `--compile-hooks FILE` - Compile WASM hooks from test file first
- `--build-jshooks-header` - Build JS hooks header first
- `--unity/--no-unity` - Unity builds
- Test names use dotted suite format after `--`: `ripple.app.Import`

### xr-build

Build xrpld with coverage, patches, and cmake presets.

```bash
xr-build --coverage --test ripple.app.Import               # build + test + coverage
xr-build --coverage --cover-diff --cover-show-uncovered-diff
xr-build --ccache --release                                # release with ccache
xr-build --clean-build                                     # fresh build
xr-build --skip-test                                       # build only
```

Key options:
- `--coverage` - Enable gcov coverage
- `--debug/--release` - Build type
- `--ccache` - Use ccache
- `--test PATTERN` - Test patterns (multiple allowed)
- `--cover-diff` - Coverage of changed lines only
- `--cover-show-uncovered-diff` - Show uncovered diff with Rich panels
- `--cover-html` - Generate HTML coverage report
- `--patches/--no-patches` - Apply bundled patches (default: enabled)
- `--clean/--clean-build` - Clean build artifacts
- `--jobs N` - Parallel build jobs

### xr-coverage-diff

Show uncovered lines from existing coverage data.

```bash
xr-coverage-diff --since origin/dev
```

### x-testnet

Launch and manage local xahaud test networks (5 nodes by default).

```bash
# Lifecycle
x-testnet generate                              # generate configs + validator keys
x-testnet generate --node-count 3               # fewer nodes
x-testnet generate --log-level-suite consensus   # preset log levels
x-testnet generate --find-ports                  # auto-find free ports
x-testnet run                                    # launch nodes + monitor
x-testnet run --launcher tmux                    # use tmux instead of iTerm
x-testnet run --reconnect                        # reconnect to existing network
x-testnet teardown                               # kill all node processes
x-testnet clean                                  # remove generated files

# Inspection
x-testnet check                                  # amendment status table
x-testnet server-info n0                         # query specific node
x-testnet server-definitions -o defs.json        # fetch server definitions
x-testnet ledger                                 # latest validated ledger
x-testnet ledger 100 -o l.json                   # specific ledger to file
x-testnet ping n0                                # trigger injection on node
x-testnet inject n0,n1,n2 --amendment-id X --ledger-seq 100
x-testnet logs Validations trace                 # set log level
x-testnet logs PeerTMProposeSet debug n0         # set log level on specific node
x-testnet topology                               # peer connection map
x-testnet ports                                  # port listening status
x-testnet check-ports                            # check if ports are free
x-testnet peer-addrs                             # output ip:port list
x-testnet dump-conf                              # show all node configs

# Config generation (production)
x-testnet create-config --network mainnet        # mainnet xahaud.cfg + validators
x-testnet create-config --network testnet --db-type RWDB
x-testnet create-config --network mainnet --hooks-server

# Utilities
x-testnet hooks-server                           # mock webhook receiver
x-testnet hooks-server --error 500:0.25          # with random error responses
x-testnet logs-search "LedgerConsensus.*accepted" # search all node logs
x-testnet logs-search -s -5m                     # last 5 minutes of logs
x-testnet logs-search Shuffle --tail 1000 -n 0-2 # tail + filter nodes
x-testnet scenario-test-guide                    # show scenario script docs
```

`run` key options:
- `--amendment-id` - Amendment hash for injection
- `--quorum N` - Consensus quorum value
- `--flood N` - Inject every N ledgers
- `--feature HASH` - Enable amendment (prefix `-` to disable, repeatable)
- `--genesis-file PATH` - Custom genesis ledger
- `--env NAME=VALUE` - Env vars for nodes (or `n0:NAME=VALUE` for specific node)
- `--launcher tmux|iterm|iterm-panes`
- `--desktop N` - macOS desktop number for window placement
- `--scenario-script PATH` - Run scenario script instead of monitoring
- `--teardown` - Kill nodes after scenario/txn-gen finishes

### x-get-job

Fetch GitHub Actions job details and logs (works without auth for public repos).

```bash
x-get-job <github-actions-url>
x-get-job "<clip>"                    # use URL from clipboard
x-get-job <url> --no-logs             # steps only, no log output
x-get-job <url> --raw-logs            # unformatted log output
```

### x-build-jshooks-header

Build the JS hooks header file using qjsc (QuickJS compiler).

```bash
x-build-jshooks-header --canonical
x-build-jshooks-header --qjsc-binary /path/to/qjsc
```

### x-build-test-hooks

Extract WASM test blocks from C++ source, compile to WASM, generate header.

```bash
x-build-test-hooks                    # build with caching
x-build-test-hooks -j 4              # 4 parallel workers
x-build-test-hooks --force-write     # force regenerate
```

Requires: wasmcc, hook-cleaner, wat2wasm, clang-format

### x-format-changed

Format changed files (C++, Python, shell, CMake) in git.

```bash
x-format-changed                      # format dirty files
x-format-changed --since origin/dev   # files changed since branch
x-format-changed --all                # all files in repo
x-format-changed --cpp-only           # only C++ files
x-format-changed --stage              # git-add formatted files
x-format-changed --no-cmake           # skip CMake formatting
```

Uses: clang-format 18 (via mise), ruff, shfmt, cmake-format

## Project Structure

```
src/xahaud_scripts/
├── __init__.py
├── run_tests.py ............... x-run-tests entrypoint
├── build_xrpld.py ............. xr-build + xr-coverage-diff entrypoints
├── get_job.py ................. x-get-job entrypoint (GitHubActionsFetcher)
├── build_jshooks_header.py .... x-build-jshooks-header entrypoint
├── build_test_hooks.py ........ x-build-test-hooks entrypoint
├── format_changed.py .......... x-format-changed entrypoint
│
├── build/ ..................... Build system utilities
│   ├── config.py .............. BuildConfig dataclass, config mismatch detection
│   ├── cmake.py ............... CMakeOptions, cmake_configure(), cmake_build()
│   ├── conan.py ............... conan_install(), check_conan_available()
│   └── ccache.py .............. ccache env/config, cross-worktree cache sharing
│
├── hooks/ ..................... WASM hook compilation
│   └── compiler.py ............ WasmCompiler, CompilationCache, SourceValidator, BinaryChecker
│
├── patches/ ................... Bundled patch files
│   └── coverage-cmake-clang-gcov.patch
│
├── utils/ ..................... Shared utilities
│   ├── logging.py ............. setup_logging(), make_logger()
│   ├── paths.py ............... get_xahaud_root()
│   ├── clipboard.py ........... get_clipboard()
│   ├── shell_utils.py ......... run_command(), check_tool_exists(), get_mise_tool_cmd()
│   ├── coverage.py ............ LLVM coverage (v1): merge profdata, generate reports
│   ├── coverage_diff.py ....... Diff coverage (v1 llvm-cov + v2 gcovr): uncovered changed lines
│   └── lldb.py ................ LLDB script generation for debugging
│
└── testnet/ ................... Local testnet management
    ├── cli.py ................. Click CLI group + all subcommands
    ├── config.py .............. NetworkConfig, LaunchConfig, NodeInfo, port/genesis helpers
    ├── generator.py ........... ValidatorKeysGenerator, config generation, log level suites
    ├── network.py ............. TestNetwork orchestrator (DI-based)
    ├── rpc.py ................. RequestsRPCClient (HTTP JSON-RPC)
    ├── websocket.py ........... WebSocketClient (async ledger streaming)
    ├── process.py ............. UnixProcessManager (pgrep, lsof, kill)
    ├── protocols.py ........... Protocol interfaces (Launcher, RPCClient, ProcessManager, KeyGenerator)
    ├── monitor.py ............. NetworkMonitor, Rich table displays
    ├── testing.py ............. Shared test utilities (XahauClient, account derivation, txn gen runner)
    ├── xrpl_patch.py .......... Runtime monkey-patch xrpl-py definitions for Xahau types
    ├── data/genesis.json ...... Base genesis ledger
    ├── cli_handlers/
    │   ├── create_config.py ... Production config generator (mainnet/testnet presets)
    │   ├── hooks_server.py .... Mock webhook receiver (ErrorConfig, ServerStats)
    │   └── logs_search.py ..... Heap-based log merge across nodes
    └── launcher/
        ├── iterm.py ........... iTerm2 tab launcher
        ├── iterm_panes.py ..... iTerm2 pane management
        └── tmux.py ............ Tmux launcher
```

## Key Design Patterns

- **Dependency injection** - TestNetwork accepts pluggable Launcher, RPCClient, ProcessManager via Protocol interfaces
- **Caching** - WASM bytecode cached by source+binary hash in `~/.cache/xahaud-hooks`; ccache shared across worktrees via `~/.config/xahaud-scripts/ccache.conf`
- **Dry-run mode** - Many commands support `--dry-run` to preview without executing
- **Coverage v1 vs v2** - v1 uses llvm-profdata + llvm-cov (source-based), v2 uses gcovr (gcov-based); auto-detected from CMakeCache.txt

## Development

```bash
uv sync --dev
uv run ruff check      # lint
uv run ruff format     # format
uv run mypy            # type check
uv run pytest          # test
```

## Notes

- Designed for macOS (iTerm2/tmux for testnet, lldb for debugging)
- Run commands from within a xahaud worktree (auto-detects root via git or CMakeLists.txt)
- Local testnet default ports: 51235+ (peer), 5005+ (rpc), 6005+ (ws)
- Production config ports: 21337 (mainnet peer), 21338 (testnet peer), 5009 (rpc), 6009 (ws)
- Test script accounts are deterministic (SHA-512 of name -> seed -> wallet)
