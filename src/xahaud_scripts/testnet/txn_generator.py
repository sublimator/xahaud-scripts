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
from enum import Enum
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
    fund_amount_xah: int = 100_000
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


# -- SubmissionTracker: pure state machine (no I/O) ---------------------------


class BatchAction(Enum):
    """Action to take after processing a submit result."""

    CONTINUE = "continue"
    BREAK = "break"
    RECOVER = "recover"
    SKIP = "skip"


class SubmissionTracker:
    """Pure state machine for transaction sequence tracking and reconciliation.

    No async, no I/O, no xrpl imports. All state transitions are synchronous
    and deterministic, making this fully unit-testable.
    """

    def __init__(self, config: TxnGeneratorConfig) -> None:
        self._config = config

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

    # -- Lifecycle -------------------------------------------------------------

    def reset(self, initial_seq: int) -> None:
        """Set initial sequence after funding. Call once before submitting."""
        self._initial_seq = initial_seq
        self._confirmed_seq = initial_seq
        self._next_seq = initial_seq

    def on_recovery(self, server_seq: int) -> None:
        """Re-sync state from server's AccountInfo.Sequence.

        Called after a sequence error (tefPAST_SEQ, etc.) triggers a server
        query. Clears all pending and syncs total_validated so that the next
        reconcile() doesn't revert the recovered sequence.
        """
        self._pending.clear()
        self._confirmed_seq = server_seq
        self._next_seq = server_seq
        self._total_validated = server_seq - self._initial_seq

    # -- Per-txn events --------------------------------------------------------

    def on_submit_result(
        self, engine_result: str, tx_hash: str | None, lls: int
    ) -> BatchAction:
        """Process an engine result from SubmitOnly. Returns the action to take.

        The caller must pass the tx_hash and LLS used for this submission.
        The tracker manages sequence increment and pending tracking internally.
        """
        self._results_by_engine[engine_result] = (
            self._results_by_engine.get(engine_result, 0) + 1
        )

        if engine_result in ("tesSUCCESS", "terQUEUED"):
            if tx_hash:
                self._pending[tx_hash] = _PendingTxn(
                    tx_hash=tx_hash,
                    sequence=self._next_seq,
                    last_ledger_seq=lls,
                )
            self._next_seq += 1
            self._total_submitted += 1
            return BatchAction.CONTINUE

        if engine_result in ("tefPAST_SEQ", "tefMAX_LEDGER", "terPRE_SEQ"):
            return BatchAction.RECOVER

        if engine_result.startswith("tel"):
            return BatchAction.BREAK

        return BatchAction.SKIP

    def on_validated_txn(self, tx_hash: str) -> None:
        """A transaction we're tracking was validated on-chain."""
        if tx_hash in self._pending:
            del self._pending[tx_hash]
            self._total_validated += 1

    # -- Per-ledger events -----------------------------------------------------

    def should_submit(self, ledger_index: int) -> bool:
        """Whether to submit transactions for this ledger."""
        return not (
            self._config.start_ledger > 0 and ledger_index < self._config.start_ledger
        )

    def prepare_batch(self, ledger_index: int) -> tuple[int, int]:
        """Prepare batch parameters: (count, last_ledger_sequence)."""
        k = random.randint(self._config.min_txns, self._config.max_txns)
        lls = ledger_index + self._config.lls_offset
        return k, lls

    def on_ledger_closed(self, ledger_index: int) -> None:
        """Handle a ledgerClosed event — expire and reconcile.

        Call this after draining validated-transaction messages (via
        ``on_validated_txn``) for the closing ledger.  Uses ``< ledger_index``
        (not ``<=``) as a safety margin — the server sends ``ledgerClosed``
        before the per-txn messages, and drain may not capture all of them
        (timeout, pseudo-txns in txn_count, etc.).
        """
        self._current_ledger = ledger_index

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

    def end_batch(self) -> None:
        """Mark a batch as complete (increments ledgers_active)."""
        self._ledgers_active += 1

    # -- Read-only properties --------------------------------------------------

    @property
    def next_sequence(self) -> int:
        return self._next_seq

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def stats(self, *, running: bool = False) -> TxnGeneratorStats:
        """Snapshot of current state. Pass running=True from TxnGenerator."""
        return TxnGeneratorStats(
            running=running,
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
    def pending_summary(self) -> str:
        """Breakdown of pending txns by LLS for logging."""
        by_lls: dict[int, int] = {}
        for ptx in self._pending.values():
            by_lls[ptx.last_ledger_seq] = by_lls.get(ptx.last_ledger_seq, 0) + 1
        return " ".join(f"lls{lls}:{n}" for lls, n in sorted(by_lls.items()))

    @property
    def batch_results_summary(self) -> str:
        """Per-batch engine result summary for logging."""
        return ", ".join(f"{r}:{n}" for r, n in self._results_by_engine.items())


# -- TxnGenerator: async I/O driver -------------------------------------------


class TxnGenerator:
    """Background transaction generator with per-ledger reconciliation.

    Owns two WebSocket connections:
    1. XahauClient for RPC requests (Fee, AccountInfo, SubmitOnly)
    2. Raw websockets for ledger+transactions subscription stream

    All sequence tracking logic is delegated to SubmissionTracker.
    """

    def __init__(
        self,
        ws_url: str,
        config: TxnGeneratorConfig | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._config = config or TxnGeneratorConfig()
        self._tracker = SubmissionTracker(self._config)

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

    @property
    def stats(self) -> TxnGeneratorStats:
        """Snapshot of current generator state."""
        return self._tracker.stats(running=self.running)

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
            server_fee = fee_response.result.get("drops", {}).get("base_fee", "10")
            # Use 4000x base fee to bypass TxQ via tryDirectApply
            self._base_fee = str(max(int(server_fee) * 4000, 40000))
            server_response = await client.request(ServerInfo())
            self._network_id = server_response.result.get("info", {}).get("network_id")

            # Create and fund accounts
            await self._fund_accounts(client)

            # Record initial sequence (post-funding)
            acct_response = await client.request(
                AccountInfoRequest(account=self._genesis_wallet.classic_address)
            )
            initial_seq = acct_response.result["account_data"]["Sequence"]
            self._tracker.reset(initial_seq)

            self._ready_event.set()
            logger.info(
                f"TxnGenerator ready: {self._config.min_txns}-{self._config.max_txns} "
                f"txns/ledger, genesis seq={initial_seq}"
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
                        tx_hash = data.get("transaction", {}).get("hash")
                        if tx_hash:
                            self._tracker.on_validated_txn(tx_hash)
                    elif data.get("type") == "ledgerClosed":
                        ledger_index = data.get("ledger_index", 0)
                        txn_count = data.get("txn_count", 0)

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
                                tx_hash = txn_data.get("transaction", {}).get("hash")
                                if tx_hash:
                                    self._tracker.on_validated_txn(tx_hash)
                            else:
                                # Non-txn message (e.g. next ledgerClosed) —
                                # buffer it for the next iteration.
                                buffered = txn_data
                                break

                        self._tracker.on_ledger_closed(ledger_index)
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
            response = await client.request(SubmitOnly(tx_blob=tx_blob, fail_hard=True))
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

    # -- Internal: submission ----------------------------------------------

    async def _submit_batch(self, client: XahauClient, ledger_index: int) -> None:
        """Submit a batch of random payments for this ledger."""
        assert self._genesis_wallet is not None

        if not self._tracker.should_submit(ledger_index):
            return

        k, lls = self._tracker.prepare_batch(ledger_index)
        destinations = random.choices(self._accounts, k=k)

        batch_results: dict[str, int] = {}
        for dest in destinations:
            payment = Payment(
                account=self._genesis_wallet.classic_address,
                destination=dest.address,
                amount=self._config.amount_drops,
                fee=self._base_fee,
                sequence=self._tracker.next_sequence,
                last_ledger_sequence=lls,
                network_id=self._network_id,
            )
            signed_tx = sign(payment, self._genesis_wallet)
            tx_blob = encode(signed_tx.to_xrpl())

            response = await client.request(SubmitOnly(tx_blob=tx_blob, fail_hard=True))
            result = response.result
            engine_result = result.get("engine_result", "unknown")
            batch_results[engine_result] = batch_results.get(engine_result, 0) + 1

            tx_hash = result.get("tx_json", {}).get("hash")
            action = self._tracker.on_submit_result(engine_result, tx_hash, lls)

            if action == BatchAction.RECOVER:
                await self._recover_sequence(client)
                break
            elif action == BatchAction.BREAK:
                break

        self._tracker.end_batch()
        stats_str = ", ".join(f"{r}:{n}" for r, n in batch_results.items())
        pending_detail = self._tracker.pending_summary

        logger.info(
            f"Ledger {ledger_index}: submitted {k} txns ({stats_str}) "
            f"pending={self._tracker.pending_count} [{pending_detail}]"
        )

    async def _recover_sequence(self, client: XahauClient) -> None:
        """Re-fetch sequence from server on tefPAST_SEQ/tefMAX_LEDGER."""
        assert self._genesis_wallet is not None
        acct_response = await client.request(
            AccountInfoRequest(account=self._genesis_wallet.classic_address)
        )
        server_seq = acct_response.result["account_data"]["Sequence"]
        self._tracker.on_recovery(server_seq)
        logger.warning(f"Sequence recovered from server: {server_seq}")
