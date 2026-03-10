"""Tests for SubmissionTracker state machine."""

from __future__ import annotations

import pytest

from xahaud_scripts.testnet.txn_generator import (
    BatchAction,
    SubmissionTracker,
    TxnGeneratorConfig,
)


@pytest.fixture
def tracker() -> SubmissionTracker:
    """Tracker with default config, reset to initial_seq=100."""
    t = SubmissionTracker(TxnGeneratorConfig())
    t.reset(100)
    return t


# -- reset -----------------------------------------------------------------


def test_reset_sets_sequences():
    t = SubmissionTracker(TxnGeneratorConfig())
    t.reset(42)
    assert t.next_sequence == 42
    assert t.stats().confirmed_sequence == 42
    assert t.stats().pending_count == 0


# -- on_submit_result ------------------------------------------------------


def test_submit_success_tracks_pending(tracker: SubmissionTracker):
    action = tracker.on_submit_result("tesSUCCESS", "hash_a", lls=110)
    assert action == BatchAction.CONTINUE
    assert tracker.next_sequence == 101
    assert tracker.pending_count == 1
    assert tracker.stats().total_submitted == 1


def test_submit_queued_tracks_pending(tracker: SubmissionTracker):
    action = tracker.on_submit_result("terQUEUED", "hash_a", lls=110)
    assert action == BatchAction.CONTINUE
    assert tracker.next_sequence == 101
    assert tracker.pending_count == 1
    assert tracker.stats().total_submitted == 1


def test_submit_queue_full_breaks(tracker: SubmissionTracker):
    action = tracker.on_submit_result("telCAN_NOT_QUEUE_FULL", "hash_a", lls=110)
    assert action == BatchAction.BREAK
    assert tracker.next_sequence == 100  # unchanged
    assert tracker.pending_count == 0
    assert tracker.stats().total_submitted == 0


def test_submit_tel_prefix_breaks(tracker: SubmissionTracker):
    """Any tel* result should break — they're all dead with fail_hard."""
    action = tracker.on_submit_result("telCAN_NOT_QUEUE", "hash_a", lls=110)
    assert action == BatchAction.BREAK
    assert tracker.next_sequence == 100  # unchanged


def test_submit_past_seq_recovers(tracker: SubmissionTracker):
    action = tracker.on_submit_result("tefPAST_SEQ", "hash_a", lls=110)
    assert action == BatchAction.RECOVER
    assert tracker.next_sequence == 100  # unchanged — caller must recover


def test_submit_pre_seq_recovers(tracker: SubmissionTracker):
    action = tracker.on_submit_result("terPRE_SEQ", "hash_a", lls=110)
    assert action == BatchAction.RECOVER


def test_submit_max_ledger_recovers(tracker: SubmissionTracker):
    action = tracker.on_submit_result("tefMAX_LEDGER", "hash_a", lls=110)
    assert action == BatchAction.RECOVER


def test_submit_malformed_skips(tracker: SubmissionTracker):
    action = tracker.on_submit_result("temMALFORMED", "hash_a", lls=110)
    assert action == BatchAction.SKIP
    assert tracker.next_sequence == 100  # unchanged
    assert tracker.pending_count == 0


def test_submit_no_hash_still_tracks_sequence(tracker: SubmissionTracker):
    """tesSUCCESS with no hash — sequence increments but no pending entry."""
    action = tracker.on_submit_result("tesSUCCESS", None, lls=110)
    assert action == BatchAction.CONTINUE
    assert tracker.next_sequence == 101
    assert tracker.pending_count == 0  # no hash to track
    assert tracker.stats().total_submitted == 1


def test_submit_results_tracked_in_engine_stats(tracker: SubmissionTracker):
    tracker.on_submit_result("tesSUCCESS", "h1", lls=110)
    tracker.on_submit_result("tesSUCCESS", "h2", lls=110)
    tracker.on_submit_result("telCAN_NOT_QUEUE_FULL", "h3", lls=110)
    stats = tracker.stats()
    assert stats.results_by_engine == {"tesSUCCESS": 2, "telCAN_NOT_QUEUE_FULL": 1}


# -- on_validated_txn ------------------------------------------------------


