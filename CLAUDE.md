# xahaud-scripts

Developer tooling for xahaud development. Install globally with:

```bash
uv tool install --force --editable .
```

## Commands

### x-run-tests
Build and run xahaud tests with conan, ccache, coverage, and lldb support.

```bash
x-run-tests --ccache --build-type Release -- unit_test_hook
x-run-tests --coverage --generate-coverage-report -- unit_test_hook
x-run-tests --lldb -- unit_test_hook           # Debug with lldb
x-run-tests --times=0 --build                  # Just build, no tests
x-run-tests --dry-run --reconfigure-build      # Preview commands
```

Key options:
- `--conan/--no-conan` - Use conan (default: enabled)
- `--ccache/--no-ccache` - Use ccache with worktree cache sharing
- `--build-type Debug|Release`
- `--target rippled|xrpld`

### x-testnet
Launch and manage local test networks (5 nodes by default).

```bash
x-testnet generate                    # Generate configs
x-testnet run                         # Launch in iTerm2 tabs
x-testnet check                       # Monitor consensus/amendments
x-testnet topology                    # Show peer connections
x-testnet ports                       # Show port status
x-testnet server-info n0              # Query specific node
x-testnet teardown                    # Kill all nodes
x-testnet clean                       # Remove generated files
```

### x-get-job
Fetch GitHub Actions job details and logs.

```bash
x-get-job <github-actions-url>
x-get-job "<clip>"                    # Use URL from clipboard
```

### x-build-jshooks-header
Build the JS hooks header file using qjsc.

```bash
x-build-jshooks-header --canonical
```

### x-build-test-hooks
Generate SetHook_wasm.h from SetHook_test.cpp. Compiles WASM test blocks.

```bash
x-build-test-hooks                    # Build with caching
x-build-test-hooks -j 4               # Use 4 workers
x-build-test-hooks --force-write      # Force regenerate
```

Requires: wasmcc, hook-cleaner, wat2wasm, clang-format

### x-format-changed
Format changed files (cpp, python, cmake, shell) in git.

```bash
x-format-changed                      # Format dirty files
x-format-changed --since origin/dev   # Format files changed since branch
x-format-changed --all                # Format all files
```

## Project Structure

```
src/xahaud_scripts/
├── build/           # Build utilities (cmake, conan, ccache)
├── testnet/         # Local testnet management
│   ├── cli.py       # Click CLI commands
│   ├── config.py    # NetworkConfig, LaunchConfig, NodeInfo
│   ├── generator.py # Config file generation
│   ├── launcher/    # Terminal launchers (iTerm2)
│   ├── monitor.py   # Rich table displays
│   ├── network.py   # NetworkManager orchestration
│   ├── process.py   # Process management
│   ├── rpc.py       # JSON-RPC client
│   └── websocket.py # WebSocket client
├── utils/           # Shared utilities
├── run_tests.py     # x-run-tests entrypoint
├── get_job.py       # x-get-job entrypoint
├── format_changed.py
└── build_jshooks_header.py
```

## Development

```bash
uv sync --dev
uv run ruff check      # Lint
uv run ruff format     # Format
uv run mypy            # Type check
uv run pytest          # Test
```

## Notes

- Designed for macOS (iTerm2 for testnet, lldb for debugging)
- Run commands from within a xahaud worktree
- Testnet uses ports 51235+ (peer), 5005+ (rpc), 6005+ (ws)
