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
x-run-tests --times 5 -- ripple.app.Import                # repeat 5x
x-run-tests --times=0                                     # build only
x-run-tests --no-fith --times=0 --save-binary @rng-ce     # ordinary build + saved binary
x-run-tests --compile-hooks src/test/app/Export_test.cpp -- ripple.app.Export
x-run-tests --lldb -- ripple.app.Import                   # debug with lldb
x-run-tests --ccache --build-type Release -- unit_test_hook
x-run-tests --coverage --diff-cover -- unit_test_hook      # coverage + diff
x-run-tests --dry-run --reconfigure-build                 # preview commands
x-run-tests --fith -- unit_test_hook                        # dirty-worktree FITH + build + run
x-run-tests --fith --fith-base origin/dev -- unit_test_hook # full branch FITH + build + run
```

Key options:
- Always builds first — `--no-build` was removed deliberately (tests against a
  stale binary present green results as evidence for code they never ran); an
  up-to-date incremental build is a cheap no-op
- `--fith` replaces the ordinary target build with the heuristic `cppt beta
  fith` compile-and-link path. Incomplete dependency evidence warns by default;
  `--fith-strict` opts into refusal. Use `--no-fith` to override the legacy
  `XAHAU_SCRIPTS_FITH_BETA=1` opt-in.
- FITH output is a mixed-generation development binary and cannot be stored as
  an ordinary `@name` alias. `--save-binary` refuses explicit or environment-
  enabled FITH; use `--no-fith` to perform the authoritative build first.
- Builds hold an exclusive per-build-dir lock (`build*/.x-build-lock`) and
  recompact ninja's databases after success — overlapping/killed ninja
  invocations corrupt `.ninja_deps`, which ninja never self-repairs (the
  2026-07-11 replay-the-world incident). Corollary: never run raw `ninja`/
  `cmake -B` against a shared build dir; go through x-run-tests
- `--reconfigure-build` - Force CMake reconfiguration
- `--conan/--no-conan` - Use conan (default: enabled)
- `--ccache/--no-ccache` - ccache with worktree cache sharing
- `--build-type Debug|Release|Coverage`
- `--target rippled|xrpld`
- `--save-binary @name` - Copy an ordinary `rippled` build into the local binary
  registry (incompatible with FITH)
- `--coverage` - Enable coverage instrumentation
- `--coverage-version v1|v2|auto` - v1=llvm-cov, v2=gcovr
- `--diff-cover` - Show uncovered lines in git diff
- `--lldb` - Run under lldb debugger
- `--compile-hooks FILE` - Compile WASM hooks from test file first
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

Refuses to run inside a Xahau (xahaud) checkout (detected via its hooks
trees) — use `x-run-tests` there; xr-build would pollute the repo with the
wrong build tree, patches, and conan state.

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

The root-level `patches/disconnect-rpc-ip-only.patch` is not one of xr-build's
bundled patches. It is a manual xahaud testnet-only admin RPC, and automatically
adding it to xrpld or every xahaud build would be a product/security boundary
violation. See README.md for exact `git apply --check` / `git apply` commands,
compatible source intent, and the IP-wide disconnect warning.

### xr-coverage-diff

Show uncovered lines from existing coverage data.

```bash
xr-coverage-diff --since origin/dev
```

### x-testnet

Launch and manage local xahaud test networks (5 nodes by default).

```bash
# Scenario testing (the primary workflow — see "Scenario testing" below)
x-testnet suite .testnet/scenarios/suite.yml     # run a YAML suite (fresh net per test)
x-testnet suite suite.yml --list-tests           # list test names (+ descriptions)
x-testnet suite suite.yml --test my_test         # one test (or one variant: my_test@heavy)
x-testnet suite suite.yml --dry-run              # print the plan, launch nothing
x-testnet suite suite.yml --test-n 5             # repeat each test (flaky hunting)
x-testnet scenario-test-guide                    # ScenarioContext API reference

