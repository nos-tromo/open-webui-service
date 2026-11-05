"""Tests for retention_state.py: when a retention period took effect.

Run from the repo root: ``python3 -m unittest discover -s retention/tests -t retention``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

import retention_state
from retention_state import RetentionState, StateRefused, deletion_allowed

DAY = 86_400
NOW = 1_772_323_200.0


class RetentionStateTest(unittest.TestCase):
    """The state file on Open WebUI's data volume."""

    @override
    def setUp(self) -> None:
        """Point the state at a fresh directory."""
        self.tmp = Path(tempfile.mkdtemp(prefix="retention-state-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / ".retention-state.json"

    def test_first_run_records_now(self) -> None:
        """A store seen for the first time takes effect now, and the file says so."""
        since = RetentionState.load(self.path).effective_since("chats", 365, NOW)
        self.assertEqual(since, NOW)
        self.assertEqual(json.loads(self.path.read_text()), {"stores": {"chats": {"days": 365, "since": NOW}}})

    def test_same_period_keeps_since(self) -> None:
        """A restart with the same period keeps the time it took effect."""
        RetentionState.load(self.path).effective_since("chats", 365, NOW)
        since = RetentionState.load(self.path).effective_since("chats", 365, NOW + 10 * DAY)
        self.assertEqual(since, NOW)

    def test_changed_period_restarts_since(self) -> None:
        """A new period takes effect when it is first seen; other stores keep theirs."""
        state = RetentionState.load(self.path)
        state.effective_since("chats", 365, NOW)
        state.effective_since("notes", 365, NOW)
        since = RetentionState.load(self.path).effective_since("chats", 180, NOW + 5 * DAY)
        self.assertEqual(since, NOW + 5 * DAY)
        self.assertEqual(RetentionState.load(self.path).effective_since("notes", 365, NOW + 6 * DAY), NOW)

    def test_deletion_waits_for_the_notice(self) -> None:
        """Nothing is deleted until the notice period after it took effect has passed."""
        self.assertFalse(deletion_allowed(since=0, notice_days=30, now=30 * DAY - 1))
        self.assertTrue(deletion_allowed(since=0, notice_days=30, now=30 * DAY))

    def test_corrupt_state_is_refused(self) -> None:
        """A file that is not the state, or not readable, refuses instead of restarting the grace."""
        for content in ("not json", "[]", '{"stores": {"chats": {"days": "365", "since": 1}}}', '{"stores": 3}'):
            self.path.write_text(content, encoding="utf-8")
            with self.assertRaises(StateRefused, msg=content):
                RetentionState.load(self.path)

    def test_write_is_atomic_and_private(self) -> None:
        """Written to a temporary file in the same directory, then renamed into place, mode 0600."""
        with mock.patch.object(retention_state.os, "replace", wraps=retention_state.os.replace) as replace:
            RetentionState.load(self.path).effective_since("chats", 365, NOW)
        ((source, target), _) = replace.call_args
        self.assertEqual(Path(target), self.path)
        self.assertEqual(Path(source).parent, self.tmp)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [self.path.name])


if __name__ == "__main__":
    unittest.main()
