"""Tests for note_retention.py: notes go a period after they were created.

Run from the repo root: ``python3 -m unittest discover -s retention/tests -t retention``.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, override

from audit_event import AuditWriteError
from note_retention import NoteSweep, NoteSweeper

from tests.synthetic import (
    ALICE,
    BOB,
    NOW,
    SENTINEL,
    FailingStream,
    Store,
    chain_problems,
    close_quietly,
    deletions,
    emitter,
    make_database,
)


class NoteSweepTest(unittest.TestCase):
    """A fresh synthetic database per test."""

    @override
    def setUp(self) -> None:
        """Create the database and a writer for it."""
        self.tmp = Path(tempfile.mkdtemp(prefix="retention-notes-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.database = self.tmp / "webui.db"
        make_database(self.database)
        self.store = Store(self.database)
        self.addCleanup(self.store.close)

    def sweep(self, days: int = 365, stream: io.StringIO | None = None, **kwargs: Any) -> tuple[NoteSweep, io.StringIO]:
        """Run one sweep on a captured stream and check its chain.

        Args:
            days: The retention period.
            stream: The audit stream; a fresh one by default.
            **kwargs: Passed to :class:`NoteSweeper`.

        Returns:
            The result and the stream.
        """
        audit, out = emitter(stream)
        try:
            result = NoteSweeper(audit, self.database, days, pause_s=0, **kwargs).sweep(NOW)
        finally:
            close_quietly(audit)
        return result, out

    def notes(self) -> set[str]:
        """List the notes left.

        Returns:
            Their ids.
        """
        return {str(row[0]) for row in self.store.conn.execute("SELECT id FROM note")}

    def test_note_expires_by_its_creation_in_nanoseconds(self) -> None:
        """Open WebUI times notes in nanoseconds; a note created a period and a day ago goes."""
        old = self.store.note(ALICE, created_days=366)
        result, stream = self.sweep(365)
        self.assertEqual((result.notes, result.owners), (1, 1))
        self.assertNotIn(old, self.notes())
        self.assertEqual(chain_problems(stream), [])

    def test_young_note_stays(self) -> None:
        """A note created within the period stays."""
        young = self.store.note(ALICE, created_days=364)
        result, stream = self.sweep(365)
        self.assertEqual(result.notes, 0)
        self.assertEqual(self.notes(), {young})
        self.assertEqual(deletions(stream), [])

    def test_note_without_a_creation_time_stays(self) -> None:
        """A note without created_at is never deleted."""
        undated = self.store.note(ALICE, created_days=500)
        self.store.conn.execute("UPDATE note SET created_at = NULL WHERE id = ?", (undated,))
        self.sweep(365)
        self.assertEqual(self.notes(), {undated})

    def test_dependents_go_with_it(self) -> None:
        """Its pin, comments with their reactions, notifications and access grants go too; others stay."""
        old = self.store.note(ALICE, created_days=400)
        kept = self.store.note(BOB, created_days=1)
        self.store.note_related(old, ALICE)
        self.store.note_related(kept, BOB)
        result, stream = self.sweep(365)
        for table, where in (
            ("pinned_note", "note_id = ?"),
            ("comment", "resource_type = 'note' AND resource_id = ?"),
            ("notification", "resource_type = 'note' AND resource_id = ?"),
            ("access_grant", "resource_type = 'note' AND resource_id = ?"),
        ):
            self.assertEqual(self.store.count(table, where, (old,)), 0, table)
            self.assertEqual(self.store.count(table, where, (kept,)), 1, table)
        self.assertEqual(self.store.count("comment_reaction"), 1)
        (event,) = deletions(stream)
        self.assertEqual(
            {key: event["criteria"][key] for key in ("pins", "comments", "comment_reactions", "notifications", "grants")},
            {"pins": 1, "comments": 1, "comment_reactions": 1, "notifications": 1, "grants": 1},
        )
        self.assertEqual(result.notes, 1)

    def test_record_names_notes_by_id_only(self) -> None:
        """The record holds the note ids, the owner's account id, the rule; never content or titles."""
        old = self.store.note(ALICE, created_days=400)
        _, stream = self.sweep(365)
        (event,) = deletions(stream)
        self.assertEqual(event["target"], {"kind": "note", "ids": [old], "count": 1})
        self.assertEqual(event["criteria"]["owner"], ALICE)
        self.assertEqual((event["criteria"]["store"], event["criteria"]["period_days"]), ("notes", 365))
        self.assertNotIn(SENTINEL, stream.getvalue())

    def test_failed_record_rolls_back(self) -> None:
        """When the record cannot be written, the note and its dependents stay."""
        old = self.store.note(ALICE, created_days=400)
        self.store.note_related(old, ALICE)
        with self.assertRaises(AuditWriteError):
            self.sweep(365, stream=FailingStream(fail_at=2))
        self.assertEqual(self.notes(), {old})
        self.assertEqual(self.store.count("pinned_note"), 1)

    def test_the_sweep_empties_the_write_ahead_log(self) -> None:
        """After deleting, the log holds no earlier version of the note."""
        self.store.note(ALICE, created_days=400)
        wal = self.database.with_name(f"{self.database.name}-wal")
        result, _ = self.sweep(365)
        self.assertEqual(result.log_emptied, True)
        self.assertEqual(wal.stat().st_size, 0)
        self.assertNotIn(SENTINEL.encode(), self.database.read_bytes())

    def test_missing_database_is_not_created(self) -> None:
        """Before Open WebUI's first start there is nothing to sweep, and nothing is created."""
        missing = self.tmp / "absent" / "webui.db"
        audit, _ = emitter()
        self.addCleanup(close_quietly, audit)
        result = NoteSweeper(audit, missing, 365).sweep(NOW)
        self.assertFalse(result.database)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