def test_validated_tracked_hash(tracker: SubmissionTracker):
    tracker.on_submit_result("tesSUCCESS", "hash_a", lls=110)
    tracker.on_validated_txn("hash_a")
    assert tracker.pending_count == 0
    assert tracker.stats().total_validated == 1


def test_validated_unknown_hash(tracker: SubmissionTracker):
    tracker.on_validated_txn("unknown_hash")
    assert tracker.pending_count == 0
    assert tracker.stats().total_validated == 0  # no-op


# -- reconcile -------------------------------------------------------------


def test_ledger_closed_no_expiry(tracker: SubmissionTracker):
    tracker.on_submit_result("tesSUCCESS", "h1", lls=110)
    tracker.on_submit_result("tesSUCCESS", "h2", lls=110)
    # Reconcile at ledger 108 — LLS 110 not yet passed
    tracker.on_ledger_closed(108)
    assert tracker.stats().total_expired == 0
    assert tracker.pending_count == 2
    # confirmed = initial(100) + validated(0) = 100
    # next = confirmed(100) + pending(2) = 102
    assert tracker.stats().confirmed_sequence == 100
    assert tracker.next_sequence == 102


def test_ledger_closed_with_expiry(tracker: SubmissionTracker):
    tracker.on_submit_result("tesSUCCESS", "h1", lls=105)
    tracker.on_submit_result("tesSUCCESS", "h2", lls=110)
    # Reconcile at ledger 106 — h1 (LLS 105) expired, h2 (LLS 110) still pending
    tracker.on_ledger_closed(106)
    assert tracker.stats().total_expired == 1
    assert tracker.pending_count == 1
    # confirmed = 100 + 0 validated = 100
    # next = 100 + 1 pending = 101
    assert tracker.stats().confirmed_sequence == 100
    assert tracker.next_sequence == 101


def test_ledger_closed_with_validation(tracker: SubmissionTracker):
    tracker.on_submit_result("tesSUCCESS", "h1", lls=110)
    tracker.on_submit_result("tesSUCCESS", "h2", lls=110)
    # h1 validates
    tracker.on_validated_txn("h1")
    tracker.on_ledger_closed(108)
    # confirmed = 100 + 1 validated = 101
    # next = 101 + 1 pending = 102
    assert tracker.stats().confirmed_sequence == 101
    assert tracker.next_sequence == 102
    assert tracker.stats().total_validated == 1
    assert tracker.pending_count == 1


def test_ledger_closed_expiry_uses_strict_less_than(tracker: SubmissionTracker):
    """LLS == ledger_index should NOT expire (grace period for drain lag)."""
    tracker.on_submit_result("tesSUCCESS", "h1", lls=110)
    tracker.on_ledger_closed(110)  # LLS 110 is NOT < 110
    assert tracker.stats().total_expired == 0
    assert tracker.pending_count == 1
    # But reconcile at 111 should expire it
    tracker.on_ledger_closed(111)
    assert tracker.stats().total_expired == 1
    assert tracker.pending_count == 0


# -- on_recovery -----------------------------------------------------------


def test_recovery_syncs_state(tracker: SubmissionTracker):
    tracker.on_submit_result("tesSUCCESS", "h1", lls=110)
    tracker.on_submit_result("tesSUCCESS", "h2", lls=110)
    assert tracker.pending_count == 2
    assert tracker.next_sequence == 102

    # Server says sequence is 105 (3 more than we expected)
    tracker.on_recovery(105)
    assert tracker.pending_count == 0
    assert tracker.next_sequence == 105
    assert tracker.stats().confirmed_sequence == 105
    # total_validated synced: 105 - 100 = 5
    assert tracker.stats().total_validated == 5


def test_ledger_closed_after_recovery_preserves_sequence(tracker: SubmissionTracker):
    """The alternating-ledger bug: reconcile must not revert recovery."""
    tracker.on_submit_result("tesSUCCESS", "h1", lls=110)
    assert tracker.next_sequence == 101

    # Recovery sets seq to 105
    tracker.on_recovery(105)
    assert tracker.next_sequence == 105

    # Next reconcile should NOT revert to 101
    tracker.on_ledger_closed(112)
    assert tracker.next_sequence == 105
    assert tracker.stats().confirmed_sequence == 105


