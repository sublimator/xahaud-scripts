"""Shared test utilities for x-testnet.

Provides deterministic account creation, xrpl-py client wrappers,
and the standalone transaction generator runner. These utilities are
used by both the scenario framework and the TxnGenerator.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from hashlib import sha512
from typing import Any

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.core import keypairs
from xrpl.models.requests import Request
from xrpl.wallet import Wallet

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


async def wait_for_network_ready(ws_url: str, timeout: float = 120.0) -> int:
    """Wait for the network to be ready by polling until first ledger closes.

    Handles connection failures gracefully - nodes may not be up yet.

    Args:
        ws_url: WebSocket URL to connect to
        timeout: Maximum time to wait in seconds

    Returns:
        The ledger index when network is ready
    """
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


async def run_txn_generator_with_monitor(
    min_txns: int,
    max_txns: int,
    ws_url: str,
    network_config: Any,
    rpc_client: Any,
    tracked_features: list[str] | None = None,
) -> None:
    """Run a transaction generator with the network monitor in background.

    Creates test accounts from genesis, then on each ledger close submits
    random payments from genesis to those accounts.

    Args:
        min_txns: Minimum transactions per ledger
        max_txns: Maximum transactions per ledger
        ws_url: WebSocket URL to connect to
        network_config: NetworkConfig for the monitor
        rpc_client: RPCClient for the monitor
        tracked_features: Optional list of feature names to track
    """
    from xahaud_scripts.testnet.monitor import NetworkMonitor
    from xahaud_scripts.testnet.txn_generator import TxnGenerator, TxnGeneratorConfig

    stop_event = asyncio.Event()

    monitor = NetworkMonitor(
        rpc_client=rpc_client,
        network_config=network_config,
        tracked_features=tracked_features,
    )

    async def run_monitor() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await monitor.monitor(stop_event=stop_event)

    config = TxnGeneratorConfig(min_txns=min_txns, max_txns=max_txns)
    gen = TxnGenerator(ws_url, config=config)

    monitor_task = asyncio.create_task(run_monitor())

    try:
        await gen.start()
        await gen.wait_until_ready()
        # Run until cancelled (KeyboardInterrupt at asyncio.run boundary)
        while gen.running:
            await asyncio.sleep(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        stats = await gen.stop()
        logger.info(
            f"TxnGenerator stopped: submitted={stats.total_submitted}, "
            f"validated={stats.total_validated}, expired={stats.total_expired}"
        )
        stop_event.set()
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
