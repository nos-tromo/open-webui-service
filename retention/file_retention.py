"""Retention of the uploads Open WebUI kept from before its intake was closed.

Uploads have been refused since edge-guard closed the intake routes, but files
uploaded before stay: their row in ``webui.db`` (``file``, whose ``data`` holds
the extracted text) and the file itself under the data volume's ``uploads/``.
Open WebUI never expires them. The sweeper deletes every upload made more than
the configured period ago (the chats' period), with what belongs only to it,
read from the pinned image's ``routers/files.py`` ``delete_file_by_id`` and
``storage/provider.py``:

* the row, its chat links (``chat_file``), knowledge links (``knowledge_file``)
  and access grants (``access_grant`` rows of ``resource_type='file'``);
* the file on disk. The image records its path as
  ``/app/backend/data/uploads/<name>`` and deletes it by base name in its
  upload directory, as this sweeper does in ``<data volume>/uploads``. A
  recorded path elsewhere is never followed: its row is kept and named in the
  log. A symbolic link is removed itself, never its target.

What the image derived into its vector store (the ``file-<id>`` collection) is
not reachable without Open WebUI's own client: every record says so
(``vectors_left_in_store``), the log names *Reset vector storage* in Open
WebUI's admin settings, which clears it, and so does the dry run while uploads
are due (CLAUDE.md § Retention).

An ``uploads/`` that is a symbolic link, or not a directory, refuses the store
(:class:`UploadsRefused`). A name in it that is neither a regular file nor a
link (a directory, say) keeps its row, logged, so one odd entry never stops
the others.

A file's ``created_at`` is in seconds. The record names uploads by id only,
never by file name, which can name a person. Each batch is recorded, then its
files are removed from disk, then the rows' deletion is committed; when the
commit or a removal fails, the batch is rolled back and recorded as failed.
Locking, the schema guard and the checkpoint are the chat sweeper's. Stdlib only.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
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
    _placeholders,
    _remove,
    open_database,
    schema_problems,
    truncate_wal,
)
from sweep_events import counted, emit_deletion, iso_utc

LOG = logging.getLogger("retention")

#: Where the pinned image records its uploads.
IMAGE_UPLOAD_DIR: Final = "/app/backend/data/uploads/"
#: What belongs to an upload besides its row.
FILE_RELATED: Final = (
    Related("chat_links", "chat_file", "file_id IN ({ids})"),
    Related("knowledge_links", "knowledge_file", "file_id IN ({ids})"),
    Related("grants", "access_grant", "resource_type = 'file' AND resource_id IN ({ids})"),
)


class UploadsRefused(Exception):
    """``uploads/`` is not a plain directory on the data volume; nothing is deleted."""


@dataclass
class FileSweep:
    """What one upload sweep did.

    Attributes:
        files: Upload rows deleted.
        removed_from_disk: Files removed from the uploads directory.
        refused: Rows kept because their recorded path is not an upload's.
        batches: Write transactions committed.
        complete: Whether everything expired was handled.
        database: Whether the database existed.
        log_emptied: Whether the write-ahead log was emptied after the
            deletions; ``None`` when nothing was deleted.
    """

    files: int = 0
    removed_from_disk: int = 0
    refused: int = 0
    batches: int = 0
    complete: bool = True
    database: bool = True
    log_emptied: bool | None = None


def upload_name(path: object) -> str | None:
    """Return the base name of an upload the image recorded, or ``None`` when the path is not one.

    Args:
        path: The ``file.path`` value.

    Returns:
        The name inside the upload directory.
    """
    if not isinstance(path, str) or not path.startswith(IMAGE_UPLOAD_DIR):
        return None
    name = path[len(IMAGE_UPLOAD_DIR) :]
    if not name or "/" in name or name in {".", ".."} or "\x00" in name:
        return None
    return name


def due_uploads(conn: sqlite3.Connection, cutoff: int) -> list[tuple[str, str | None, object]]:
    """List the uploads made before the cutoff, oldest first.

    Args:
        conn: The connection.
        cutoff: Seconds since the epoch.

    Returns:
        ``(id, owner, recorded path)`` per upload.
    """
    rows = conn.execute("SELECT id, user_id, path FROM file WHERE created_at < ? ORDER BY created_at, id", (cutoff,))
    return [(str(row[0]), None if row[1] is None else str(row[1]), row[2]) for row in rows]


def count_uploads(conn: sqlite3.Connection, cutoff: int) -> tuple[int, int]:
    """Count what a sweep with this cutoff would delete and keep (the dry run).

    An upload whose recorded path is outside the upload directory is never
    deleted, so it counts as kept.

    Args:
        conn: A read-only connection.
        cutoff: Seconds since the epoch.

    Returns:
        Uploads that would be deleted, and uploads that would stay.
    """
    due = sum(1 for _, _, path in due_uploads(conn, cutoff) if upload_name(path) is not None)
    (total,) = conn.execute("SELECT count(*) FROM file").fetchone()
    return due, int(total) - due


class FileSweeper:
    """Deletes expired uploads, their rows and their files, in bounded, recorded transactions."""

    def __init__(
        self,
        emitter: AuditEmitter,
        database: Path,
        data_dir: Path,
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
            data_dir: Its data volume (``uploads/`` lives there).
            days: The retention period.
            busy_timeout_s: How long a statement waits for a lock.
            pause_s: The pause between two write transactions.
            max_batches: Batches per sweep, at most.
            stop: Ends a sweep after the current batch when set.
        """
        self._emitter = emitter
        self._database = database
        self._uploads = data_dir / "uploads"
        self._days = days
        self._busy_timeout_s = busy_timeout_s
        self._pause_s = pause_s
        self._max_batches = max_batches
        self._stop = stop or threading.Event()

    def sweep(self, now: float) -> FileSweep:
        """Delete every upload made more than the period ago, oldest first.

        Args:
            now: The sweep's time, in seconds since the epoch.

        Returns:
            What was deleted.

        Raises:
            SchemaRefused: If the database is not the verified schema.
            sqlite3.Error: If the database could not be read or written.
            OSError: If a file could not be removed; its batch was rolled back.
            AuditWriteError: If a record could not be written; its batch was rolled back.
            RuntimeError: If the emitter is closed.
        """
        result = FileSweep()
        if not self._database.is_file():
            result.database = False
            return result
        cutoff = int(now) - self._days * 86_400
        if self._uploads.is_symlink() or (self._uploads.exists() and not self._uploads.is_dir()):
            raise UploadsRefused(f"{self._uploads.name} is a symbolic link or not a directory")
        conn = open_database(self._database, readonly=False, busy_timeout_s=self._busy_timeout_s)
        try:
            problems = schema_problems(conn)
            if problems:
                raise SchemaRefused(problems)
            planned: dict[str | None, list[tuple[str, str]]] = {}
            for file_id, owner, path in due_uploads(conn, cutoff):
                name = upload_name(path)
                if name is None:
                    result.refused += 1
                    LOG.warning("uploads: %s records a path outside the upload directory; kept", file_id)
                    continue
                if not self._removable(name):
                    result.refused += 1
                    LOG.warning("uploads: the file of %s is not a regular file; kept", file_id)
                    continue
                planned.setdefault(owner, []).append((file_id, name))
            for owner, uploads in sorted(planned.items(), key=lambda item: (item[0] is not None, item[0] or "")):
                for start in range(0, len(uploads), ROOTS_PER_BATCH):
                    if result.batches >= self._max_batches or self._stop.is_set():
                        result.complete = False
                        break
                    deleted, removed = self._delete_batch(conn, owner, uploads[start : start + ROOTS_PER_BATCH], cutoff)
                    if deleted:
                        result.batches += 1
                        result.files += deleted
                        result.removed_from_disk += removed
                    self._stop.wait(self._pause_s)
                if not result.complete:
                    break
            if result.files:
                result.log_emptied = truncate_wal(conn)
        finally:
            conn.close()
        if result.files:
            LOG.warning(
                "uploads: what Open WebUI derived from the deleted uploads into its vector store stays until an "
                "admin runs Reset vector storage (Admin settings, Documents)"
            )
            LOG.info(
                "uploads: deleted %s made more than %s ago (%s removed from disk), in %s",
                counted(result.files, "uploads"),
                counted(self._days, "days"),
                result.removed_from_disk,
                counted(result.batches, "batches"),
            )
        return result

    def _removable(self, name: str) -> bool:
        """Check that an upload's name in ``uploads/`` is absent, a regular file or a link.

        Args:
            name: Its base name.

        Returns:
            Whether the sweeper may remove it.
        """
        try:
            mode = os.lstat(self._uploads / name).st_mode
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return stat.S_ISREG(mode) or stat.S_ISLNK(mode)

    def _remove_file(self, name: str) -> bool:
        """Remove one upload from the uploads directory, never following a link.

        Args:
            name: Its base name.

        Returns:
            Whether a file (or link) was there and is gone.

        Raises:
            OSError: If it is there and cannot be removed.
        """
        path = self._uploads / name
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        os.unlink(path)
        return True

    def _delete_batch(
        self, conn: sqlite3.Connection, owner: str | None, uploads: Sequence[tuple[str, str]], cutoff: int
    ) -> tuple[int, int]:
        """Delete one batch of an owner's uploads in one recorded transaction.

        Args:
            conn: The connection, outside any transaction.
            owner: The owner.
            uploads: ``(id, name in uploads/)`` per upload.
            cutoff: Seconds since the epoch.

        Returns:
            How many rows were deleted, and how many files were removed from disk.
        """
        rule: dict[str, object] = {"store": "uploads", "period_days": self._days, "cutoff": iso_utc(cutoff)}
        names = dict(uploads)
        conn.execute("BEGIN IMMEDIATE")
        try:
            problems = schema_problems(conn)
            if problems:
                raise SchemaRefused(problems)
            still: set[str] = set()
            for chunk in _chunks([file_id for file_id, _ in uploads]):
                rows = conn.execute(
                    f"SELECT id FROM file WHERE user_id IS ? AND created_at < ? AND id IN ({_placeholders(len(chunk))})",
                    (owner, cutoff, *chunk),
                )
                still.update(str(row[0]) for row in rows)
            ids = [file_id for file_id, _ in uploads if file_id in still]
            if not ids:
                conn.execute("ROLLBACK")
                return 0, 0
            counts = {related.key: _remove(conn, related, ids) for related in FILE_RELATED}
            files = 0
            for chunk in _chunks(ids):
                files += conn.execute(
                    f"DELETE FROM file WHERE user_id IS ? AND id IN ({_placeholders(len(chunk))})", (owner, *chunk)
                ).rowcount
            criteria = {**rule, "owner": owner, **counts, "vectors_left_in_store": True}
            record = emit_deletion(self._emitter, "upload", ids, criteria)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        try:
            removed = sum(self._remove_file(names[file_id]) for file_id in ids)
            conn.execute("COMMIT")
        except (OSError, sqlite3.Error):
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            LOG.error("uploads: a batch of %s could not be deleted and was rolled back", counted(len(ids), "uploads"))
            emit_deletion(
                self._emitter,
                "upload",
                ids,
                {**rule, "owner": owner},
                outcome="error",
                parent_event_id=str(record["event_id"]),
            )
            raise
        return files, removed
