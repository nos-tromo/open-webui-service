"""Note retention in Open WebUI's SQLite database (``webui.db``).

Notes are switched off here (``ENABLE_NOTES=false``; the pinned image's notes
router checks ``notes.enable`` on every route, ``routers/notes.py``
``check_notes_access``), but notes written before stay in the database, and
Open WebUI never expires them. The sweeper deletes every note created more
than the configured period ago (the chats' period), with what belongs only to
it, read from the pinned image's ``models/notes.py`` ``delete_note_by_id`` and
``models/comments.py`` ``delete_resource_comments``:

* the note and its pins (``pinned_note``);
* its comments, their reactions, and the notifications about it;
* its access grants (``access_grant`` rows of ``resource_type='note'``), which
  upstream's delete leaves behind.

A note's chats (internal chats with ``meta.note_id``) are due once their note
is gone; the chat sweeper deletes them (:mod:`chat_retention`).

Open WebUI writes a note's ``created_at`` in nanoseconds (``time.time_ns()``),
unlike chats and files. A note without ``created_at`` is never deleted.

Locking, the schema guard, the record written before each commit, and the
``TRUNCATE`` checkpoint after a sweep that deleted something are the chat
sweeper's (:mod:`chat_retention`). Stdlib only.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from audit_event import AuditEmitter
from chat_retention import (
    BATCH_PAUSE_S,
    BUSY_TIMEOUT_S,
    MAX_BATCHES,
    ROOTS_PER_BATCH,
    Related,
    SchemaRefused,
    _chunks,
    _existing,
    _placeholders,
    _remove,
    open_database,
    schema_problems,
    truncate_wal,
)
from sweep_events import counted, emit_deletion, iso_utc

LOG = logging.getLogger("retention")

#: Open WebUI's notes count time in nanoseconds.
NS: Final = 1_000_000_000
#: What belongs to a note, in order: reactions before the comments they belong to.
NOTE_RELATED: Final = (
    Related("pins", "pinned_note", "note_id IN ({ids})"),
    Related(
        "comment_reactions",
        "comment_reaction",
        "comment_id IN (SELECT id FROM comment WHERE resource_type = 'note' AND resource_id IN ({ids}))",
    ),
    Related("comments", "comment", "resource_type = 'note' AND resource_id IN ({ids})"),
    Related(
        "notifications",
        "notification",
        "resource_type = 'note' AND resource_id IN ({ids})",
    ),
    Related(
        "grants", "access_grant", "resource_type = 'note' AND resource_id IN ({ids})"
    ),
)


@dataclass
class NoteSweep:
    """What one note sweep did.

    Attributes:
        notes: Notes deleted.
        owners: The accounts they belonged to.
        batches: Write transactions committed.
        complete: Whether everything expired was handled.
        database: Whether the database existed.
        log_emptied: Whether the write-ahead log was emptied after the
            deletions; ``None`` when nothing was deleted.
    """

    notes: int = 0
    owners: int = 0
    batches: int = 0
    complete: bool = True
    database: bool = True
    log_emptied: bool | None = None


def expired_notes(
    conn: sqlite3.Connection, owner: str | None, cutoff_ns: int
) -> list[str]:
    """List an owner's notes created before the cutoff, oldest first.

    Args:
        conn: The connection.
        owner: The owner.
        cutoff_ns: The cutoff, in nanoseconds since the epoch.

    Returns:
        The note ids.
    """
    rows = conn.execute(
        "SELECT id FROM note WHERE user_id IS ? AND created_at < ? ORDER BY created_at, id",
        (owner, cutoff_ns),
    )
    return [str(row[0]) for row in rows]


def note_owners(conn: sqlite3.Connection, cutoff_ns: int) -> list[str | None]:
    """List the accounts that have notes created before the cutoff.

    Args:
        conn: The connection.
        cutoff_ns: The cutoff, in nanoseconds since the epoch.

    Returns:
        Their user ids (``None`` for notes without an owner, first).
    """
    owners = {
        None if row[0] is None else str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT user_id FROM note WHERE created_at < ?", (cutoff_ns,)
        )
    }
    return sorted(owners, key=lambda owner: (owner is not None, owner or ""))


def count_notes(conn: sqlite3.Connection, cutoff_ns: int) -> tuple[int, int]:
    """Count what a sweep with this cutoff would delete and keep (the dry run).

    Args:
        conn: A read-only connection.
        cutoff_ns: The cutoff, in nanoseconds since the epoch.

    Returns:
        Notes that would be deleted, and notes that would stay.
    """
    (due,) = conn.execute(
        "SELECT count(*) FROM note WHERE created_at < ?", (cutoff_ns,)
    ).fetchone()
    (total,) = conn.execute("SELECT count(*) FROM note").fetchone()
    return int(due), int(total) - int(due)


class NoteSweeper:
    """Deletes expired notes in bounded, recorded transactions."""

    def __init__(
        self,
        emitter: AuditEmitter,
        database: Path,
        days: int,
        *,
        busy_timeout_s: float = BUSY_TIMEOUT_S,
        pause_s: float = BATCH_PAUSE_S,
        max_batches: int = MAX_BATCHES,
        stop: threading.Event | None = None,
    ) -> None:
        """Prepare a sweeper; nothing is read until :meth:`sweep`.

        Args:
            emitter: The started emitter.
            database: Open WebUI's database file.
            days: The retention period.
            busy_timeout_s: How long a statement waits for a lock.
            pause_s: The pause between two write transactions.
            max_batches: Batches per sweep, at most.
            stop: Ends a sweep after the current batch when set.
        """
        self._emitter = emitter
        self._database = database
        self._days = days
        self._busy_timeout_s = busy_timeout_s
        self._pause_s = pause_s
        self._max_batches = max_batches
        self._stop = stop or threading.Event()

    def sweep(self, now: float) -> NoteSweep:
        """Delete every note created more than the period ago, oldest first, per owner.

        Args:
            now: The sweep's time, in seconds since the epoch.

        Returns:
            What was deleted.

        Raises:
            SchemaRefused: If the database is not the verified schema.
            sqlite3.Error: If the database could not be read or written; the
                failed batch was rolled back.
            AuditWriteError: If a record could not be written; its batch was
                rolled back.
            RuntimeError: If the emitter is closed.
        """
        result = NoteSweep()
        if not self._database.is_file():
            result.database = False
            return result
        cutoff = int(now) - self._days * 86_400
        conn = open_database(
            self._database, readonly=False, busy_timeout_s=self._busy_timeout_s
        )
        try:
            problems = schema_problems(conn)
            if problems:
                raise SchemaRefused(problems)
            for owner in note_owners(conn, cutoff * NS):
                ids = expired_notes(conn, owner, cutoff * NS)
                deleted_for_owner = 0
                for start in range(0, len(ids), ROOTS_PER_BATCH):
                    if result.batches >= self._max_batches or self._stop.is_set():
                        result.complete = False
                        break
                    deleted = self._delete_batch(
                        conn, owner, ids[start : start + ROOTS_PER_BATCH], cutoff
                    )
                    if deleted:
                        result.batches += 1
                        result.notes += deleted
                        deleted_for_owner += deleted
                    self._stop.wait(self._pause_s)
                if deleted_for_owner:
                    result.owners += 1
                if not result.complete:
                    break
            if result.notes:
                result.log_emptied = truncate_wal(conn)
                if not result.log_emptied:
                    LOG.warning(
                        "notes: the write-ahead log stayed busy, so earlier versions of the deleted rows may stay "
                        "in it until a later checkpoint"
                    )
        finally:
            conn.close()
        if result.notes:
            LOG.info(
                "notes: deleted %s of %s created more than %s ago, in %s",
                counted(result.notes, "notes"),
                counted(result.owners, "accounts"),
                counted(self._days, "days"),
                counted(result.batches, "batches"),
            )
        return result

    def _delete_batch(
        self,
        conn: sqlite3.Connection,
        owner: str | None,
        planned: Sequence[str],
        cutoff: int,
    ) -> int:
        """Delete one batch of an owner's notes in one recorded transaction.

        Args:
            conn: The connection, outside any transaction.
            owner: The owner.
            planned: The notes planned for deletion.
            cutoff: Seconds since the epoch.

        Returns:
            How many notes were deleted.
        """
        rule: dict[str, object] = {
            "store": "notes",
            "period_days": self._days,
            "cutoff": iso_utc(cutoff),
        }
        conn.execute("BEGIN IMMEDIATE")
        try:
            problems = schema_problems(conn)
            if problems:
                raise SchemaRefused(problems)
            still = _existing(
                conn,
                "SELECT id FROM note WHERE user_id IS ? AND created_at < ? AND id IN ({ids})",
                planned,
                (owner, cutoff * NS),
            )
            ids = [note_id for note_id in planned if note_id in still]
            if not ids:
                conn.execute("ROLLBACK")
                return 0
            counts = {
                related.key: _remove(conn, related, ids) for related in NOTE_RELATED
            }
            notes = 0
            for chunk in _chunks(ids):
                notes += conn.execute(
                    f"DELETE FROM note WHERE user_id IS ? AND id IN ({_placeholders(len(chunk))})",
                    (owner, *chunk),
                ).rowcount
            # The owner by Open WebUI's opaque account id only, never the person's address.
            record = emit_deletion(
                self._emitter, "note", ids, {**rule, "owner": owner, **counts}
            )
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            LOG.error(
                "notes: a batch of %s could not be committed and was rolled back",
                counted(len(ids), "notes"),
            )
            emit_deletion(
                self._emitter,
                "note",
                ids,
                {**rule, "owner": owner},
                outcome="error",
                parent_event_id=str(record["event_id"]),
            )
            raise
        return notes
