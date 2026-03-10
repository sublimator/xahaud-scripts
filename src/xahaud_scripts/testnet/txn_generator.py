"""Reusable transaction generator for xahaud test networks.

Funds N accounts at startup, then round-robins across them as senders.
Each account has its own SubmissionTracker for sequence management, avoiding
per-account TxQ contention. Uses tight LastLedgerSequence and per-ledger
reconciliation for robust tracking.

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
    AccountInfo,
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
    funded_accounts: int | None = None  # defaults to ceil(max_txns/txns_per_account)
    txns_per_account: int = 1  # max txns per sender per batch
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
    sender_count: int
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
    def confirmed_sequence(self) -> int:
        return self._confirmed_seq

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
            sender_count=1,
            pending_count=len(self._pending),
            ledgers_active=self._ledgers_active,
            results_by_engine=dict(self._results_by_engine),
        )

    def pending_by_lls(self) -> dict[int, int]:
        """Pending txn counts grouped by LastLedgerSequence."""
        by_lls: dict[int, int] = {}
        for ptx in self._pending.values():
            by_lls[ptx.last_ledger_seq] = by_lls.get(ptx.last_ledger_seq, 0) + 1
        return by_lls

    @property
    def pending_summary(self) -> str:
        """Breakdown of pending txns by LLS for logging."""
        return " ".join(
            f"lls{lls}:{n}" for lls, n in sorted(self.pending_by_lls().items())
        )

    @property
    def batch_results_summary(self) -> str:
        """Per-batch engine result summary for logging."""
        return ", ".join(f"{r}:{n}" for r, n in self._results_by_engine.items())


# -- TxnGenerator: async I/O driver -------------------------------------------


@dataclass
class _Sender:
    """A funded account used as a transaction sender."""

    info: AccountInfo
    tracker: SubmissionTracker


class TxnGenerator:
    """Background transaction generator using round-robin senders.

    Funds N accounts at startup, then round-robins across them as senders.
    Each account has its own SubmissionTracker for sequence management,
    avoiding per-account TxQ contention.

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

        # Senders (funded accounts with per-account trackers)
        self._senders: list[_Sender] = []
        self._rr_idx: int = 0
        self._genesis_wallet: Wallet | None = None

        # Network params (fetched once at startup)
        self._network_id: int | None = None
        self._base_fee: str = "10"

        # Batch-level stats (not per-account)
        self._current_ledger: int = 0
        self._ledgers_active: int = 0

    @property
    def stats(self) -> TxnGeneratorStats:
        """Aggregate stats across all sender trackers."""
        total_submitted = 0
        total_validated = 0
        total_expired = 0
        pending_count = 0
        results_by_engine: dict[str, int] = {}

        for s in self._senders:
            st = s.tracker.stats()
            total_submitted += st.total_submitted
            total_validated += st.total_validated
            total_expired += st.total_expired
            pending_count += st.pending_count
            for r, n in st.results_by_engine.items():
                results_by_engine[r] = results_by_engine.get(r, 0) + n

        return TxnGeneratorStats(
            running=self.running,
            current_ledger=self._current_ledger,
            total_submitted=total_submitted,
            total_validated=total_validated,
            total_expired=total_expired,
            sender_count=len(self._senders),
            pending_count=pending_count,
            ledgers_active=self._ledgers_active,
            results_by_engine=results_by_engine,
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
            server_fee = fee_response.result.get("drops", {}).get("base_fee", "10")
            # Use 4000x base fee to bypass TxQ via tryDirectApply
            self._base_fee = str(max(int(server_fee) * 4000, 40000))
            server_response = await client.request(ServerInfo())
            self._network_id = server_response.result.get("info", {}).get("network_id")

            # Create, fund, and initialize senders
            await self._fund_accounts(client)

            self._ready_event.set()
            n = len(self._senders)
            tpa = self._config.txns_per_account
            logger.info(
                f"TxnGenerator ready: {self._config.min_txns}-{self._config.max_txns} "
                f"txns/ledger, {n} senders ({tpa} txn/account)"
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
                            for s in self._senders:
                                s.tracker.on_validated_txn(tx_hash)
                    elif data.get("type") == "ledgerClosed":
                        ledger_index = data.get("ledger_index", 0)
                        txn_count = data.get("txn_count", 0)

                        # Drain validated-txn messages for this ledger.
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
                                    for s in self._senders:
                                        s.tracker.on_validated_txn(tx_hash)
                            else:
                                buffered = txn_data
                                break

                        self._current_ledger = ledger_index
                        for s in self._senders:
                            s.tracker.on_ledger_closed(ledger_index)
                        await self._submit_batch(client, ledger_index)
            finally:
                await ws_raw.close()

    # -- Internal: funding -------------------------------------------------

    async def _fund_accounts(self, client: XahauClient) -> None:
        """Create deterministic accounts, fund from genesis, init trackers."""
        assert self._genesis_wallet is not None
        cfg = self._config
        count = cfg.funded_accounts or (
            (cfg.max_txns + cfg.txns_per_account - 1) // cfg.txns_per_account
        )
        accounts = [create_account_info(f"txgen-{i}") for i in range(count)]

        # Get current state
        acct_response = await client.request(
            AccountInfoRequest(account=self._genesis_wallet.classic_address)
        )
        genesis_seq = acct_response.result["account_data"]["Sequence"]
        ledger_response = await client.request(Ledger())
        current_ledger = get_ledger_index(ledger_response.result)

        # Submit funding txns in batches (TxQ can only hold ~90 per ledger)
        logger.info(f"Funding {count} accounts from genesis...")
        pending_hashes: set[str] = set()
        fund_amount = str(cfg.fund_amount_xah * 1_000_000)
        lls = current_ledger + 20
        remaining = list(accounts)

        ws_fund = await websockets.connect(self._ws_url, open_timeout=10)
        try:
            await ws_fund.send(
                json.dumps(
                    {
                        "id": 2,
                        "command": "subscribe",
                        "streams": ["transactions", "ledger"],
                    }
                )
            )
            await ws_fund.recv()  # subscription response

            while remaining:
                # Submit as many as the TxQ will accept
                batch_submitted = 0
                retry_from: list[Any] = []
                for acct in remaining:
                    payment = Payment(
                        account=self._genesis_wallet.classic_address,
                        destination=acct.address,
                        amount=fund_amount,
                        fee=self._base_fee,
                        sequence=genesis_seq,
                        last_ledger_sequence=lls,
                        network_id=self._network_id,
                    )
                    signed_tx = sign(payment, self._genesis_wallet)
                    tx_blob = encode(signed_tx.to_xrpl())
                    response = await client.request(
                        SubmitOnly(tx_blob=tx_blob, fail_hard=True)
                    )
                    result = response.result
                    engine_result = result.get("engine_result", "???")
                    tx_hash = result.get("tx_json", {}).get("hash")

                    if engine_result in ("tesSUCCESS", "terQUEUED"):
                        if tx_hash:
                            pending_hashes.add(tx_hash)
                        genesis_seq += 1
                        batch_submitted += 1
                    elif (
                        engine_result.startswith("tel") or engine_result == "terPRE_SEQ"
                    ):
                        # TxQ full — queue the rest for the next ledger
                        retry_from.append(acct)
                    else:
                        logger.warning(f"  Fund {acct.name}: {engine_result}")
                        genesis_seq += 1
                        batch_submitted += 1

                remaining = retry_from
                if remaining:
                    logger.info(
                        f"  Funded batch of {batch_submitted}, "
                        f"waiting for ledger close ({len(remaining)} remaining)..."
                    )
                    # Wait for a ledgerClosed, draining validated txns along the way
                    while True:
                        message = await ws_fund.recv()
                        data = json.loads(message)
                        if data.get("type") == "transaction" and data.get("validated"):
                            tx_hash = data.get("transaction", {}).get("hash")
                            if tx_hash:
                                pending_hashes.discard(tx_hash)
                        elif data.get("type") == "ledgerClosed":
                            break

                    # Re-fetch genesis sequence and bump LLS
                    acct_response = await client.request(
                        AccountInfoRequest(account=self._genesis_wallet.classic_address)
                    )
                    genesis_seq = acct_response.result["account_data"]["Sequence"]
                    ledger_response = await client.request(Ledger())
                    current_ledger = get_ledger_index(ledger_response.result)
                    lls = current_ledger + 20

            # Wait for all remaining funding txns to validate
            total = count
            funded = total - len(pending_hashes)
            if pending_hashes:
                logger.info(
                    f"Waiting for {len(pending_hashes)} funding txns to validate..."
                )
                while pending_hashes:
                    message = await ws_fund.recv()
                    data = json.loads(message)
                    if data.get("type") == "transaction" and data.get("validated"):
                        tx_hash = data.get("transaction", {}).get("hash")
                        if tx_hash and tx_hash in pending_hashes:
                            pending_hashes.discard(tx_hash)
                            funded += 1
                            logger.info(f"  Funded ({funded}/{total})")
        finally:
            await ws_fund.close()

        logger.info("All accounts funded")

        # Query starting sequences in parallel batches (accounts funded in
        # different ledgers have different initial sequences because Xahau
        # sets initial seq = parent ledger close time in Ripple epoch).
        batch_size = 20
        sequences: dict[str, int] = {}
        for i in range(0, len(accounts), batch_size):
            batch = accounts[i : i + batch_size]
            responses = await asyncio.gather(
                *(client.request(AccountInfoRequest(account=a.address)) for a in batch)
            )
            for acct, resp in zip(batch, responses, strict=True):
                sequences[acct.address] = resp.result["account_data"]["Sequence"]

        unique_seqs = sorted(set(sequences.values()))
        if len(unique_seqs) == 1:
            logger.info(f"Sender starting sequence: {unique_seqs[0]}")
        else:
            logger.info(f"Sender starting sequences: {unique_seqs}")

        self._senders = []
        for acct in accounts:
            tracker = SubmissionTracker(cfg)
            tracker.reset(sequences[acct.address])
            self._senders.append(_Sender(info=acct, tracker=tracker))

    # -- Internal: submission ----------------------------------------------

    def _should_submit(self, ledger_index: int) -> bool:
        """Whether to submit transactions for this ledger."""
        sl = self._config.start_ledger
        return not (sl > 0 and ledger_index < sl)

    def _pending_summary(self) -> str:
        """Aggregate pending breakdown across all senders."""
        by_lls: dict[int, int] = {}
        for s in self._senders:
            for lls, n in s.tracker.pending_by_lls().items():
                by_lls[lls] = by_lls.get(lls, 0) + n
        return " ".join(f"lls{lls}:{n}" for lls, n in sorted(by_lls.items()))

    def _total_pending(self) -> int:
        return sum(s.tracker.pending_count for s in self._senders)

    async def _submit_batch(self, client: XahauClient, ledger_index: int) -> None:
        """Submit a batch of round-robin payments for this ledger."""
        if not self._should_submit(ledger_index):
            return

        cfg = self._config
        k = random.randint(cfg.min_txns, cfg.max_txns)
        lls = ledger_index + cfg.lls_offset

        batch_results: dict[str, int] = {}
        submitted = 0
        skip_senders: set[int] = set()
        batch_sends: dict[int, int] = {}
        exhausted = 0

        while submitted < k:
            idx = self._rr_idx % len(self._senders)
            self._rr_idx += 1

            if idx in skip_senders:
                exhausted += 1
                if exhausted >= len(self._senders):
                    break
                continue

            if batch_sends.get(idx, 0) >= cfg.txns_per_account:
                skip_senders.add(idx)
                exhausted += 1
                if exhausted >= len(self._senders):
                    break
                continue

            exhausted = 0
            sender = self._senders[idx]

            # Pick destination != sender
            dest = sender
            while dest is sender:
                dest = random.choice(self._senders)

            payment = Payment(
                account=sender.info.address,
                destination=dest.info.address,
                amount=cfg.amount_drops,
                fee=self._base_fee,
                sequence=sender.tracker.next_sequence,
                last_ledger_sequence=lls,
                network_id=self._network_id,
            )
            signed_tx = sign(payment, sender.info.wallet)
            tx_blob = encode(signed_tx.to_xrpl())

            response = await client.request(SubmitOnly(tx_blob=tx_blob))
            result = response.result
            engine_result = result.get("engine_result", "unknown")
            batch_results[engine_result] = batch_results.get(engine_result, 0) + 1

            tx_hash = result.get("tx_json", {}).get("hash")
            action = sender.tracker.on_submit_result(engine_result, tx_hash, lls)

            if action == BatchAction.CONTINUE:
                batch_sends[idx] = batch_sends.get(idx, 0) + 1
                submitted += 1
            elif action == BatchAction.RECOVER:
                await self._recover_account(client, sender)
                skip_senders.add(idx)
            elif action == BatchAction.BREAK:
                skip_senders.add(idx)
            # SKIP: don't count, don't skip sender, try next

        self._ledgers_active += 1
        stats_str = ", ".join(f"{r}:{n}" for r, n in batch_results.items())

        logger.info(
            f"Ledger {ledger_index}: submitted {submitted} txns ({stats_str}) "
            f"pending={self._total_pending()} [{self._pending_summary()}]"
        )

    async def _recover_account(self, client: XahauClient, sender: _Sender) -> None:
        """Re-fetch sequence from server for a specific account."""
        acct_response = await client.request(
            AccountInfoRequest(account=sender.info.address)
        )
        server_seq = acct_response.result["account_data"]["Sequence"]
        sender.tracker.on_recovery(server_seq)
        logger.warning(f"Sequence recovered for {sender.info.name}: {server_seq}")