# -- should_submit ---------------------------------------------------------


def test_should_submit_no_start_ledger():
    t = SubmissionTracker(TxnGeneratorConfig(start_ledger=0))
    t.reset(100)
    assert t.should_submit(1) is True
    assert t.should_submit(100) is True


def test_should_submit_with_start_ledger():
    t = SubmissionTracker(TxnGeneratorConfig(start_ledger=10))
    t.reset(100)
    assert t.should_submit(5) is False
    assert t.should_submit(9) is False
    assert t.should_submit(10) is True
    assert t.should_submit(11) is True


# -- prepare_batch ---------------------------------------------------------


def test_prepare_batch_lls_offset():
    t = SubmissionTracker(TxnGeneratorConfig(lls_offset=5))
    t.reset(100)
    _count, lls = t.prepare_batch(200)
    assert lls == 205


def test_prepare_batch_count_range():
    t = SubmissionTracker(TxnGeneratorConfig(min_txns=3, max_txns=7))
    t.reset(100)
    for _ in range(50):
        count, _ = t.prepare_batch(100)
        assert 3 <= count <= 7


# -- end_batch -------------------------------------------------------------


def test_end_batch_increments_ledgers_active(tracker: SubmissionTracker):
    assert tracker.stats().ledgers_active == 0
    tracker.end_batch()
    assert tracker.stats().ledgers_active == 1
    tracker.end_batch()
    assert tracker.stats().ledgers_active == 2


# -- pending_summary -------------------------------------------------------


def test_pending_summary_empty(tracker: SubmissionTracker):
    assert tracker.pending_summary == ""


def test_pending_summary_grouped_by_lls(tracker: SubmissionTracker):
    tracker.on_submit_result("tesSUCCESS", "h1", lls=110)
    tracker.on_submit_result("tesSUCCESS", "h2", lls=110)
    tracker.on_submit_result("tesSUCCESS", "h3", lls=112)
    assert tracker.pending_summary == "lls110:2 lls112:1"


# -- stats -----------------------------------------------------------------


def test_stats_running_flag(tracker: SubmissionTracker):
    assert tracker.stats(running=False).running is False
    assert tracker.stats(running=True).running is True


# -- full cycle integration ------------------------------------------------


def test_full_ledger_cycle(tracker: SubmissionTracker):
    """Submit → validate → reconcile → submit again. Sequences stay correct."""
    # Ledger 10: submit 3 txns
    for i in range(3):
        action = tracker.on_submit_result("tesSUCCESS", f"L10_{i}", lls=13)
        assert action == BatchAction.CONTINUE
    tracker.end_batch()
    assert tracker.next_sequence == 103
    assert tracker.pending_count == 3

    # All 3 validate
    for i in range(3):
        tracker.on_validated_txn(f"L10_{i}")
    assert tracker.pending_count == 0
    assert tracker.stats().total_validated == 3

    # Ledger 11: reconcile
    tracker.on_ledger_closed(11)
    # confirmed = 100 + 3 = 103, next = 103 + 0 = 103
    assert tracker.stats().confirmed_sequence == 103
    assert tracker.next_sequence == 103

    # Ledger 11: submit 2 more
    for i in range(2):
        action = tracker.on_submit_result("tesSUCCESS", f"L11_{i}", lls=14)
        assert action == BatchAction.CONTINUE
    tracker.end_batch()
    assert tracker.next_sequence == 105
    assert tracker.pending_count == 2


