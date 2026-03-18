#!/usr/bin/env python3
"""Rebuild the Amendments list in genesis.json from genesis_amendments.py.

Usage:
    python -m xahaud_scripts.testnet.data.rebuild_genesis
    python -m xahaud_scripts.testnet.data.rebuild_genesis --check  # verify only
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from xahaud_scripts.testnet.data.genesis_amendments import GENESIS_AMENDMENTS

GENESIS_JSON = Path(__file__).parent / "genesis.json"


def _name_to_hash(name: str) -> str:
    return hashlib.sha512(name.encode()).digest()[:32].hex().upper()


def rebuild(*, check_only: bool = False) -> bool:
    with open(GENESIS_JSON) as f:
        genesis = json.load(f)

    for entry in genesis["ledger"]["accountState"]:
        if entry.get("LedgerEntryType") == "Amendments":
            amendments_entry = entry
            break
    else:
        print("ERROR: No Amendments entry in genesis.json", file=sys.stderr)
        return False

    new_hashes = [_name_to_hash(name) for name in GENESIS_AMENDMENTS]
    old_hashes = amendments_entry["Amendments"]

    if old_hashes == new_hashes:
        print(f"genesis.json is up to date ({len(new_hashes)} amendments)")
        return True

    old_set = set(old_hashes)
    new_set = set(new_hashes)
    added = new_set - old_set
    removed = old_set - new_set

    # Build reverse map for display
    name_by_hash = {_name_to_hash(n): n for n in GENESIS_AMENDMENTS}

    if added:
        print(f"Added ({len(added)}):")
        for h in sorted(added):
            print(f"  + {name_by_hash.get(h, h)}")
    if removed:
        print(f"Removed ({len(removed)}):")
        for h in sorted(removed):
            print(f"  - {h}")

    if check_only:
        print(
            "\ngenesis.json is OUT OF DATE. Run: python -m xahaud_scripts.testnet.data.rebuild_genesis"
        )
        return False

    amendments_entry["Amendments"] = new_hashes
    with open(GENESIS_JSON, "w") as f:
        json.dump(genesis, f, indent=2)
        f.write("\n")

    print(f"\nUpdated genesis.json ({len(new_hashes)} amendments)")
    return True


if __name__ == "__main__":
    check_only = "--check" in sys.argv
    ok = rebuild(check_only=check_only)
    sys.exit(0 if ok else 1)
