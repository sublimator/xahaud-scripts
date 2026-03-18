"""Genesis amendment list by name.

The bundled genesis.json includes these amendments as pre-enabled.
Each name is hashed via sha512(name)[:32] to produce the amendment ID.

To add an amendment: just add its name to the list below.
To verify: x-run-tests or `python -m pytest tests/test_genesis.py`
"""

# fmt: off
GENESIS_AMENDMENTS: list[str] = [
    # --- Core Xahau ---
    "XahauGenesis",
    "Hooks",
    "HooksUpdate1",
    "Import",
    "URIToken",
    "BalanceRewards",

    # --- Payment/offer mechanics ---
    "Checks",
    "CheckCashMakesTrustLine",
    "Flow",
    "FlowCross",
    "FlowSortStrands",
    "PaychanAndEscrowForTokens",
    "TicketBatch",

    # --- Auth/security ---
    "RequireFullyCanonicalSig",
    "HardenedValidations",
    "DepositAuth",
    "DepositPreauth",
    "DisallowIncoming",
    "ExpandedSignerList",
    "MultiSignReserve",
    "DeletableAccounts",
    "CryptoConditionsSuite",
    "ImmediateOfferKilled",
    "NegativeUNL",

    # --- NFT ---
    "NonFungibleTokensV1",
    "NonFungibleTokensV1_1",

    # --- XRP fees ---
    "XRPFees",

    # --- Fixes ---
    "fix1513",
    "fix1515",
    "fix1543",
    "fix1571",
    "fix1578",
    "fix1623",
    "fix1781",
    "fixAmendmentMajorityCalc",
    "fixCheckThreading",
    "fixMasterKeyAsRegularKey",
    "fixNFTokenDirV1",
    "fixNFTokenNegOffer",
    "fixNFTokenRemint",
    "fixNonFungibleTokensV1_2",
    "fixPayChanRecipientOwnerDir",
    "fixQualityUpperBound",
    "fixRemoveNFTokenAutoTrustLine",
    "fixRmSmallIncreasedQOffers",
    "fixSTAmountCanonicalize",
    "fixTakerDryOfferRemoval",
    "fixUniversalNumber",
    "fixXahauV2",
]
# fmt: on
