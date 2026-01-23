"""Monkey-patch xrpl-py definitions at runtime.

xrpl-py loads definitions at import time from a hardcoded JSON file with no
API to update them. This module provides a function to patch the definitions
module with server_definitions fetched from a running node.

Usage:
    from xahaud_scripts.testnet.xrpl_patch import patch_definitions

    # Fetch server_definitions via RPC
    server_defs = rpc_client.server_definitions(node_id=0)

    # Patch xrpl-py
    patch_definitions(server_defs)

    # Now encode() will work with Xahau types like SetHook
    from xrpl.core.binarycodec import encode
    tx_blob = encode({"TransactionType": "SetHook", ...})
"""

from typing import Any

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


def patch_definitions(server_defs: dict[str, Any]) -> None:
    """Patch xrpl-py binarycodec definitions with server definitions.

    This replaces all module-level variables in xrpl.core.binarycodec.definitions.definitions
    with values derived from the server_definitions response.

    Args:
        server_defs: The result from server_definitions RPC call
    """
    # Import the module we're patching
    from xrpl.core.binarycodec.definitions import definitions as defs_module
    from xrpl.core.binarycodec.definitions.field_header import FieldHeader
    from xrpl.core.binarycodec.definitions.field_info import FieldInfo

    # Convert FIELDS from list of tuples to dict (same as load_definitions does)
    fields_dict = dict(server_defs["FIELDS"])

    # Build the main definitions dict
    new_defs = {
        "TYPES": server_defs["TYPES"],
        "FIELDS": fields_dict,
        "LEDGER_ENTRY_TYPES": server_defs["LEDGER_ENTRY_TYPES"],
        "TRANSACTION_RESULTS": server_defs["TRANSACTION_RESULTS"],
        "TRANSACTION_TYPES": server_defs["TRANSACTION_TYPES"],
    }

    # Build reverse lookup maps
    tx_type_code_to_str = {v: k for k, v in new_defs["TRANSACTION_TYPES"].items()}
    tx_results_code_to_str = {v: k for k, v in new_defs["TRANSACTION_RESULTS"].items()}
    ledger_entry_code_to_str = {v: k for k, v in new_defs["LEDGER_ENTRY_TYPES"].items()}

    # Build delegations maps
    granular_permissions = {
        "TrustlineAuthorize": 65537,
        "TrustlineFreeze": 65538,
        "TrustlineUnfreeze": 65539,
        "AccountDomainSet": 65540,
        "AccountEmailHashSet": 65541,
        "AccountMessageKeySet": 65542,
        "AccountTransferRateSet": 65543,
        "AccountTickSizeSet": 65544,
        "PaymentMint": 65545,
        "PaymentBurn": 65546,
        "MPTokenIssuanceLock": 65547,
        "MPTokenIssuanceUnlock": 65548,
    }
    tx_delegations = {k: v + 1 for k, v in new_defs["TRANSACTION_TYPES"].items()}
    delegable_str_to_code = {**tx_delegations, **granular_permissions}
    delegable_code_to_str = {v: k for k, v in delegable_str_to_code.items()}

    # Build field info and header maps
    type_ordinal_map = new_defs["TYPES"]
    field_info_map = {}
    field_header_name_map = {}

    for field_name, field_entry in fields_dict.items():
        field_info = FieldInfo(
            field_entry["nth"],
            field_entry["isVLEncoded"],
            field_entry["isSerialized"],
            field_entry["isSigningField"],
            field_entry["type"],
        )
        header = FieldHeader(type_ordinal_map[field_entry["type"]], field_entry["nth"])
        field_info_map[field_name] = field_info
        field_header_name_map[header] = field_name

    # Patch all module-level variables
    defs_module._DEFINITIONS = new_defs
    defs_module._TYPE_ORDINAL_MAP = type_ordinal_map
    defs_module._TRANSACTION_TYPE_CODE_TO_STR_MAP = tx_type_code_to_str
    defs_module._TRANSACTION_RESULTS_CODE_TO_STR_MAP = tx_results_code_to_str
    defs_module._LEDGER_ENTRY_TYPES_CODE_TO_STR_MAP = ledger_entry_code_to_str
    defs_module._FIELD_INFO_MAP = field_info_map
    defs_module._FIELD_HEADER_NAME_MAP = field_header_name_map
    defs_module._tx_delegations = tx_delegations
    defs_module._DELEGABLE_PERMISSIONS_STR_TO_CODE_MAP = delegable_str_to_code
    defs_module._DELEGABLE_PERMISSIONS_CODE_TO_STR_MAP = delegable_code_to_str

    # Log what we added
    original_tx_types = {
        "Payment",
        "EscrowCreate",
        "EscrowFinish",
        "AccountSet",
        "EscrowCancel",
        "RegularKeySet",
        "NickNameSet",
        "OfferCreate",
        "OfferCancel",
        "Contract",
        "TicketCreate",
        "TicketCancel",
        "SignerListSet",
        "PaymentChannelCreate",
        "PaymentChannelFund",
        "PaymentChannelClaim",
        "CheckCreate",
        "CheckCash",
        "CheckCancel",
        "DepositPreauth",
        "TrustSet",
        "AccountDelete",
        "SetHook",
        "EnableAmendment",
        "SetFee",
        "UNLModify",
    }
    new_types = set(new_defs["TRANSACTION_TYPES"].keys()) - original_tx_types
    if new_types:
        logger.debug(f"Added transaction types: {', '.join(sorted(new_types))}")

    logger.info(
        f"Patched xrpl-py definitions: "
        f"{len(new_defs['TRANSACTION_TYPES'])} tx types, "
        f"{len(fields_dict)} fields"
    )
