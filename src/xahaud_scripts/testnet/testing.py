"""Test script framework for x-testnet.

Provides a simple way to run test scripts against a local testnet.
Test scripts declare accounts with initial balances, and get a
configured xrpl-py client to work with.

Example test script:

    accounts = {"alice": 1000, "bob": 500}

    async def run(ctx):
        alice = ctx.get_account("alice")
        bob = ctx.get_account("bob")

        from xrpl.models import Payment
        from xrpl.transaction import submit_and_wait

        payment = Payment(
            account=alice.address,
            destination=bob.address,
            amount="100000000",
        )
        result = await submit_and_wait(payment, ctx.client, alice.wallet)
        print(f"Payment: {result.result['hash']}")
"""

import importlib.util
from dataclasses import dataclass
from hashlib import sha512
from pathlib import Path
from typing import Any

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.core import keypairs
from xrpl.models import Payment
from xrpl.wallet import Wallet

from xahaud_scripts.hooks import WasmCompiler
from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)

# Standard XRPL genesis account (from "masterpassphrase")
GENESIS_SEED = "snoPBrXtMeMyMHUVTgbuqAfg1SUTb"
GENESIS_ADDRESS = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"


@dataclass
class AccountInfo:
    """Information about a test account."""

    name: str
    address: str  # rXXX...
    public_key: str  # ED... or 02/03...
    seed: str  # sXXX...
    wallet: Wallet


def wallet_from_passphrase(passphrase: str) -> Wallet:
    """Create a deterministic wallet from a passphrase.

    Uses SHA-512 to derive seed bytes from the passphrase,
    ensuring the same passphrase always produces the same wallet.
    """
    seed_bytes = sha512(passphrase.encode("utf-8")).digest()[:16]
    seed = keypairs.generate_seed(entropy=seed_bytes.hex())
    return Wallet.from_seed(seed)


def create_account_info(name: str) -> AccountInfo:
    """Create AccountInfo from a name (uses name as passphrase)."""
    wallet = wallet_from_passphrase(name)
    # Seed is always present when wallet is created from seed
    assert wallet.seed is not None
    return AccountInfo(
        name=name,
        address=wallet.classic_address,
        public_key=wallet.public_key,
        seed=wallet.seed,
        wallet=wallet,
    )


class TestContext:
    """Context passed to test scripts.

    Provides access to the xrpl-py client, account information, and hook compiler.
    """

    def __init__(
        self,
        client: AsyncWebsocketClient,
        accounts: dict[str, AccountInfo],
    ) -> None:
        self.client = client
        self._accounts = accounts
        self._compiler = WasmCompiler()  # Uses default cache

    def get_account(self, name: str) -> AccountInfo:
        """Get account info by name.

        Raises KeyError if account was not declared in the accounts dict.
        """
        if name not in self._accounts:
            available = ", ".join(self._accounts.keys())
            raise KeyError(
                f"Account '{name}' not found. Available accounts: {available}"
            )
        return self._accounts[name]

    def compile_hook(self, source: str, label: str = "hook") -> bytes:
        """Compile C or WAT source to WASM bytecode.

        Uses cached compilation - same source returns cached result.

        Args:
            source: C or WAT hook source code
            label: Label for logging (e.g., "my-hook")

        Returns:
            Compiled WASM bytecode
        """
        return self._compiler.compile(source, label)


def load_test_script(script_path: Path) -> tuple[dict[str, int], Any]:
    """Load a test script module and extract accounts dict and run function.

    Returns:
        Tuple of (accounts dict, run function)

    Raises:
        ValueError: If script is missing required attributes
    """
    spec = importlib.util.spec_from_file_location("test_script", script_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load script: {script_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Get accounts dict (optional, default to empty)
    accounts: dict[str, int] = getattr(module, "accounts", {})

    # Get run function (required)
    if not hasattr(module, "run"):
        raise ValueError(f"Script must define 'async def run(ctx)': {script_path}")

    run_func = module.run
    if not callable(run_func):
        raise ValueError(f"'run' must be a callable: {script_path}")

    return accounts, run_func


async def wait_for_network_ready(ws_url: str, timeout: float = 120.0) -> int:
    """Wait for the network to be ready by polling until first ledger closes.

    Handles connection failures gracefully - nodes may not be up yet.

    Args:
        ws_url: WebSocket URL to connect to
        timeout: Maximum time to wait in seconds

    Returns:
        The ledger index when network is ready
    """
    import asyncio
    import time

    from xrpl.models import ServerInfo

    start = time.monotonic()
    attempt = 0

    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            raise TimeoutError(f"Timed out waiting for network after {timeout}s")

        attempt += 1
        try:
            async with AsyncWebsocketClient(ws_url) as client:
                response = await client.request(ServerInfo())
                info = response.result.get("info", {})
                validated = info.get("validated_ledger")
                if validated:
                    ledger_index = validated.get("seq", 0)
                    if ledger_index > 1:
                        logger.info(f"Network ready at ledger {ledger_index}")
                        return ledger_index

                # Connected but no validated ledger yet
                server_state = info.get("server_state", "unknown")
                logger.info(f"Waiting for consensus... (state: {server_state})")

        except Exception as e:
            if attempt == 1:
                logger.info("Waiting for nodes to start...")
            logger.debug(f"Connection attempt {attempt}: {e}")

        await asyncio.sleep(2.0)


async def fund_account(
    client: AsyncWebsocketClient,
    genesis_wallet: Wallet,
    destination: str,
    amount_xah: int,
) -> dict:
    """Fund an account from genesis.

    Args:
        client: Connected xrpl-py client
        genesis_wallet: Genesis account wallet
        destination: Destination address
        amount_xah: Amount in XAH (will be converted to drops)

    Returns:
        Transaction result
    """
    amount_drops = str(amount_xah * 1_000_000)

    payment = Payment(
        account=genesis_wallet.classic_address,
        destination=destination,
        amount=amount_drops,
    )

    logger.info(f"Funding {destination} with {amount_xah} XAH")
    result = await submit_and_wait(payment, client, genesis_wallet)
    return result.result


async def run_test_script(
    script_path: Path,
    ws_url: str,
    genesis_seed: str | None = None,
) -> None:
    """Run a test script.

    Args:
        script_path: Path to the test script
        ws_url: WebSocket URL to connect to (e.g., ws://localhost:6005)
        genesis_seed: Seed for the genesis account (default: standard genesis)
    """
    if genesis_seed is None:
        genesis_seed = GENESIS_SEED
    logger.info(f"Loading test script: {script_path}")
    accounts_config, run_func = load_test_script(script_path)

    # Create account info for each declared account
    accounts: dict[str, AccountInfo] = {}
    for name in accounts_config:
        accounts[name] = create_account_info(name)
        logger.info(f"Account '{name}': {accounts[name].address}")

    # Connect to run test (network already ready - monitor waited for first ledger)
    logger.info(f"Connecting to {ws_url}...")
    async with AsyncWebsocketClient(ws_url) as client:
        # Fund accounts from genesis
        if accounts_config:
            genesis_wallet = Wallet.from_seed(genesis_seed)
            logger.info(f"Genesis account: {genesis_wallet.classic_address}")

            for name, amount_xah in accounts_config.items():
                account = accounts[name]
                await fund_account(client, genesis_wallet, account.address, amount_xah)

        # Create context and run the test
        ctx = TestContext(client=client, accounts=accounts)

        logger.info("Running test script...")
        await run_func(ctx)

        logger.info("Test script completed")
