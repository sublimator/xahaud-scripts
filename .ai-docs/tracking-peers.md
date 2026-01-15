# Tracking Peers Feature

## Overview

Add `--tracking-peers M` option to spawn non-validating nodes alongside validators.

## Key Insight

A tracking peer is just a node **without** `[validator_token]` in its config. That's the only difference from a validator.

## Port Allocation

With `-n 5 --tracking-peers 3`:

```
Validators (0-4):
  n0: peer=51235, rpc=5005, ws=6005 (EXPLOIT)
  n1: peer=51236, rpc=5006, ws=6006 (CLEAN)
  n2: peer=51237, rpc=5007, ws=6007 (CLEAN)
  n3: peer=51238, rpc=5008, ws=6008 (CLEAN)
  n4: peer=51239, rpc=5009, ws=6009 (CLEAN)

Tracking Peers (5-7):
  n5: peer=51240, rpc=5010, ws=6010 (TRACKING)
  n6: peer=51241, rpc=5011, ws=6011 (TRACKING)
  n7: peer=51242, rpc=5012, ws=6012 (TRACKING)
```

## Implementation Plan

### 1. config.py

- Add `tracking_peer_count: int = 0` to `NetworkConfig`
- Add `is_validator: bool = True` to `NodeInfo`
- Update `role` property: returns "EXPLOIT", "CLEAN", or "TRACKING"
- Add `tracking_peers()` method to `ConfigBuilder`

### 2. generator.py

- Split key generation: validators get keys, tracking peers don't
- New function `generate_tracking_peer_config()`:
  - No `[validator_token]` section
  - No entry in validators.txt
  - Still has `[ips_fixed]` pointing to validators
- Update `generate_all_configs()` to handle both types

### 3. network.py

- Update `generate()` to pass tracking_peer_count
- Handle both node lists in iteration
- No changes to launch logic (same for both types)

### 4. cli.py

- Add `--tracking-peers M` to `generate` command
- Add `--tracking-peers M` to `run` command

### 5. monitor.py

- Update display to show role in Node column or State column
- Example: show "TRACKING" state or "[5 T]" node label

## validators.txt Behavior

- Only validator public keys are listed (not tracking peers)
- All nodes (validators + tracking peers) read the same validators.txt
- Tracking peers trust the validators but don't validate themselves

## No Changes Needed

- Launchers (agnostic to node type - just run rippled with config)
- RPC client (same interface for all nodes)
- WebSocket client (same interface)

## Backward Compatibility

- Default `tracking_peer_count = 0` preserves existing behavior
- `is_validator` defaults to `True` for existing nodes
- network.json can add new field with backward-compatible default
