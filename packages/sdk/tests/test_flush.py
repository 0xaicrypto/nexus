"""
Regression tests for FlushPolicy, WriteAheadLog, and FlushBuffer.

Covers:
  - WAL append / read_all / truncate cycle
  - WAL corrupt line recovery
  - WAL fsync durability (file exists on disk after append)
  - FlushBuffer event-count trigger
  - FlushBuffer time trigger
  - FlushBuffer force_flush
  - FlushBuffer close flushes remaining events
  - FlushBuffer retains events on flush failure
  - FlushPolicy preset factories
  - FlushBuffer + WAL integration (WAL truncated on flush)
  - WAL crash recovery via FlushBuffer.recover_from_wal()
"""

import json
import os
import time

import pytest

from nexus_core.flush import FlushPolicy, WriteAheadLog, FlushBuffer


# ── WriteAheadLog ───────────────────────────────────────────────────


class TestWriteAheadLog:

    def test_append_and_read_all(self, tmp_state_dir):
        wal = WriteAheadLog(tmp_state_dir, "agent-1")
        wal.append({"event": 1})
        wal.append({"event": 2})
        entries = wal.read_all()
        assert len(entries) == 2
        assert entries[0]["event"] == 1
        assert entries[1]["event"] == 2
        wal.close()

    def test_truncate_clears_entries(self, tmp_state_dir):
        wal = WriteAheadLog(tmp_state_dir, "agent-1")
        wal.append({"event": 1})
        wal.truncate()
        entries = wal.read_all()
        assert entries == []

    def test_read_all_on_empty(self, tmp_state_dir):
        wal = WriteAheadLog(tmp_state_dir, "agent-1")
        assert wal.read_all() == []

    def test_corrupt_line_skipped(self, tmp_state_dir):
        """Corrupt WAL entries should be skipped, not crash recovery."""
        wal = WriteAheadLog(tmp_state_dir, "agent-1")
        wal.append({"event": 1})
        wal.close()

        # Inject a corrupt line
        wal_path = os.path.join(tmp_state_dir, "agent-1.wal")
        with open(wal_path, "a") as f:
            f.write("THIS IS NOT JSON\n")
            f.write(json.dumps({"event": 2}) + "\n")

        entries = wal.read_all()
        assert len(entries) == 2
        assert entries[0]["event"] == 1
        assert entries[1]["event"] == 2

    def test_size_property(self, tmp_state_dir):
        wal = WriteAheadLog(tmp_state_dir, "agent-1")
        assert wal.size == 0
        wal.append({"event": 1})
        wal.append({"event": 2})
        assert wal.size == 2
        wal.truncate()
        assert wal.size == 0
        wal.close()

    def test_file_exists_after_append(self, tmp_state_dir):
        """WAL file should be fsynced to disk."""
        wal = WriteAheadLog(tmp_state_dir, "agent-1")
        wal.append({"event": 1})
        wal_path = os.path.join(tmp_state_dir, "agent-1.wal")
        assert os.path.exists(wal_path)
        assert os.path.getsize(wal_path) > 0
        wal.close()

    def test_sanitizes_agent_id(self, tmp_state_dir):
        """Agent IDs with path separators should be sanitized."""
        wal = WriteAheadLog(tmp_state_dir, "ns/agent/1")
        wal.append({"event": 1})
        # Should not create subdirectories
        assert os.path.exists(os.path.join(tmp_state_dir, "ns_agent_1.wal"))
        wal.close()


# ── FlushPolicy Presets ─────────────────────────────────────────────


class TestFlushPolicy:

    def test_balanced_defaults(self):
        p = FlushPolicy.balanced()
        assert p.every_n_events == 5
        assert p.interval_seconds == 30.0
        assert p.wal_enabled is True

    def test_sync_every(self):
        p = FlushPolicy.sync_every()
        assert p.every_n_events == 1
        assert p.interval_seconds == 0
        assert p.wal_enabled is False

    def test_manual(self):
        p = FlushPolicy.manual()
        assert p.every_n_events == 0
        assert p.interval_seconds == 0

    def test_aggressive(self):
        p = FlushPolicy.aggressive()
        assert p.every_n_events == 20
        assert p.interval_seconds == 120


# ── FlushBuffer ─────────────────────────────────────────────────────