def test_alternating_ledger_regression():
    """Reproduce the exact tefPAST_SEQ loop seen in production.

    Scenario (from logs with 50-60 txns/ledger, initial_seq=61):
    - Ledger 8: 33 tesSUCCESS + 10 terQUEUED + telCAN_NOT_QUEUE_FULL (break)
      → 43 pending, next_seq=104
    - Ledger 9: all 43 validate via drain, reconcile sets next_seq=104
      → submit gets tefPAST_SEQ, recover to 105
    - Ledger 10: reconcile should NOT revert to 104 — next_seq stays 105

    The bug was: reconcile computed confirmed_seq = 61 + 43 = 104, but the
    server had advanced to 105. Without syncing total_validated in recovery,
    reconcile kept reverting the sequence.
    """
    t = SubmissionTracker(TxnGeneratorConfig(min_txns=50, max_txns=60, lls_offset=3))
    t.reset(61)

    # -- Ledger 8: submit 44 txns, 43 accepted, 1 queue full --
    for i in range(33):
        action = t.on_submit_result("tesSUCCESS", f"L8_s_{i}", lls=11)
        assert action == BatchAction.CONTINUE
    for i in range(10):
        action = t.on_submit_result("terQUEUED", f"L8_q_{i}", lls=11)
        assert action == BatchAction.CONTINUE
    action = t.on_submit_result("telCAN_NOT_QUEUE_FULL", "L8_full", lls=11)
    assert action == BatchAction.BREAK
    t.end_batch()

    assert t.next_sequence == 104  # 61 + 43
    assert t.pending_count == 43
    assert t.stats().total_submitted == 43

    # -- Ledger 9: all 43 validate, then reconcile --
    for i in range(33):
        t.on_validated_txn(f"L8_s_{i}")
    for i in range(10):
        t.on_validated_txn(f"L8_q_{i}")

    assert t.stats().total_validated == 43
    assert t.pending_count == 0

    t.on_ledger_closed(9)
    # confirmed = 61 + 43 = 104, next = 104 + 0 = 104
    assert t.stats().confirmed_sequence == 104
    assert t.next_sequence == 104

    # Submit at seq 104 → server says tefPAST_SEQ (server seq is 105 because
    # 44 sequences were consumed: 43 tracked + 1 funding txn we didn't account
    # for, or similar). The point is: server is ahead of our tracking.
    action = t.on_submit_result("tefPAST_SEQ", "L9_fail", lls=12)
    assert action == BatchAction.RECOVER

    # Caller fetches server seq = 105 and calls on_recovery
    t.on_recovery(105)
    t.end_batch()

    assert t.next_sequence == 105
    assert t.stats().total_validated == 44  # synced: 105 - 61 = 44

    # -- Ledger 10: reconcile must NOT revert to 104 --
    t.on_ledger_closed(10)
    assert t.next_sequence == 105  # the critical assertion
    assert t.stats().confirmed_sequence == 105

    # Now submit should work from seq 105
    action = t.on_submit_result("tesSUCCESS", "L10_ok", lls=13)
    assert action == BatchAction.CONTINUE
    assert t.next_sequence == 106


def test_mixed_results_batch(tracker: SubmissionTracker):
    """Batch with success, skip, then queue full."""
    a1 = tracker.on_submit_result("tesSUCCESS", "h1", lls=110)
    assert a1 == BatchAction.CONTINUE

    a2 = tracker.on_submit_result("temINVALID", "h2", lls=110)
    assert a2 == BatchAction.SKIP
    # Sequence unchanged after skip
    assert tracker.next_sequence == 101

    a3 = tracker.on_submit_result("tesSUCCESS", "h3", lls=110)
    assert a3 == BatchAction.CONTINUE
    assert tracker.next_sequence == 102

    a4 = tracker.on_submit_result("telCAN_NOT_QUEUE_FULL", "h4", lls=110)
    assert a4 == BatchAction.BREAK

    assert tracker.pending_count == 2
    assert tracker.stats().total_submitted == 2


def test_multiple_recoveries_accumulate():
    """Multiple recovery cycles don't corrupt total_validated."""
    t = SubmissionTracker(TxnGeneratorConfig())
    t.reset(100)

    # First cycle: submit 5, recover to 108
    for i in range(5):
        t.on_submit_result("tesSUCCESS", f"c1_{i}", lls=110)
    t.on_recovery(108)
    assert t.stats().total_validated == 8  # 108 - 100

    # Second cycle: submit 3, recover to 115
    for i in range(3):
        t.on_submit_result("tesSUCCESS", f"c2_{i}", lls=120)
    t.on_recovery(115)
    assert t.stats().total_validated == 15  # 115 - 100
    assert t.next_sequence == 115

    # Reconcile doesn't break it
    t.on_ledger_closed(120)
    assert t.next_sequence == 115