# Lifecycle
x-testnet generate                              # generate configs + validator keys
x-testnet generate --node-count 3               # fewer nodes
x-testnet generate --node-count 7 --validators 5 # 5 on the UNL, 2 non-UNL trackers
x-testnet generate --no-fixed-peers             # start isolated; shape topology via connect/RPC
x-testnet generate --log-level-suite consensus   # preset log levels
x-testnet generate --find-ports                  # auto-find free ports
x-testnet setup-aliases -n 7                     # macOS: lo0 aliases 127.0.0.2+ (REQUIRED, see Notes)
x-testnet run                                    # launch nodes + monitor (needs a prior generate)
x-testnet --rippled-path @rng-ce run             # launch with a saved binary
x-testnet run --launcher tmux                    # tmux is the default; iterm/iterm-panes also exist
x-testnet run --node-binary n0:@old --node-binary n1:@new # mixed binary run
x-testnet run --reconnect                        # reconnect to existing network
x-testnet monitor                                # attach monitor; Ctrl+C detaches, nodes live on
x-testnet stop n1,n2 / start n1,n2 / restart n1  # per-node lifecycle (tmux launcher only)
x-testnet snapshot before-restart                # copy net dir to .testnet/output/snapshots/
x-testnet teardown                               # kill processes AND delete node dirs
x-testnet clean                                  # remove generated files

# Inspection
x-testnet check <AMENDMENT_HASH>                 # amendment status table (hash is required)
x-testnet feature ConsensusEntropy               # query an amendment on all nodes
x-testnet feature ConsensusEntropy accept ^n4    # vote yes everywhere except n4
x-testnet server-info n0                         # query specific node
x-testnet server-definitions -o defs.json        # fetch server definitions
x-testnet ledger                                 # latest validated ledger
x-testnet ledger 100 -o l.json                   # specific ledger to file
x-testnet ping n0                                # ping a node
x-testnet node-output n4                         # capture the node's tmux pane (pre-log crashes!)
x-testnet logs Validations trace                 # set log level
x-testnet logs PeerTMProposeSet debug n0         # set log level on specific node
x-testnet topology                               # peer connection map
x-testnet topology-graph -f svg                  # render a Graphviz digraph
x-testnet connect --bi n0 n1                     # add runtime peer connection
x-testnet disconnect --bi n0 n1                  # drop runtime peer connection
x-testnet ports                                  # port listening status
x-testnet check-ports                            # check if ports are free
x-testnet peer-addrs                             # output ip:port list
x-testnet dump-conf                              # show all node configs