class TestFlushBuffer:

    def test_event_count_trigger(self):
        """Buffer should flush when event count reaches threshold."""
        flushed_events = []

        def on_flush(events):
            flushed_events.extend(events)

        policy = FlushPolicy(every_n_events=3, interval_seconds=0, wal_enabled=False)
        buf = FlushBuffer(policy=policy, on_flush=on_flush)

        assert buf.append({"e": 1}) is False
        assert buf.append({"e": 2}) is False
        assert buf.append({"e": 3}) is True  # triggers flush
        assert len(flushed_events) == 3
        assert buf.pending_count == 0
        assert buf.total_flushed == 3

    def test_force_flush(self):
        """force_flush should flush all buffered events immediately."""
        flushed = []
        policy = FlushPolicy(every_n_events=0, interval_seconds=0, wal_enabled=False)
        buf = FlushBuffer(policy=policy, on_flush=lambda es: flushed.extend(es))

        buf.append({"e": 1})
        buf.append({"e": 2})
        assert buf.pending_count == 2

        count = buf.force_flush()
        assert count == 2
        assert len(flushed) == 2
        assert buf.pending_count == 0

    def test_force_flush_empty(self):
        """force_flush on empty buffer should return 0."""
        policy = FlushPolicy(every_n_events=0, interval_seconds=0, wal_enabled=False)
        buf = FlushBuffer(policy=policy, on_flush=lambda es: None)
        assert buf.force_flush() == 0

    def test_close_flushes_remaining(self):
        """close() should flush remaining events when sync_on_close=True."""
        flushed = []
        policy = FlushPolicy(every_n_events=0, interval_seconds=0,
                             sync_on_close=True, wal_enabled=False)
        buf = FlushBuffer(policy=policy, on_flush=lambda es: flushed.extend(es))

        buf.append({"e": 1})
        buf.close()
        assert len(flushed) == 1

    def test_close_no_flush_when_disabled(self):
        """close() should NOT flush when sync_on_close=False."""
        flushed = []
        policy = FlushPolicy(every_n_events=0, interval_seconds=0,
                             sync_on_close=False, wal_enabled=False)
        buf = FlushBuffer(policy=policy, on_flush=lambda es: flushed.extend(es))

        buf.append({"e": 1})
        buf.close()
        assert len(flushed) == 0

    def test_flush_failure_retains_events(self):
        """On flush callback error, events should stay in buffer."""
        def failing_flush(events):
            raise RuntimeError("storage down")

        policy = FlushPolicy(every_n_events=2, interval_seconds=0, wal_enabled=False)
        buf = FlushBuffer(policy=policy, on_flush=failing_flush)

        buf.append({"e": 1})
        with pytest.raises(RuntimeError, match="storage down"):
            buf.append({"e": 2})

        # Events should still be in buffer for retry
        assert buf.pending_count == 2

    def test_wal_integration(self, tmp_state_dir):
        """WAL should be written on append and truncated on flush."""
        flushed = []
        policy = FlushPolicy(every_n_events=3, wal_enabled=True, wal_dir=tmp_state_dir)
        wal = WriteAheadLog(tmp_state_dir, "agent-1")
        buf = FlushBuffer(policy=policy, on_flush=lambda es: flushed.extend(es), wal=wal)

        buf.append({"e": 1})
        buf.append({"e": 2})
        assert wal.size == 2  # WAL has 2 entries

        buf.append({"e": 3})  # triggers flush
        assert len(flushed) == 3
        assert wal.size == 0  # WAL truncated after flush

        buf.close()

    def test_wal_crash_recovery(self, tmp_state_dir):
        """Simulate crash: WAL entries should be recoverable."""
        policy = FlushPolicy(every_n_events=10, wal_enabled=True, wal_dir=tmp_state_dir)
        wal = WriteAheadLog(tmp_state_dir, "agent-1")
        buf = FlushBuffer(policy=policy, on_flush=lambda es: None, wal=wal)

        buf.append({"e": 1})
        buf.append({"e": 2})
        # Simulate crash — don't call close()
        wal.close()

        # New runtime: recover from WAL
        wal2 = WriteAheadLog(tmp_state_dir, "agent-1")
        buf2 = FlushBuffer(policy=policy, on_flush=lambda es: None, wal=wal2)
        recovered = buf2.recover_from_wal()

        assert len(recovered) == 2
        assert recovered[0]["e"] == 1
        assert recovered[1]["e"] == 2
        buf2.close()

    def test_policy_change_at_runtime(self):
        """Changing flush policy at runtime should take effect."""
        flushed = []
        policy = FlushPolicy(every_n_events=10, interval_seconds=0, wal_enabled=False)
        buf = FlushBuffer(policy=policy, on_flush=lambda es: flushed.extend(es))

        buf.append({"e": 1})
        buf.append({"e": 2})
        assert len(flushed) == 0  # threshold is 10

        # Lower threshold
        buf.policy = FlushPolicy(every_n_events=3, interval_seconds=0, wal_enabled=False)
        buf.append({"e": 3})
        assert len(flushed) == 3  # now triggers at 3

    def test_time_trigger(self):
        """Time-based trigger should flush when interval exceeded."""
        flushed = []
        # 0.1 second interval for testing
        policy = FlushPolicy(every_n_events=0, interval_seconds=0.1, wal_enabled=False)
        buf = FlushBuffer(policy=policy, on_flush=lambda es: flushed.extend(es))

        buf.append({"e": 1})
        assert buf.check_time_trigger() is False  # too soon

        # Backdate last_flush_time to simulate elapsed time
        buf._last_flush_time = time.time() - 1.0
        assert buf.check_time_trigger() is True
        assert len(flushed) == 1
