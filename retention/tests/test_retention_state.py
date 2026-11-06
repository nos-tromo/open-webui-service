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
from retention_state import RetentionState, StateRefused, StoreRule

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

    def rule(self, days: int, now: float, notice: int = 30, store: str = "chats") -> StoreRule:
        """Read the rule of a store as a round would, recording a new or changed period.

        Args:
            days: The configured period.
            now: The round's time.
            notice: ``RETENTION_NOTICE_DAYS``.
            store: The store.

        Returns:
            The rule.
        """
        return RetentionState.load(self.path).rule(store, days, notice, now)

    def entry(self, store: str = "chats") -> dict[str, float]:
        return json.loads(self.path.read_text())["stores"][store]

    def test_first_run_records_now(self) -> None:
        """A store seen for the first time takes effect now and deletes nothing for the notice."""
        rule = self.rule(365, NOW)
        self.assertEqual((rule.since, rule.grace_until), (NOW, NOW + 30 * DAY))
        self.assertEqual(self.entry(), {"days": 365, "since": NOW, "grace_until": NOW + 30 * DAY})
        self.assertIsNone(rule.sweep_days(NOW + 30 * DAY - 1))
        self.assertEqual(rule.sweep_days(NOW + 30 * DAY), 365)

    def test_same_period_keeps_its_dates(self) -> None:
        """A restart with the same period keeps when it took effect; other stores keep theirs."""
        self.rule(365, NOW)
        self.rule(365, NOW, store="notes")
        self.assertEqual(self.rule(365, NOW + 10 * DAY).since, NOW)
        self.rule(180, NOW + 40 * DAY)
        self.assertEqual(self.rule(365, NOW + 41 * DAY, store="notes").since, NOW)

    def test_shortening_spares_only_what_falls_due_sooner(self) -> None:
        """A shorter period: the longer one in force still deletes meanwhile; the shorter after the notice."""
        self.rule(365, NOW - 400 * DAY)
        rule = self.rule(30, NOW)
        self.assertEqual(rule.sweep_days(NOW), 365)
        self.assertEqual(rule.sweep_days(NOW + 30 * DAY), 30)

    def test_lengthening_takes_effect_at_once(self) -> None:
        """A longer period never pauses deletion."""
        self.rule(30, NOW - 400 * DAY)
        self.assertEqual(self.rule(365, NOW).sweep_days(NOW), 365)

    def test_changing_back_and_forth_postpones_nothing(self) -> None:
        """365 → 364 → 365 within the notice: 365 still deletes on time."""
        self.rule(365, NOW - 400 * DAY)
        self.rule(364, NOW)
        self.assertEqual(self.rule(365, NOW + DAY).sweep_days(NOW + DAY), 365)

    def test_raising_then_lowering_gives_the_notice(self) -> None:
        """30 → 365 → 30: the 365 days were in force, so they still decide during the new notice."""
        self.rule(30, NOW - 400 * DAY)
        self.rule(365, NOW)
        rule = self.rule(30, NOW + 10 * DAY)
        self.assertEqual(rule.sweep_days(NOW + 10 * DAY), 365)
        self.assertEqual(rule.sweep_days(NOW + 40 * DAY), 30)

    def test_a_change_during_the_first_notice_keeps_its_end(self) -> None:
        """Changing the period before the first notice ended never moves that end later."""
        self.rule(365, NOW)
        rule = self.rule(30, NOW + 20 * DAY)
        self.assertIsNone(rule.sweep_days(NOW + 29 * DAY))
        self.assertEqual(rule.sweep_days(NOW + 30 * DAY), 365)

    def test_a_lower_notice_never_ends_a_running_notice_sooner(self) -> None:
        """Changing the period and lowering the notice together keeps the notice already running."""
        self.rule(365, NOW)
        rule = self.rule(30, NOW + DAY, notice=7)
        self.assertIsNone(rule.sweep_days(NOW + 8 * DAY))
        self.assertEqual(rule.grace_until, NOW + 30 * DAY)

    def test_a_notice_change_alone_moves_nothing(self) -> None:
        """Lowering RETENTION_NOTICE_DAYS during the notice never ends it sooner than announced."""
        self.rule(365, NOW)
        self.assertIsNone(self.rule(365, NOW + 10 * DAY, notice=7).sweep_days(NOW + 10 * DAY))

    def test_an_old_entry_is_upgraded_with_its_end(self) -> None:
        """An entry of the release before is rewritten with its notice's end, so the notice moves nothing."""
        self.path.write_text(json.dumps({"stores": {"chats": {"days": 365, "since": NOW - 5 * DAY}}}), encoding="utf-8")
        self.rule(365, NOW)
        self.assertEqual(self.entry()["grace_until"], NOW + 25 * DAY)
        self.assertIsNone(self.rule(365, NOW + 2 * DAY, notice=1).sweep_days(NOW + 2 * DAY))

    def test_a_state_dated_in_the_future_restarts_the_notice(self) -> None:
        """A state ahead of the clock never blocks deletion for long."""
        self.path.write_text(json.dumps({"stores": {"chats": {"days": 365, "since": NOW + 100 * DAY}}}), encoding="utf-8")
        self.assertEqual(self.rule(365, NOW).grace_until, NOW + 30 * DAY)

    def test_forget_drops_the_store(self) -> None:
        """Switching retention off forgets the store, so switching on again gives a fresh notice."""
        self.rule(365, NOW - 400 * DAY)
        state = RetentionState.load(self.path)
        state.forget("chats")
        self.assertEqual(self.rule(365, NOW).grace_until, NOW + 30 * DAY)

    def test_corrupt_state_is_refused(self) -> None:
        """A file that is not the state, or not readable, refuses instead of restarting the grace."""
        for content in (
            "not json",
            "[]",
            '{"stores": {"chats": {"days": "365", "since": 1}}}',
            '{"stores": 3}',
            '{"stores": {"chats": {"days": 0, "since": 1}}}',
            '{"stores": {"chats": {"days": 30, "since": 100, "grace_until": 50}}}',
            '{"stores": {"chats": {"days": 30, "since": 100, "grace_until": 200, "in_force_days": 365}}}',
            '{"stores": {"chats": {"days": 30, "since": 100, "grace_until": 200, "in_force_days": 0, "in_force_grace": 50}}}',
        ):
            self.path.write_text(content, encoding="utf-8")
            with self.assertRaises(StateRefused, msg=content):
                RetentionState.load(self.path)

    def test_write_is_atomic_and_private(self) -> None:
        """Written to a temporary file in the same directory, then renamed into place, mode 0600."""
        with mock.patch.object(retention_state.os, "replace", wraps=retention_state.os.replace) as replace:
            RetentionState.load(self.path).rule("chats", 365, 30, NOW)
        ((source, target), _) = replace.call_args
        self.assertEqual(Path(target), self.path)
        self.assertEqual(Path(source).parent, self.tmp)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [self.path.name])


if __name__ == "__main__":
    unittest.main()
