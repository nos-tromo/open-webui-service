"""Tests for chat_retention.py against synthetic databases with the pinned schema.

Run from the repo root: ``python3 -m unittest discover -s retention/tests -t retention``.
"""

from __future__ import annotations

import io
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, override
from unittest import mock

import chat_retention
from audit_event import AuditWriteError
from chat_retention import ChatSweeper, SchemaRefused, open_database, preview, schema_problems

from tests.synthetic import (
    ALICE,
    BOB,
    DAY,
    NOW,
    SENTINEL,
    FailingStream,
    Store,
    chain_problems,
    close_quietly,
    deletions,
    emitter,
    events,
    make_database,
)


class Workspace(unittest.TestCase):
    """A fresh synthetic database per test."""

    @override
    def setUp(self) -> None:
        """Create the database and a writer for it."""
        self.tmp = Path(tempfile.mkdtemp(prefix="retention-chats-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.database = self.tmp / "webui.db"
        make_database(self.database)
        self.store = Store(self.database)
        self.addCleanup(self.store.close)

    def sweep(self, days: int = 30, **kwargs: Any) -> tuple[chat_retention.ChatSweep, io.StringIO]:
        """Run one sweep on a captured stream and check its chain.

        Args:
            days: The retention period.
            **kwargs: Passed to :class:`ChatSweeper`.

        Returns:
            The result and the stream.
        """
        audit, stream = emitter()
        result = ChatSweeper(audit, self.database, days, pause_s=0, **kwargs).sweep(NOW)
        audit.close()
        self.assertEqual(chain_problems(stream), [])
        return result, stream


class SchemaGuardTest(Workspace):
    """Chat retention runs only on the schema it was verified against."""

    def problems(self) -> list[str]:
        """Check the workspace database.

        Returns:
            The guard's findings.
        """
        conn = open_database(self.database, readonly=True)
        try:
            return schema_problems(conn)
        finally:
            conn.close()

    def test_pinned_schema_is_verified(self) -> None:
        """The pinned image's own schema passes."""
        self.assertEqual(self.problems(), [])

    def test_another_revision_is_refused(self) -> None:
        """An image bump that migrated the database stops chat retention."""
        self.store.conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        self.assertEqual(len(self.problems()), 1)
        self.assertIn("schema revision ffffffffffff", self.problems()[0])

    def test_missing_revision_is_refused(self) -> None:
        """A database without a revision is not one this release knows."""
        self.store.conn.execute("DELETE FROM alembic_version")
        self.assertIn("schema revision none", self.problems()[0])

    def test_missing_column_is_refused(self) -> None:
        """Every column a statement relies on must exist."""
        self.store.conn.execute("ALTER TABLE feedback DROP COLUMN meta")
        self.assertIn("table feedback lacks column meta", self.problems())

    def test_missing_table_is_refused(self) -> None:
        """Every table a statement relies on must exist."""
        self.store.conn.execute("DROP TABLE chatidtag")
        self.assertIn("table chatidtag is missing", self.problems())

    def test_new_table_with_chat_ids_is_refused(self) -> None:
        """A table that refers to chats by id would keep data of deleted chats."""
        self.store.conn.execute("CREATE TABLE chat_bookmark (id TEXT PRIMARY KEY, chat_id TEXT)")
        self.assertIn("table chat_bookmark refers to chats and is not cleaned up with them", self.problems())

    def test_new_foreign_key_to_chats_is_refused(self) -> None:
        """So would a table that points at chats through another column."""
        self.store.conn.execute("CREATE TABLE chat_pin (id TEXT PRIMARY KEY, pinned TEXT REFERENCES chat (id))")
        self.assertIn("table chat_pin refers to chats and is not cleaned up with them", self.problems())

    def test_refused_sweep_deletes_and_records_nothing(self) -> None:
        """A refused schema raises before anything is deleted."""
        self.store.chat(ALICE, age_days=400)
        self.store.conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        before = self.store.rows()
        audit, stream = emitter()
        with self.assertRaises(SchemaRefused) as caught:
            ChatSweeper(audit, self.database, 30, pause_s=0).sweep(NOW)
        audit.close()
        self.assertIn("schema revision", caught.exception.problems[0])
        self.assertEqual(self.store.rows(), before)
        self.assertEqual(deletions(stream), [])
        self.assertEqual(chain_problems(stream), [])

    def test_migration_during_a_sweep_is_caught_inside_the_transaction(self) -> None:
        """The guard runs again under the write lock, after the plan was made."""
        self.store.chat(ALICE, age_days=400)
        before = self.store.rows()
        real_plan = chat_retention.plan_owner

        def plan_then_migrate(conn: sqlite3.Connection, owner: str | None, cutoff: int) -> list[Any]:
            families = real_plan(conn, owner, cutoff)
            self.store.conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
            return families

        audit, stream = emitter()
        with mock.patch.object(chat_retention, "plan_owner", plan_then_migrate), self.assertRaises(SchemaRefused):
            ChatSweeper(audit, self.database, 30, pause_s=0).sweep(NOW)
        audit.close()
        self.store.conn.execute("UPDATE alembic_version SET version_num = 'e42c8f710a36'")
        self.assertEqual(self.store.rows(), before)
        self.assertEqual(deletions(stream), [])


class SweepTest(Workspace):
    """What a sweep deletes, keeps and records."""

    def test_expired_chat_goes_with_everything_that_belongs_to_it(self) -> None:
        """Messages, shares, grants, links, comments, ratings and unused tags go; the rest stays."""
        expired = self.store.chat(ALICE, age_days=45, tags=["only_here", "shared_tag"])
        for _ in range(3):
            self.store.message(expired, ALICE)
        self.store.related(expired, ALICE)
        kept = self.store.chat(ALICE, age_days=5, tags=["shared_tag"])
        self.store.message(kept, ALICE)
        self.store.related(kept, ALICE)
        for tag in ("only_here", "shared_tag"):
            self.store.tag(ALICE, tag)

        result, stream = self.sweep(30)

        self.assertEqual((result.chats, result.owners, result.batches, result.complete), (1, 1, 1, True))
        self.assertEqual(self.store.chat_ids(), {kept})
        for table, column in (
            ("chat_message", "chat_id"),
            ("shared_chat", "chat_id"),
            ("chat_file", "chat_id"),
            ("chatidtag", "chat_id"),
        ):
            self.assertEqual(self.store.count(table, f"{column} = ?", (expired,)), 0, table)
            self.assertEqual(self.store.count(table, f"{column} = ?", (kept,)), 1, table)
        self.assertEqual(self.store.count("access_grant", "resource_id = ?", (expired,)), 0)
        self.assertEqual(self.store.count("comment", "resource_id = ?", (expired,)), 0)
        self.assertEqual(self.store.count("comment_reaction"), 1)
        self.assertEqual(self.store.count("notification", "resource_id = ?", (expired,)), 0)
        self.assertEqual(self.store.count("feedback", "json_extract(meta, '$.chat_id') = ?", (expired,)), 0)
        self.assertEqual(self.store.count("feedback"), 1)
        self.assertEqual(self.store.count("file"), 2, "uploaded files belong to their owner and stay")
        self.assertEqual(self.store.count("automation_run"), 2, "automation runs keep their record")
        self.assertEqual(self.store.count("automation_run", "chat_id IS NULL"), 1)
        self.assertEqual({str(row[0]) for row in self.store.conn.execute("SELECT id FROM tag")}, {"shared_tag"})

        (event,) = deletions(stream)
        self.assertEqual((event["operation"], event["outcome"]), ("retention.sweep", "ok"))
        self.assertEqual(event["actor"], {"user": None, "via": "system"})
        self.assertEqual(event["target"], {"kind": "chat", "ids": [expired], "count": 1})
        self.assertEqual(
            event["criteria"],
            {
                "store": "chats",
                "period_days": 30,
                "cutoff": "2026-08-21T12:00:00Z",
                "owner": ALICE,
                "internal_chats": 0,
                "messages": 3,
                "shares": 1,
                "share_grants": 1,
                "file_links": 1,
                "comment_reactions": 1,
                "comments": 1,
                "notifications": 1,
                "feedback": 1,
                "legacy_tag_links": 1,
                "automation_runs_unlinked": 1,
                "tags": 1,
            },
        )
        self.assertNotIn(SENTINEL, stream.getvalue())
        self.assertNotIn("only_here", stream.getvalue(), "tag names describe content and are never recorded")
        self.assertNotIn("alice@example.org", stream.getvalue(), "the owner is named by account id only")

    def test_age_is_the_last_change(self) -> None:
        """updated_at decides; created_at stands in when it is missing; neither means kept."""
        boundary = self.store.chat(ALICE, age_days=30)
        old_but_changed = self.store.chat(ALICE, age_days=1, created_days=400)
        undated_old = self.store.chat(ALICE, age_days=None, created_days=40)
        undated = self.store.chat(ALICE, age_days=None, created_days=None)
        expired = self.store.chat(ALICE, age_days=30.001)
        self.sweep(30)
        self.assertEqual(self.store.chat_ids(), {boundary, old_but_changed, undated})
        self.assertNotIn(undated_old, self.store.chat_ids())
        self.assertNotIn(expired, self.store.chat_ids())

    def test_one_record_per_owner(self) -> None:
        """Each record names the owner by Open WebUI's account id only, never by address or name."""
        alice = self.store.chat(ALICE, age_days=60)
        bob = self.store.chat(BOB, age_days=60)
        nobody = self.store.chat(None, age_days=60)
        result, stream = self.sweep(30)
        self.assertEqual((result.chats, result.owners), (3, 3))
        by_owner = {event["criteria"]["owner"]: event for event in deletions(stream)}
        self.assertEqual(set(by_owner), {ALICE, BOB, None})
        self.assertEqual(by_owner[ALICE]["target"]["ids"], [alice])
        self.assertEqual(by_owner[BOB]["target"]["ids"], [bob])
        self.assertEqual(by_owner[None]["target"]["ids"], [nobody])
        for event in deletions(stream):
            self.assertNotIn("owner_email", event["criteria"])
        for detail in ("alice@example.org", "Alice Example", "Bob Example"):
            self.assertNotIn(detail, stream.getvalue())

    def test_internal_chats_go_with_their_parent(self) -> None:
        """Sub-agent chats, at any depth and however recent, follow the expired chat they came from."""
        parent = self.store.chat(ALICE, age_days=90)
        child = self.store.internal_chat(ALICE, parent, age_days=1)
        grandchild = self.store.internal_chat(ALICE, child, age_days=1)
        self.store.message(grandchild, ALICE)
        _, stream = self.sweep(30)
        self.assertEqual(self.store.chat_ids(), set())
        (event,) = deletions(stream)
        self.assertEqual(event["target"]["ids"], [parent, child, grandchild])
        self.assertEqual(event["criteria"]["internal_chats"], 2)
        self.assertEqual(event["criteria"]["messages"], 1)

    def test_internal_chat_of_a_live_parent_stays(self) -> None:
        """An old sub-agent chat lives as long as the chat it belongs to."""
        parent = self.store.chat(ALICE, age_days=1)
        child = self.store.internal_chat(ALICE, parent, age_days=90)
        result, stream = self.sweep(30)
        self.assertEqual(result.chats, 0)
        self.assertEqual(self.store.chat_ids(), {parent, child})
        self.assertEqual(deletions(stream), [])

    def test_orphaned_internal_chat_expires_on_its_own_age(self) -> None:
        """Once its parent is gone, an internal chat is a chat like any other."""
        orphan = self.store.internal_chat(ALICE, "chat-gone", age_days=90)
        recent_orphan = self.store.internal_chat(ALICE, "chat-gone", age_days=1)
        self.sweep(30)
        self.assertEqual(self.store.chat_ids(), {recent_orphan})
        self.assertNotIn(orphan, self.store.chat_ids())

    def test_note_chats_expire_on_their_own_age(self) -> None:
        """Internal chats without a parent (a note's) are swept like any chat."""
        note = self.store.chat(ALICE, age_days=90, meta={"internal": True, "type": "note", "note_id": "note-1"})
        self.sweep(30)
        self.assertNotIn(note, self.store.chat_ids())

    def test_batches_are_bounded(self) -> None:
        """Many expired chats go in several transactions, each with its own record."""
        expired = [self.store.chat(ALICE, age_days=40 + index / 100) for index in range(120)]
        result, stream = self.sweep(30)
        self.assertEqual((result.chats, result.batches), (120, 3))
        records = deletions(stream)
        self.assertEqual([event["target"]["count"] for event in records], [50, 50, 20])
        recorded = [chat_id for event in records for chat_id in event["target"]["ids"]]
        self.assertEqual(recorded, list(reversed(expired)), "oldest first")

    def test_batch_cap_leaves_the_rest_for_the_next_round(self) -> None:
        """A sweep stops after its batch cap and says it is not complete."""
        for index in range(60):
            self.store.chat(ALICE, age_days=40 + index)
        result, _ = self.sweep(30, max_batches=1)
        self.assertEqual((result.chats, result.complete), (50, False))
        self.assertEqual(len(self.store.chat_ids()), 10)

    def test_large_family_is_split_into_continuation_records(self) -> None:
        """More ids than one event holds go into retention.sweep.ids events linked to the first."""
        parent = self.store.chat(ALICE, age_days=90)
        children = [self.store.internal_chat(ALICE, parent, age_days=90) for _ in range(250)]
        _, stream = self.sweep(30)
        self.assertEqual(self.store.chat_ids(), set())
        primary, continuation = deletions(stream)
        self.assertEqual((primary["operation"], primary["target"]["count"]), ("retention.sweep", 251))
        self.assertEqual(len(primary["target"]["ids"]), 200)
        self.assertNotIn("ids_truncated", primary["target"])
        self.assertEqual(continuation["operation"], "retention.sweep.ids")
        self.assertEqual(continuation["parent_event_id"], primary["event_id"])
        self.assertEqual(continuation["target"]["count"], 51)
        self.assertEqual(primary["target"]["ids"] + continuation["target"]["ids"], [parent, *children])

    def test_chat_changed_after_planning_stays(self) -> None:
        """Inside the transaction a chat that changed since the plan no longer expires."""
        resumed = self.store.chat(ALICE, age_days=60)
        expired = self.store.chat(ALICE, age_days=61)
        real_plan = chat_retention.plan_owner

        def plan_then_resume(conn: sqlite3.Connection, owner: str | None, cutoff: int) -> list[Any]:
            families = real_plan(conn, owner, cutoff)
            self.store.conn.execute("UPDATE chat SET updated_at = ? WHERE id = ?", (NOW, resumed))
            return families

        with mock.patch.object(chat_retention, "plan_owner", plan_then_resume):
            _, stream = self.sweep(30)
        self.assertEqual(self.store.chat_ids(), {resumed})
        (event,) = deletions(stream)
        self.assertEqual(event["target"]["ids"], [expired])

    def test_malformed_json_does_not_stop_the_sweep(self) -> None:
        """Rows whose JSON does not parse are judged on their columns alone."""
        expired = self.store.chat(ALICE, age_days=60)
        self.store.conn.execute("UPDATE chat SET meta = 'not json' WHERE id = ?", (expired,))
        self.store.conn.execute(
            "INSERT INTO feedback (id, meta, created_at, updated_at) VALUES ('feedback-x', 'not json', 0, 0)"
        )
        self.sweep(30)
        self.assertEqual(self.store.chat_ids(), set())
        self.assertEqual(self.store.count("feedback"), 1)

    def test_deleted_content_is_overwritten(self) -> None:
        """secure_delete: once checkpointed, the deleted rows' text is gone from the database file."""
        expired = self.store.chat(ALICE, age_days=60)
        self.store.message(expired, ALICE)
        self.store.related(expired, ALICE)
        self.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertIn(SENTINEL.encode(), self.database.read_bytes())
        self.sweep(30)
        self.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertNotIn(SENTINEL.encode(), self.database.read_bytes())

    def test_missing_database_is_not_created(self) -> None:
        """Before Open WebUI's first start there is nothing to sweep, and nothing is created."""
        missing = self.tmp / "absent" / "webui.db"
        audit, stream = emitter()
        result = ChatSweeper(audit, missing, 30).sweep(NOW)
        audit.close()
        self.assertFalse(result.database)
        self.assertFalse(missing.exists())
        self.assertEqual(deletions(stream), [])


class FailureTest(Workspace):
    """No deletion without its record, and no record of a deletion that did not happen."""

    def test_failed_record_rolls_the_batch_back(self) -> None:
        """The record is written inside the transaction; when it fails, nothing is deleted."""
        expired = self.store.chat(ALICE, age_days=60)
        self.store.related(expired, ALICE)
        before = self.store.rows()
        audit, _ = emitter(FailingStream(fail_at=2))
        with self.assertRaises(AuditWriteError):
            ChatSweeper(audit, self.database, 30, pause_s=0).sweep(NOW)
        close_quietly(audit)
        self.assertEqual(self.store.rows(), before)

    def test_failed_commit_is_recorded_as_error(self) -> None:
        """A batch whose commit fails is rolled back and recorded again with outcome error."""
        expired = self.store.chat(ALICE, age_days=60)
        before = self.store.rows()
        real_open = chat_retention.open_database

        class CommitFails:
            """A connection whose COMMIT fails."""

            def __init__(self, conn: sqlite3.Connection) -> None:
                """Wrap a connection.

                Args:
                    conn: The real connection.
                """
                self._conn = conn

            def __getattr__(self, name: str) -> Any:
                """Pass everything else through.

                Args:
                    name: The attribute.

                Returns:
                    The real connection's attribute.
                """
                return getattr(self._conn, name)

            def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
                """Fail on COMMIT.

                Args:
                    sql: The statement.
                    *args: Its parameters.

                Returns:
                    The cursor.

                Raises:
                    sqlite3.OperationalError: For COMMIT.
                """
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("disk I/O error")
                return self._conn.execute(sql, *args)

        def open_failing(path: Path, **kwargs: Any) -> Any:
            return CommitFails(real_open(path, **kwargs))

        audit, stream = emitter()
        with (
            mock.patch.object(chat_retention, "open_database", open_failing),
            self.assertRaises(sqlite3.Error),
            self.assertLogs("retention", "ERROR"),
        ):
            ChatSweeper(audit, self.database, 30, pause_s=0).sweep(NOW)
        audit.close()
        self.assertEqual(self.store.rows(), before)
        primary, failure = deletions(stream)
        self.assertEqual(primary["outcome"], "ok")
        self.assertEqual(failure["outcome"], "error")
        self.assertEqual(failure["parent_event_id"], primary["event_id"])
        self.assertEqual(failure["target"], {"kind": "chat", "ids": [expired], "count": 1})
        self.assertEqual(chain_problems(stream), [])

    def test_locked_database_fails_the_round_without_a_record(self) -> None:
        """While Open WebUI holds the write lock longer than the busy timeout, the sweep gives up."""
        self.store.chat(ALICE, age_days=60)
        holder = sqlite3.connect(self.database, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        audit, stream = emitter()
        with self.assertRaises(sqlite3.OperationalError):
            ChatSweeper(audit, self.database, 30, pause_s=0, busy_timeout_s=0.2).sweep(NOW)
        holder.execute("ROLLBACK")
        audit.close()
        self.assertEqual(len(self.store.chat_ids()), 1)
        self.assertEqual(deletions(stream), [])

    def test_sweep_waits_for_a_short_lock(self) -> None:
        """A write Open WebUI finishes within the busy timeout only delays the sweep."""
        self.store.chat(ALICE, age_days=60)
        holder = sqlite3.connect(self.database, isolation_level=None, check_same_thread=False)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")

        def release() -> None:
            holder.execute("ROLLBACK")

        timer = threading.Timer(0.3, release)
        timer.start()
        self.addCleanup(timer.cancel)
        result, _ = self.sweep(30, busy_timeout_s=5)
        self.assertEqual(result.chats, 1)


class PreviewTest(Workspace):
    """The dry run counts exactly what a sweep would delete, and changes nothing."""

    def test_preview_matches_the_sweep_and_changes_nothing(self) -> None:
        """Counts, owners, ages; then the sweep deletes exactly that."""
        parent = self.store.chat(ALICE, age_days=90, tags=["only_here"])
        self.store.internal_chat(ALICE, parent, age_days=2)
        self.store.message(parent, ALICE)
        self.store.related(parent, ALICE)
        self.store.tag(ALICE, "only_here")
        self.store.chat(BOB, age_days=45)
        self.store.chat(BOB, age_days=3)
        before = self.store.rows()
        conn = open_database(self.database, readonly=True)
        try:
            counts = preview(conn, NOW - 30 * DAY)
        finally:
            conn.close()
        self.assertEqual(self.store.rows(), before)
        self.assertEqual((counts.chats, counts.internal, counts.owners, counts.kept), (3, 1, 2, 1))
        self.assertEqual((counts.oldest, counts.newest), (NOW - 90 * DAY, NOW - 45 * DAY))
        self.assertEqual(counts.related["messages"], 1)
        self.assertEqual(counts.related["feedback"], 1)
        self.assertEqual(counts.tags, 1)
        _, stream = self.sweep(30)
        swept = deletions(stream)
        self.assertEqual(sum(event["target"]["count"] for event in swept), counts.chats)
        for key, value in counts.related.items():
            self.assertEqual(sum(event["criteria"][key] for event in swept), value, key)

    def test_read_only_connection_cannot_write(self) -> None:
        """The dry run's connection refuses writes."""
        conn = open_database(self.database, readonly=True)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.Error):
            conn.execute("DELETE FROM chat")

    def test_events_hold_no_content(self) -> None:
        """Nothing a user wrote reaches a record: no titles, messages, tags or comments."""
        expired = self.store.chat(ALICE, age_days=60, tags=[SENTINEL])
        self.store.related(expired, ALICE)
        self.store.tag(ALICE, SENTINEL)
        _, stream = self.sweep(30)
        self.assertTrue(deletions(stream))
        self.assertNotIn(SENTINEL, stream.getvalue())
        for event in events(stream):
            self.assertNotIn("purpose", event)
            self.assertNotIn("request_id", event)


if __name__ == "__main__":
    unittest.main()