# Runtime network simulation (delays, drops, RNG/Export knobs)
x-testnet rc show                                # active runtime config per node
x-testnet rc set delay=200,jitter=50             # all nodes, all peers
x-testnet rc set 'n0->n2:drop=100,msg=proposal'  # directed; quote the '>' in shells
x-testnet rc clear                               # clear_all on every node

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
x-testnet logs-search --run latest/my_test PAT   # search an archived suite/scenario run
x-testnet logs-search --snapshot latest PAT      # search a snapshot
```

`run` key options:
- `--quorum N` - Consensus quorum value
- `--feature @Name|HASH` - Enable amendment at genesis (prefix `-` to disable, repeatable)
- `--seed-majority @Name` - Pre-seed sfMajorities (hold only — still needs a real vote)
- `--start-ledger N` - Start genesis at ledger N (1-256; injects synthetic skip lists)
- `--genesis-file PATH` - Custom genesis ledger
- `--env NAME=VALUE` - Env vars for nodes (or `n0:NAME=VALUE` for specific node)
- `--node-binary n0:@name` - Per-node saved binary override for mixed-binary tests
- `--rc SPEC` / `--rc-clear` - Runtime config at launch (same DSL as `x-testnet rc`)
- `--track-feature NAME` - Add a per-node amendment column to the monitor
- `--generate-txns N|MIN-MAX` - Background payment traffic each ledger
- `--launcher tmux|iterm|iterm-panes`
- `--lldb n0,n4|all` - Run node(s) under lldb for crash backtraces
- `--desktop N` - macOS desktop number for window placement
- `--no-monitor` / `--no-teardown` - Launch and detach / Ctrl+C detaches without killing

`run` launches and monitors a network; it does NOT run scenarios. Scenarios go
through `x-testnet suite` (see Scenario testing below). `--scenario-script` was
removed — suite is the only scenario entry point.

Saved binaries:
- Aliases use an explicit `@name` prefix. Non-`@` paths and peer-binary names
  keep their existing meaning; an `@...` value is always a saved-binary alias.
- `x-run-tests --save-binary @name` copies the built `rippled` into
  `~/.cache/xahaud-scripts/binaries/<name>/` and records metadata in
  `~/.config/xahaud-scripts/binaries.json`.
- The JSON manifest is generated state: branch, commit, dirty flag, source path,
  build type, and `--version` output are best-effort evidence, not a package
  manager.

Scenario testing:
- A scenario is a Python file defining `async def scenario(ctx, log)`, where
  `ctx` is a `ScenarioContext` (ledger/RPC waits, log assertions, topology
  shaping, node lifecycle, tx submission). `x-testnet scenario-test-guide`
  generates the full API reference from `scenario.py` itself.
- One way to run one: `x-testnet suite <yaml>`. Each test gets a fresh network
  (teardown -> generate -> launch -> scenario), configured entirely from the
  YAML `network:` block: `node_count`, `validators`, `quorum`, `fixed_peers`,
  `find_ports`, `features`, `majority_features`, `start_ledger`, `unl_report`,
  `genesis_file`, `env`, `node_env`, `node_binaries`, `extra_args`, `desktop`,
  `log_levels`, `track_features`, `rc`, `topology`, `lldb`, `launcher`,
  `slave_delay`.
- **Breaking difference from the removed `run --teardown`:** suite tears down
  with `keep_dirs=True`, so node dirs and logs survive a run. That is
  deliberate — the old flag deleted exactly the `debug.log` you need to
  diagnose the failure. Clean up with `x-testnet clean` when you want the
  space back.
- `lldb: all` or `lldb: [0, 4]` launches those nodes under lldb, so a crash
  leaves a backtrace in the pane (read it with `x-testnet node-output nN`).
- Output: live net in `testnet/`; suite log at
  `.testnet/output/logs/scenario-test.log`; per-run archives in
  `.testnet/output/runs/<timestamp>-<test>/` and `runs/latest/<test>/`, both
  searchable via `logs-search --run`.
- A script may export `variants = [{"label": "heavy", ...}, ...]`; each entry
  becomes a `name@label` test whose non-`label` keys are scenario kwargs.

### x-inspect-net

Inspect live Xahau/XRPL networks without a local node. Three subcommands.

```bash
# Amendment status (public server_definitions RPC)
x-inspect-net amendments                       # diff mainnet vs testnet
x-inspect-net amendments --diff-only           # only amendments that differ
x-inspect-net amendments --net mainnet          # single network, full table
x-inspect-net amendments --net mainnet --pending  # only not-enabled
x-inspect-net amendments --check NamedHooks     # highlight one amendment
x-inspect-net amendments --url https://my.node  # custom JSON-RPC endpoint
x-inspect-net amendments --samples 5            # cross-ref load-balanced backends
x-inspect-net amendments --json out.json

# Overlay version composition (BFS crawl of /crawl peer endpoint)
x-inspect-net crawl                            # crawl xahau mainnet
x-inspect-net crawl --network testnet
x-inspect-net crawl --network xrpl
x-inspect-net crawl --seeds bacab.alloy.ee:21337   # custom seeds
x-inspect-net crawl --max-nodes 500 --concurrency 64 --json nodes.json

# Visible stale-build/zombie check: live enabled amendments vs local source tags
x-inspect-net zombies --repo ~/projects/xahaud-worktrees/xahaud-feature-export-rng
x-inspect-net zombies --samples 3 --json zombies.json --include-nodes
x-inspect-net zombies --ref xahaud-2026.7.4-CustomBuild+DEBUG=my-local-ref
```

Notes:
- `amendments` relies on xahaud returning the full amendment table to anonymous
  callers (rippled does not, so XRPL is crawl-only).
- The cross-network delta is computed from `enabled` (network truth — every
  synced node agrees). Veto/vote tallies (`vetoed`/`count`/`majority`) are the
  *queried node's* view; public endpoints are load-balanced, so `--samples N`
  hits each endpoint N times to report distinct backends, mark node-local
  fields with `~`, and warn (`⚠`) if `enabled` itself disagrees (a stale node).
- `crawl` unions peers by `public_key` (each node counted once), reports a
  version histogram + by-release rollup. Uses `requests` + a thread pool; peer
  ports use self-signed certs so TLS verification is disabled for `/crawl`.
- `zombies` marks a visible version `INCOMPATIBLE` only when the matching local
  source ref is missing or marks unsupported an amendment that is already
  enabled on the sampled network. Missing-amendment evidence links the exact
  source file searched at the resolved commit; unsupported declarations link
  the declaration line.

### x-get-job

Fetch GitHub Actions job details and logs (works without auth for public repos).

```bash
x-get-job <github-actions-url>
x-get-job "<clip>"                    # use URL from clipboard
x-get-job <url> --no-logs             # steps only, no log output
x-get-job <url> --raw-logs            # unformatted log output
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

