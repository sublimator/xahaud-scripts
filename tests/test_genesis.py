"""Verify genesis_amendments.py produces the same hashes as genesis.json."""

import hashlib
import json
from pathlib import Path

from xahaud_scripts.testnet.data.genesis_amendments import GENESIS_AMENDMENTS

GENESIS_JSON = (
    Path(__file__).parent.parent / "src/xahaud_scripts/testnet/data/genesis.json"
)


def _name_to_hash(name: str) -> str:
    return hashlib.sha512(name.encode()).digest()[:32].hex().upper()


def test_named_amendments_match_genesis_json():
    """The named amendment list must produce exactly the hashes in genesis.json."""
    with open(GENESIS_JSON) as f:
        genesis = json.load(f)

    for entry in genesis["ledger"]["accountState"]:
        if entry.get("LedgerEntryType") == "Amendments":
            json_hashes = set(entry["Amendments"])
            break
    else:
        raise AssertionError("No Amendments entry in genesis.json")

    named_hashes = {_name_to_hash(name) for name in GENESIS_AMENDMENTS}

    missing_from_names = json_hashes - named_hashes
    extra_in_names = named_hashes - json_hashes

    assert not missing_from_names, (
        f"Hashes in genesis.json but not in GENESIS_AMENDMENTS: {missing_from_names}"
    )
    assert not extra_in_names, (
        f"Hashes from GENESIS_AMENDMENTS not in genesis.json: {extra_in_names}"
    )


def test_no_duplicate_amendments():
    """No duplicate names in the list."""
    assert len(GENESIS_AMENDMENTS) == len(set(GENESIS_AMENDMENTS))


def test_resolve_feature_hash_strips_feature_prefix():
    """The C++ 'feature' prefix is stripped, 'fix' prefix is kept."""
    from xahaud_scripts.testnet.config import _resolve_feature_hash

    # featureX → hashes as X
    assert _resolve_feature_hash("featureExport") == _name_to_hash("Export")
    assert _resolve_feature_hash("featureHooks") == _name_to_hash("Hooks")

    # fixX → hashes as fixX (fix is part of the name)
    assert _resolve_feature_hash("fixXahauV2") == _name_to_hash("fixXahauV2")

    # Plain names pass through
    assert _resolve_feature_hash("ConsensusEntropy") == _name_to_hash(
        "ConsensusEntropy"
    )
    assert _resolve_feature_hash("Export") == _name_to_hash("Export")
