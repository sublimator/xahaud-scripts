"""Test script framework for x-testnet.

Provides a simple way to run test scripts against a local testnet.
Test scripts declare accounts with initial balances, and get a
configured xrpl-py client to work with.

Example test script:

    accounts = {"alice": 1000, "bob": 500}

    async def run(ctx):
        alice = ctx.get_account("alice")
        bob = ctx.get_account("bob")

        from xrpl.models import AccountInfo
        resp = await ctx.client.request(AccountInfo(account=alice.address))
        print(f"Alice balance: {resp.result['account_data']['Balance']}")
"""

import asyncio
import contextlib
import importlib.util
from dataclasses import dataclass
from hashlib import sha512
from pathlib import Path
from typing import Any

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.constants import CryptoAlgorithm
from xrpl.core import keypairs
from xrpl.models import Payment
from xrpl.models.requests import Request
from xrpl.wallet import Wallet

from xahaud_scripts.hooks import WasmCompiler
from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


class XahauClient:
    """Wrapper around AsyncWebsocketClient that uses api_version=1.

    xahaud requires api_version=1, but xrpl-py defaults to api_version=2.
    This wrapper automatically sets api_version=1 on all requests.
    """

    def __init__(self, client: AsyncWebsocketClient) -> None:
        self._client = client

    async def request(self, request: Request) -> Any:
        """Send a request with api_version=1."""
        # Create a copy of the request with api_version=1
        request_dict = request.to_dict()
        request_dict["api_version"] = 1
        patched = request.from_dict(request_dict)
        return await self._client.request(patched)

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else to the underlying client."""
        return getattr(self._client, name)


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


def get_ledger_index(ledger_result: dict[str, Any]) -> int:
    """Extract ledger_index from a Ledger response (handles both formats)."""
    idx = ledger_result.get("ledger_index")
    if not idx:
        idx = ledger_result.get("open", {}).get("ledger", {}).get("ledger_index")
    if not idx:
        idx = ledger_result.get("closed", {}).get("ledger", {}).get("ledger_index")
    return int(idx) if idx else 0


async def patch_definitions_from_server(client: "XahauClient") -> None:
    """Fetch server_definitions and patch xrpl-py's binarycodec.

    xrpl-py has hardcoded definitions that don't include Xahau-specific
    transaction types like SetHook. This fetches definitions from the
    server and patches them in at runtime.
    """
    from xrpl.models.requests import GenericRequest

    from xahaud_scripts.testnet.xrpl_patch import patch_definitions

    # Fetch server definitions
    response = await client.request(GenericRequest(command="server_definitions"))
    server_defs = response.result

    # Patch xrpl-py with the server definitions
    patch_definitions(server_defs)


class TestContext:
    """Context passed to test scripts.

    Provides access to the xrpl-py client, account information, and hook compiler.
    """

    def __init__(
        self,
        client: XahauClient,
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

    async def submit_tx(
        self, tx_dict: dict[str, Any], wallet: Wallet
    ) -> dict[str, Any]:
        """Sign and submit a raw transaction dict.

        Use this for Xahau-specific transactions like SetHook that aren't
        in xrpl-py. Autofills fee, sequence, and last_ledger_sequence.

        Args:
            tx_dict: Transaction as a dict (e.g., {"TransactionType": "SetHook", ...})
            wallet: Wallet to sign with

        Returns:
            Submit response result
        """
        from xrpl.core.binarycodec import encode
        from xrpl.core.keypairs import sign as sign_blob
        from xrpl.models import Fee, Ledger
        from xrpl.models.requests import SubmitOnly

        # Autofill fee, sequence, last_ledger_sequence if not provided
        if "Fee" not in tx_dict:
            fee_response = await self.client.request(Fee())
            if "drops" not in fee_response.result:
                logger.error(f"Fee request failed: {fee_response.result}")
                raise ValueError(f"Fee request failed: {fee_response.result}")
            # Use open_ledger_cost (higher) to ensure tx gets in
            tx_dict["Fee"] = fee_response.result["drops"].get(
                "open_ledger_cost", "1000000"
            )

        if "Sequence" not in tx_dict:
            from xrpl.models import AccountInfo

            acct_response = await self.client.request(
                AccountInfo(account=wallet.classic_address)
            )
            if "account_data" not in acct_response.result:
                logger.error(f"AccountInfo request failed: {acct_response.result}")
                raise ValueError(
                    f"AccountInfo request for {wallet.classic_address} failed: "
                    f"{acct_response.result}"
                )
            tx_dict["Sequence"] = acct_response.result["account_data"]["Sequence"]

        if "LastLedgerSequence" not in tx_dict:
            ledger_response = await self.client.request(Ledger())
            ledger_index = get_ledger_index(ledger_response.result)
            if not ledger_index:
                logger.error(
                    f"Ledger request - no ledger_index: {ledger_response.result}"
                )
                raise ValueError(f"Ledger request failed: {ledger_response.result}")
            tx_dict["LastLedgerSequence"] = ledger_index + 20

        # Get NetworkID from server (required for Xahau)
        if "NetworkID" not in tx_dict:
            from xrpl.models import ServerInfo

            server_response = await self.client.request(ServerInfo())
            network_id = server_response.result.get("info", {}).get("network_id")
            if network_id:
                tx_dict["NetworkID"] = network_id

        # Set Account if not provided
        if "Account" not in tx_dict:
            tx_dict["Account"] = wallet.classic_address

        # Add signing fields
        tx_dict["SigningPubKey"] = wallet.public_key

        # Encode for signing (different from full encoding!)
        from xrpl.core.binarycodec import encode_for_signing

        signing_blob = encode_for_signing(tx_dict)
        signature = sign_blob(signing_blob, wallet.private_key)
        tx_dict["TxnSignature"] = signature

        # Encode full transaction with signature
        signed_blob = encode(tx_dict)

        # Submit
        response = await self.client.request(SubmitOnly(tx_blob=signed_blob))
        result = response.result

        # Log if there's an error
        engine_result = result.get("engine_result", "")
        if engine_result and not engine_result.startswith("tes"):
            logger.warning(f"Transaction result: {engine_result}")
            logger.debug(f"Full response: {result}")

        return result


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
                response = await client.request(ServerInfo(api_version=1))
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
    client: XahauClient,
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
    from xrpl.asyncio.transaction import sign
    from xrpl.core.binarycodec import encode
    from xrpl.models import AccountInfo, Fee, Ledger, ServerInfo
    from xrpl.models.requests import SubmitOnly

    amount_drops = str(amount_xah * 1_000_000)

    # Get current ledger, account info, and network_id
    fee_response = await client.request(Fee())
    ledger_response = await client.request(Ledger())
    account_response = await client.request(
        AccountInfo(account=genesis_wallet.classic_address)
    )
    server_response = await client.request(ServerInfo())

    fee = fee_response.result.get("drops", {}).get("base_fee", "10")
    sequence = account_response.result.get("account_data", {}).get("Sequence", 1)
    network_id = server_response.result.get("info", {}).get("network_id")
    ledger_index = get_ledger_index(ledger_response.result)

    payment = Payment(
        account=genesis_wallet.classic_address,
        destination=destination,
        amount=amount_drops,
        fee=fee,
        sequence=sequence,
        last_ledger_sequence=ledger_index + 10,
        network_id=network_id,
    )

    signed_tx = sign(payment, genesis_wallet)

    logger.info(f"Funding {destination} with {amount_xah} XAH")
    tx_blob = encode(signed_tx.to_xrpl())
    logger.debug(f"  tx_blob: {tx_blob[:40]}...")
    submit_response = await client.request(SubmitOnly(tx_blob=tx_blob))

    # Log submit result
    result = submit_response.result
    engine_result = result.get("engine_result", "???")
    logger.info(f"  Submit result: {engine_result}")
    if engine_result != "tesSUCCESS":
        logger.warning(f"  Full response: {result}")

    # Get tx hash and wait for validation
    tx_hash = result.get("tx_json", {}).get("hash")
    if not tx_hash:
        logger.warning(f"No tx hash in response: {result}")
        return result

    # Poll for validation (check every ledger close ~4s)
    from xrpl.models import Tx

    for attempt in range(15):  # ~60 seconds max
        await asyncio.sleep(4)
        try:
            tx_response = await client.request(Tx(transaction=tx_hash))
            if tx_response.result.get("validated"):
                logger.info(f"  Funded {destination[:8]}... (validated)")
                return tx_response.result
        except Exception as e:
            logger.debug(f"Tx check attempt {attempt}: {e}")

    logger.warning(f"Funding tx {tx_hash} not validated after 60s")
    return submit_response.result


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
    async with AsyncWebsocketClient(ws_url) as raw_client:
        # Wrap client to use api_version=1 (required by xahaud)
        client = XahauClient(raw_client)

        # Patch xrpl-py definitions with Xahau types (SetHook, etc.)
        await patch_definitions_from_server(client)

        # Fund accounts from genesis
        if accounts_config:
            genesis_wallet = Wallet.from_seed(
                genesis_seed, algorithm=CryptoAlgorithm.SECP256K1
            )
            logger.info(f"Genesis account: {genesis_wallet.classic_address}")

            for name, amount_xah in accounts_config.items():
                account = accounts[name]
                await fund_account(client, genesis_wallet, account.address, amount_xah)

        # Create context and run the test
        ctx = TestContext(client=client, accounts=accounts)

        logger.info("Running test script...")
        await run_func(ctx)

        logger.info("Test script completed")


async def run_test_with_monitor(
    script_path: Path,
    ws_url: str,
    network_config: Any,
    rpc_client: Any,
    genesis_seed: str | None = None,
    tracked_amendment: str | None = None,
) -> None:
    """Run a test script with the network monitor running in background.

    The monitor displays network status tables while the test script runs.
    Output from both is interleaved.

    Args:
        script_path: Path to the test script
        ws_url: WebSocket URL to connect to
        network_config: NetworkConfig for the monitor
        rpc_client: RPCClient for the monitor
        genesis_seed: Seed for the genesis account
        tracked_amendment: Optional amendment ID to track
    """
    from xahaud_scripts.testnet.monitor import NetworkMonitor

    if genesis_seed is None:
        genesis_seed = GENESIS_SEED

    # Load and validate test script
    logger.info(f"Loading test script: {script_path}")
    accounts_config, run_func = load_test_script(script_path)

    # Create account info for each declared account
    accounts: dict[str, AccountInfo] = {}
    for name in accounts_config:
        accounts[name] = create_account_info(name)
        logger.info(f"Account '{name}': {accounts[name].address}")

    # Create stop event for monitor
    stop_event = asyncio.Event()

    # Create monitor
    monitor = NetworkMonitor(
        rpc_client=rpc_client,
        network_config=network_config,
        tracked_amendment=tracked_amendment,
    )

    async def run_monitor() -> None:
        """Run monitor until stop_event is set."""
        with contextlib.suppress(asyncio.CancelledError):
            await monitor.monitor(stop_event=stop_event)

    async def run_script() -> None:
        """Run the test script."""
        # Wait for first ledger close before starting
        # (monitor will be showing progress in the meantime)
        await wait_for_network_ready(ws_url)

        logger.info(f"Connecting to {ws_url}...")
        async with AsyncWebsocketClient(ws_url) as raw_client:
            client = XahauClient(raw_client)

            # Patch xrpl-py definitions with Xahau types (SetHook, etc.)
            await patch_definitions_from_server(client)

            # Fund accounts from genesis
            if accounts_config:
                genesis_wallet = Wallet.from_seed(
                    genesis_seed, algorithm=CryptoAlgorithm.SECP256K1
                )
                logger.info(f"Genesis account: {genesis_wallet.classic_address}")

                for name, amount_xah in accounts_config.items():
                    account = accounts[name]
                    await fund_account(
                        client, genesis_wallet, account.address, amount_xah
                    )

            # Create context and run the test
            ctx = TestContext(client=client, accounts=accounts)

            logger.info("Running test script...")
            await run_func(ctx)

            logger.info("Test script completed")

    # Run both concurrently
    monitor_task = asyncio.create_task(run_monitor())

    try:
        await run_script()
    finally:
        # Stop monitor when script finishes
        stop_event.set()
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