### x-quick-check

Run a fast compiler syntax check for dirty C/C++ translation units using
`compile_commands.json`. It does not build, link, or run tests.

```bash
x-quick-check                                      # dirty C/C++ TUs
x-quick-check --dry-run                           # show selected TUs
x-quick-check --tu src/xrpld/app/foo/Bar.cpp      # add a TU for header edits
x-quick-check --since origin/dev                  # changed files since branch
```

## Project Structure

```
src/xahaud_scripts/
├── __init__.py
├── run_tests.py ............... x-run-tests entrypoint (+ x-coverage-diff/report, tail)
├── build_xrpld.py ............. xr-build + xr-coverage-diff entrypoints
├── get_job.py ................. x-get-job entrypoint (GitHubActionsFetcher)
├── build_test_hooks.py ........ x-build-test-hooks entrypoint
├── format_changed.py .......... x-format-changed entrypoint
├── quick_check.py ............. x-quick-check entrypoint
├── codecov.py ................. x-codecov entrypoint (per-PR patch coverage via Codecov API)
├── binary_features.py ......... x-binary-features entrypoint (amendments a source ref knows)
├── binary_registry.py ......... Saved-binary @alias registry (manifest + cache, file-locked)
├── run_stats.py ............... x-run-stats entrypoint (build/test timings from runs DB)
├── hook_toolchain.py .......... Preflight external Hook-fixture compilers
│
├── build/ ..................... Build system utilities
│   ├── config.py .............. BuildConfig dataclass, config mismatch detection
│   ├── cmake.py ............... CMakeOptions, cmake_configure(), cmake_build()
│   ├── conan.py ............... conan_install(), check_conan_available()
│   └── ccache.py .............. ccache env/config, cross-worktree cache sharing
│
├── hooks/ ..................... Stub package; WASM hook compilation delegated to hookz
│
├── patches/ ................... Bundled patch files
│   └── coverage-cmake-clang-gcov.patch
│
├── inspect_net/ ............... Live network inspection (x-inspect-net)
│   ├── cli.py ................. Click group: amendments + crawl + zombies (Rich output)
│   ├── networks.py ............ Network presets (rpc_url, seed hubs, peer port)
│   ├── amendments.py .......... server_definitions fetch/normalize/compare
│   ├── crawl.py ............... Overlay /crawl BFS crawler (thread pool)
│   └── zombies.py ............. Visible versions vs enabled-amendment requirements
│
├── utils/ ..................... Shared utilities
│   ├── logging.py ............. setup_logging(), make_logger(), scenario_file_logging()
│   ├── paths.py ............... get_xahaud_root() (CMakeLists walk-up; NOT the testnet CLI's)
│   ├── clipboard.py ........... get_clipboard()
│   ├── quoting.py ............. shell_quote/shell_export/applescript_string (launcher safety)
│   ├── shell_utils.py ......... run_command(), check_tool_exists(), get_mise_tool_cmd()
│   ├── coverage_llvm.py ....... LLVM coverage (v1): merge profdata, generate reports
│   ├── coverage_diff.py ....... Diff coverage (v1 llvm-cov + v2 gcovr): uncovered changed lines
│   ├── runs_db.py ............. SQLite record of build/test run timings
│   ├── migrations/ ............ Alembic migrations for runs_db
│   └── lldb.py ................ LLDB script generation for debugging
│
└── testnet/ ................... Local testnet management
    ├── cli.py ................. Click CLI group + all subcommands (incl. rc subgroup)
    ├── config.py .............. NetworkConfig, LaunchConfig, NodeInfo, port/genesis helpers
    ├── generator.py ........... ValidatorKeysGenerator, config generation, log level suites
    ├── network.py ............. TestNetwork orchestrator (DI-based)
    ├── suite.py ............... YAML suite runner: fresh net per test, variants, archiving
    ├── scenario.py ............ ScenarioContext + timing/log/topology primitives
    ├── scenario_guide.py ...... Generates the scenario API guide from scenario.py source
    ├── topology.py ............ Directed peer-edge sets, snapshots, diffs, disconnect
    ├── txn_generator.py ....... SubmissionTracker (pure FSM) + async TxnGenerator
    ├── rpc.py ................. RequestsRPCClient (HTTP JSON-RPC)
    ├── websocket.py ........... WebSocketClient + PersistentWebSocketManager
    ├── process.py ............. UnixProcessManager (pgrep, lsof, kill)
    ├── protocols.py ........... Protocol interfaces (Launcher, ControllableLauncher, RPCClient, ...)
    ├── monitor.py ............. NetworkMonitor, Rich table displays
    ├── testing.py ............. Shared test utilities (XahauClient, account derivation, txn gen runner)
    ├── xrpl_patch.py .......... Runtime monkey-patch xrpl-py definitions for Xahau types
    ├── data/
    │   ├── genesis.json ....... Base genesis ledger
    │   ├── genesis_amendments.py  Amendment list by name (source of truth for the above)
    │   └── rebuild_genesis.py . Regenerate/verify genesis.json Amendments (--check)
    ├── cli_handlers/
    │   ├── create_config.py ... Production config generator (mainnet/testnet presets)
    │   ├── hooks_server.py .... Mock webhook receiver (ErrorConfig, ServerStats)
    │   ├── logs_search.py ..... Heap-based log merge across nodes
    │   └── rc.py .............. Runtime config DSL parser + RPC/env handlers
    └── launcher/
        ├── iterm.py ........... iTerm2 window-per-node launcher
        ├── iterm_panes.py ..... iTerm2 single-window pane launcher
        └── tmux.py ............ Tmux launcher (default; only one supporting node lifecycle control)
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
- Run commands from within a xahaud worktree. Note two different root detectors:
  the `x-testnet` CLI uses `git rev-parse --show-toplevel` from CWD (it does NOT
  honor `XAHAUD_ROOT`); `utils/paths.get_xahaud_root()` walks up for
  `CMakeLists.txt` and is used by `x-inspect-net`
- Local testnet default ports: 21235+ (peer), 5005+ (rpc), 6005+ (ws). The peer
  base sits below the macOS ephemeral range (49152+) so outbound sockets from
  any process can't squat on it
- **Peer addressing is distinct-IP, everywhere.** Each node is dialed at its own
  `127.0.0.<id+1>` (`NodeInfo.peer_host`), because rippled's peerfinder dedups
  peers by IP ignoring port — reuse 127.0.0.1 and the mesh collapses to ~1 peer.
  macOS needs `x-testnet setup-aliases -n N` first; Linux routes all of 127/8.
  Never hand-roll `rpc.connect(src, "127.0.0.1", port)`: dial through
  `topology.connect_managed_peer()` (or `ctx.connect_peer`), which is the one
  place the alias precondition is enforced
- A missing alias fails *silently and late* — `connect` returns success for
  merely scheduling the attempt, the TCP connect then fails, and the only
  symptom is `actual=[]` in a topology diff. So `testnet/loopback.py` checks up
  front (at launch and at every dial) and raises with the exact missing
  addresses plus both remedies (`setup-aliases`, and the literal
  `sudo ifconfig lo0 alias ... up` lines). Tests stub the probe via the autouse
  fixture in `tests/conftest.py`, so they never depend on the host's aliases
- Production config ports: 21337 (mainnet peer), 21338 (testnet peer), 5009 (rpc), 6009 (ws)
- Test script accounts are deterministic (SHA-512 of name -> seed -> wallet)
