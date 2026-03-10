"""Reusable transaction generator for xahaud test networks.

Submits random payments from genesis on each ledger close, with tight
LastLedgerSequence and per-ledger reconciliation for robust sequence tracking.

Usage from scenario scripts:
    gen = ctx.txn_generator(min_txns=5, max_txns=10, start_ledger=10)
    await gen.start()
    await gen.wait_until_ready()
    await ctx.wait_for_ledger(30)
    stats = gen.stats
    await gen.stop()

Usage from CLI (via --generate-txns):
    Wrapped by run_txn_generator_with_monitor() in testing.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from dataclasses import dataclass, field
from typing import Any

import websockets
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.asyncio.transaction import sign
from xrpl.constants import CryptoAlgorithm
from xrpl.core.binarycodec import encode
from xrpl.models import AccountInfo as AccountInfoRequest
from xrpl.models import Fee, Ledger, Payment, ServerInfo
from xrpl.models.requests import SubmitOnly
from xrpl.wallet import Wallet

from xahaud_scripts.testnet.testing import (
    GENESIS_SEED,
    XahauClient,
    create_account_info,
    get_ledger_index,
    patch_definitions_from_server,
    wait_for_network_ready,
)
from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


@dataclass
class TxnGeneratorConfig:
    """Configuration for TxnGenerator."""

    min_txns: int = 3
    max_txns: int = 10
    account_count: int | None = None
    start_ledger: int = 0
    amount_drops: str = "1000000"
    fund_amount_xah: int = 1000
    lls_offset: int = 3


@dataclass
class TxnGeneratorStats:
    """Read-only snapshot of generator state."""

    running: bool
    current_ledger: int
    total_submitted: int
    total_validated: int
    total_expired: int
    confirmed_sequence: int
    pending_count: int
    ledgers_active: int
    results_by_engine: dict[str, int] = field(default_factory=dict)


@dataclass
class _PendingTxn:
    """A submitted transaction awaiting validation or expiry."""

    tx_hash: str
    sequence: int
    last_ledger_seq: int


class TxnGenerator:
    """Background transaction generator with per-ledger reconciliation.

    Owns two WebSocket connections:
    1. XahauClient for RPC requests (Fee, AccountInfo, SubmitOnly)
    2. Raw websockets for ledger+transactions subscription stream
    """

    def __init__(
        self,
        ws_url: str,
        config: TxnGeneratorConfig | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._config = config or TxnGeneratorConfig()

        # Task management
        self._task: asyncio.Task[None] | None = None
        self._ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()

        # Accounts
        self._accounts: list[Any] = []
        self._genesis_wallet: Wallet | None = None

        # Network params (fetched once at startup)
        self._network_id: int | None = None
        self._base_fee: str = "10"

        # Sequence tracking
        self._initial_seq: int = 0
        self._confirmed_seq: int = 0
        self._next_seq: int = 0
        self._pending: dict[str, _PendingTxn] = {}

        # Stats
        self._current_ledger: int = 0
        self._total_submitted: int = 0
        self._total_validated: int = 0
        self._total_expired: int = 0
        self._ledgers_active: int = 0
        self._results_by_engine: dict[str, int] = {}

    @property
    def stats(self) -> TxnGeneratorStats:
        """Snapshot of current generator state."""
        return TxnGeneratorStats(
            running=self.running,
            current_ledger=self._current_ledger,
            total_submitted=self._total_submitted,
            total_validated=self._total_validated,
            total_expired=self._total_expired,
            confirmed_sequence=self._confirmed_seq,
            pending_count=len(self._pending),
            ledgers_active=self._ledgers_active,
            results_by_engine=dict(self._results_by_engine),
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the generator as a background task."""
        if self._task is not None:
            raise RuntimeError("TxnGenerator already started")
        self._stop_event.clear()
        self._ready_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> TxnGeneratorStats:
        """Stop the generator and return final stats."""
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        return self.stats

    async def wait_until_ready(self, timeout: float = 120.0) -> None:
        """Wait until accounts are funded and generator is active."""
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    # -- Internal: main loop -----------------------------------------------

    async def _run(self) -> None:
        """Main loop: connect, fund, subscribe, generate."""
        await wait_for_network_ready(self._ws_url)

        logger.info(f"TxnGenerator connecting to {self._ws_url}...")
        async with AsyncWebsocketClient(self._ws_url) as raw_client:
            client = XahauClient(raw_client)
            await patch_definitions_from_server(client)

            self._genesis_wallet = Wallet.from_seed(
                GENESIS_SEED, algorithm=CryptoAlgorithm.SECP256K1
            )

            # Fetch network params
            fee_response = await client.request(Fee())
            self._base_fee = fee_response.result.get("drops", {}).get("base_fee", "10")
            server_response = await client.request(ServerInfo())
            self._network_id = server_response.result.get("info", {}).get("network_id")

            # Create and fund accounts
            await self._fund_accounts(client)

            # Record initial sequence (post-funding)
            acct_response = await client.request(
                AccountInfoRequest(account=self._genesis_wallet.classic_address)
            )
            self._initial_seq = acct_response.result["account_data"]["Sequence"]
            self._confirmed_seq = self._initial_seq
            self._next_seq = self._initial_seq

            self._ready_event.set()
            logger.info(
                f"TxnGenerator ready: {self._config.min_txns}-{self._config.max_txns} "
                f"txns/ledger, genesis seq={self._initial_seq}"
            )

            # Open raw WS for subscription stream
            ws_raw = await websockets.connect(self._ws_url, open_timeout=10)
            try:
                await ws_raw.send(
                    json.dumps(
                        {
                            "id": 1,
                            "command": "subscribe",
                            "streams": ["ledger", "transactions"],
                        }
                    )
                )
                await ws_raw.recv()  # subscription response

                buffered: dict[str, Any] | None = None

                while not self._stop_event.is_set():
                    # Use buffered message from drain, or read from WS.
                    if buffered is not None:
                        data = buffered
                        buffered = None
                    else:
                        try:
                            message = await asyncio.wait_for(ws_raw.recv(), timeout=5.0)
                        except TimeoutError:
                            continue
                        data = json.loads(message)

                    if data.get("type") == "transaction" and data.get("validated"):
                        self._on_validated_txn(data)
                    elif data.get("type") == "ledgerClosed":
                        ledger_index = data.get("ledger_index", 0)
                        txn_count = data.get("txn_count", 0)
                        self._current_ledger = ledger_index

                        # Drain validated-txn messages for this ledger.
                        # The server sends ledgerClosed BEFORE the per-txn
                        # messages, so we read up to txn_count messages to
                        # let reconciliation see them first.
                        for _ in range(txn_count):
                            try:
                                msg = await asyncio.wait_for(ws_raw.recv(), timeout=2.0)
                            except TimeoutError:
                                break
                            txn_data = json.loads(msg)
                            if txn_data.get("type") == "transaction" and txn_data.get(
                                "validated"
                            ):
                                self._on_validated_txn(txn_data)
                            else:
                                # Non-txn message (e.g. next ledgerClosed) —
                                # buffer it for the next iteration.
                                buffered = txn_data
                                break

                        self._reconcile(ledger_index)
                        await self._submit_batch(client, ledger_index)
            finally:
                await ws_raw.close()

    # -- Internal: funding -------------------------------------------------

    async def _fund_accounts(self, client: XahauClient) -> None:
        """Create deterministic accounts and fund from genesis."""
        assert self._genesis_wallet is not None
        count = self._config.account_count or self._config.max_txns
        self._accounts = [create_account_info(f"txgen-{i}") for i in range(count)]

        # Get current state
        acct_response = await client.request(
            AccountInfoRequest(account=self._genesis_wallet.classic_address)
        )
        genesis_seq = acct_response.result["account_data"]["Sequence"]
        ledger_response = await client.request(Ledger())
        current_ledger = get_ledger_index(ledger_response.result)

        # Burst-submit funding txns
        logger.info(f"Funding {count} accounts from genesis...")
        pending_hashes: set[str] = set()
        fund_amount = str(self._config.fund_amount_xah * 1_000_000)

        for acct in self._accounts:
            payment = Payment(
                account=self._genesis_wallet.classic_address,
                destination=acct.address,
                amount=fund_amount,
                fee=self._base_fee,
                sequence=genesis_seq,
                last_ledger_sequence=current_ledger + 20,
                network_id=self._network_id,
            )
            signed_tx = sign(payment, self._genesis_wallet)
            tx_blob = encode(signed_tx.to_xrpl())
            response = await client.request(SubmitOnly(tx_blob=tx_blob))
            result = response.result
            engine_result = result.get("engine_result", "???")
            tx_hash = result.get("tx_json", {}).get("hash")
            if tx_hash:
                pending_hashes.add(tx_hash)
            if engine_result != "tesSUCCESS":
                logger.warning(f"  Fund {acct.name}: {engine_result}")
            genesis_seq += 1

        # Wait for funding to validate via a temporary subscription
        if pending_hashes:
            logger.info(
                f"Waiting for {len(pending_hashes)} funding txns to validate..."
            )
            ws_fund = await websockets.connect(self._ws_url, open_timeout=10)
            try:
                await ws_fund.send(
                    json.dumps(
                        {
                            "id": 2,
                            "command": "subscribe",
                            "streams": ["transactions"],
                        }
                    )
                )
                await ws_fund.recv()  # subscription response

                total = len(pending_hashes)
                while pending_hashes:
                    message = await ws_fund.recv()
                    data = json.loads(message)
                    if data.get("type") == "transaction" and data.get("validated"):
                        tx_hash = data.get("transaction", {}).get("hash")
                        if tx_hash and tx_hash in pending_hashes:
                            pending_hashes.discard(tx_hash)
                            logger.info(
                                f"  Funded ({total - len(pending_hashes)}/{total})"
                            )
            finally:
                await ws_fund.close()

        logger.info("All accounts funded")

    # -- Internal: reconciliation ------------------------------------------

    def _on_validated_txn(self, data: dict[str, Any]) -> None:
        """Handle a validated transaction from the stream."""
        tx_hash = data.get("transaction", {}).get("hash")
        if tx_hash and tx_hash in self._pending:
            del self._pending[tx_hash]
            self._total_validated += 1

    def _reconcile(self, ledger_index: int) -> None:
        """Reconcile pending transactions against current ledger.

        Called after draining ``txn_count`` validated-transaction messages
        from the stream.  We still use ``< ledger_index`` (not ``<=``) as
        a safety margin — the server sends ``ledgerClosed`` before the
        per-txn messages, and drain may not capture all of them (timeout,
        pseudo-txns counted in txn_count, etc.).
        """
        expired = [
            ptx for ptx in self._pending.values() if ptx.last_ledger_seq < ledger_index
        ]
        for ptx in expired:
            del self._pending[ptx.tx_hash]
        if expired:
            self._total_expired += len(expired)
            logger.debug(
                f"Ledger {ledger_index}: {len(expired)} txns expired (LLS passed)"
            )

        # Ground truth: each validated txn consumed one sequence from initial
        self._confirmed_seq = self._initial_seq + self._total_validated
        # Pending txns have already claimed their sequences optimistically
        self._next_seq = self._confirmed_seq + len(self._pending)

    # -- Internal: submission ----------------------------------------------

    async def _submit_batch(self, client: XahauClient, ledger_index: int) -> None:
        """Submit a batch of random payments for this ledger."""
        assert self._genesis_wallet is not None

        if self._config.start_ledger > 0 and ledger_index < self._config.start_ledger:
            return

        k = random.randint(self._config.min_txns, self._config.max_txns)
        destinations = random.choices(self._accounts, k=k)
        lls = ledger_index + self._config.lls_offset

        results: dict[str, int] = {}
        for dest in destinations:
            payment = Payment(
                account=self._genesis_wallet.classic_address,
                destination=dest.address,
                amount=self._config.amount_drops,
                fee=self._base_fee,
                sequence=self._next_seq,
                last_ledger_sequence=lls,
                network_id=self._network_id,
            )
            signed_tx = sign(payment, self._genesis_wallet)
            tx_blob = encode(signed_tx.to_xrpl())

            response = await client.request(SubmitOnly(tx_blob=tx_blob))
            result = response.result
            engine_result = result.get("engine_result", "unknown")
            results[engine_result] = results.get(engine_result, 0) + 1
            self._results_by_engine[engine_result] = (
                self._results_by_engine.get(engine_result, 0) + 1
            )

            tx_hash = result.get("tx_json", {}).get("hash")

            if engine_result in ("tefPAST_SEQ", "tefMAX_LEDGER"):
                await self._recover_sequence(client)
            else:
                if tx_hash:
                    self._pending[tx_hash] = _PendingTxn(
                        tx_hash=tx_hash,
                        sequence=self._next_seq,
                        last_ledger_seq=lls,
                    )
                self._next_seq += 1
                self._total_submitted += 1

        self._ledgers_active += 1
        stats_str = ", ".join(f"{r}:{n}" for r, n in results.items())

        # Break down pending by LLS expiry ledger
        by_lls: dict[int, int] = {}
        for ptx in self._pending.values():
            by_lls[ptx.last_ledger_seq] = by_lls.get(ptx.last_ledger_seq, 0) + 1
        pending_detail = " ".join(f"lls{lls}:{n}" for lls, n in sorted(by_lls.items()))

        logger.info(
            f"Ledger {ledger_index}: submitted {k} txns ({stats_str}) "
            f"pending={len(self._pending)} [{pending_detail}]"
        )

    async def _recover_sequence(self, client: XahauClient) -> None:
        """Re-fetch sequence from server on tefPAST_SEQ/tefMAX_LEDGER."""
        assert self._genesis_wallet is not None
        acct_response = await client.request(
            AccountInfoRequest(account=self._genesis_wallet.classic_address)
        )
        server_seq = acct_response.result["account_data"]["Sequence"]
        self._pending.clear()
        self._confirmed_seq = server_seq
        self._next_seq = server_seq
        logger.warning(f"Sequence recovered from server: {server_seq}")
