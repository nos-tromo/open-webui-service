"""Synthetic Open WebUI data for the tests: a database with the pinned schema, cache files.

The database is built from ``fixtures/webui-schema.sql``, the verbatim
``sqlite_master`` of a database the pinned image (Open WebUI v0.13.3) migrated,
and runs in WAL mode as Open WebUI's does. Every row is invented; every text
field carries :data:`SENTINEL`, which must never reach a record or a report.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, override

from audit_event import AuditEmitter, AuditWriteError, verify_chain
from sweep_events import SERVICE

SCHEMA = (Path(__file__).resolve().parent / "fixtures" / "webui-schema.sql").read_text(encoding="utf-8")
REVISION = "9f6c2a8d1b43"
#: A fixed "now" for the tests: 2026-09-20T12:00:00Z.
NOW = 1_789_905_600
DAY = 86_400
#: Content no record or report may ever contain.
SENTINEL = "zz-sentinel-content"
ALICE = "0d0a1a1e-0000-4000-8000-00000000a11c"
BOB = "0d0a1a1e-0000-4000-8000-0000000000b0"


def make_database(path: Path, *, revision: str | None = REVISION) -> None:
    """Create an Open WebUI v0.13.3 database with two synthetic accounts.

    Args:
        path: The database file to create.
        revision: The migration revision to record; ``None`` records none.
    """
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA)
        if revision is not None:
            conn.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (revision,))
        for user_id, name, email in ((ALICE, "Alice Example", "alice@example.org"), (BOB, "Bob Example", None)):
            conn.execute(
                'INSERT INTO "user" (id, name, email, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, name, email, "user", NOW - 400 * DAY, NOW - 400 * DAY),
            )
        conn.commit()
        conn.execute("PRAGMA journal_mode=WAL")


class Store:
    """Writes synthetic rows into a test database, the way Open WebUI lays them out."""

    def __init__(self, path: Path) -> None:
        """Open the database.

        Args:
            path: The database file.
        """
        self.conn = sqlite3.connect(path, isolation_level=None)
        self._serial = 0

    def close(self) -> None:
        """Close the connection."""
        self.conn.close()

    def _id(self, prefix: str) -> str:
        """Invent an id.

        Args:
            prefix: What it identifies.

        Returns:
            E.g. ``msg-0003``.
        """
        self._serial += 1
        return f"{prefix}-{self._serial:04d}"

    def chat(
        self,
        owner: str | None,
        *,
        age_days: float | None,
        created_days: float | None = None,
        meta: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Add a chat.

        Args:
            owner: The owner's user id.
            age_days: Days since its last change (``updated_at``); ``None`` leaves it NULL.
            created_days: Days since it was created; defaults to its age.
            meta: Its ``meta``; tags are added to it.
            tags: Its tag ids.

        Returns:
            The chat id.
        """
        chat_id = self._id("chat")
        updated = None if age_days is None else int(NOW - age_days * DAY)
        created_age = created_days if created_days is not None else age_days
        created = None if created_age is None else int(NOW - created_age * DAY)
        data = dict(meta or {})
        if tags is not None:
            data["tags"] = tags
        body = {"history": {"messages": {"m1": {"role": "user", "content": f"{SENTINEL} question"}}}}
        self.conn.execute(
            "INSERT INTO chat (id, user_id, title, chat, created_at, updated_at, archived, pinned, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
            (chat_id, owner, f"{SENTINEL} title", json.dumps(body), created, updated, json.dumps(data)),
        )
        return chat_id

    def internal_chat(self, owner: str, parent: str, *, age_days: float) -> str:
        """Add a sub-agent chat spawned from ``parent``.

        Args:
            owner: The owner.
            parent: The chat it was spawned from.
            age_days: Days since its last change.

        Returns:
            The chat id.
        """
        return self.chat(
            owner, age_days=age_days, meta={"internal": True, "type": "subagent", "parent_chat_id": parent}
        )

    def note(self, owner: str | None, *, created_days: float, note_id: str | None = None) -> str:
        """Add a note, timed as Open WebUI times notes: in nanoseconds.

        Args:
            owner: The owner's user id.
            created_days: Days since it was created.
            note_id: Its id; a fresh one by default.

        Returns:
            The note id.
        """
        note = note_id or self._id("note")
        created = int((NOW - created_days * DAY) * 1_000_000_000)
        self.conn.execute(
            "INSERT INTO note (id, user_id, title, data, meta, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (note, owner, f"{SENTINEL} note", json.dumps({"content": {"md": SENTINEL}}), "{}", created, created),
        )
        return note

    def note_related(self, note_id: str, owner: str) -> None:
        """Add one row of every kind that belongs to a note: a pin, a comment with a reaction, a notification, a grant.

        Args:
            note_id: The note.
            owner: Its owner.
        """
        self.conn.execute(
            "INSERT INTO pinned_note (id, user_id, note_id, created_at) VALUES (?, ?, ?, ?)",
            (self._id("pin"), owner, note_id, NOW),
        )
        comment = self._id("comment")
        self.conn.execute(
            "INSERT INTO comment (id, user_id, resource_type, resource_id, content, created_at, updated_at) "
            "VALUES (?, ?, 'note', ?, ?, ?, ?)",
            (comment, owner, note_id, f"{SENTINEL} comment", NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO comment_reaction (id, user_id, comment_id, name, created_at) VALUES (?, ?, ?, 'thumbsup', ?)",
            (self._id("reaction"), BOB, comment, NOW),
        )
        self.conn.execute(
            "INSERT INTO notification (id, user_id, event_type, event_key, actor_type, resource_type, resource_id, "
            "object_type, object_id, data, created_at, updated_at) "
            "VALUES (?, ?, 'comment', ?, 'user', 'note', ?, 'comment', ?, ?, ?, ?)",
            (self._id("notif"), owner, self._id("key"), note_id, comment, json.dumps({"text": SENTINEL}), NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, "
            "created_at) VALUES (?, 'note', ?, 'user', ?, 'read', ?)",
            (self._id("grant"), note_id, BOB, NOW),
        )

    def upload(self, owner: str | None, *, created_days: float, path: str | None = None) -> str:
        """Add an uploaded file's row the way the pinned image writes it (seconds; path under its upload dir).

        Args:
            owner: The owner's user id.
            created_days: Days since it was uploaded.
            path: Its recorded path; ``/app/backend/data/uploads/<id>_upload.txt`` by default.

        Returns:
            The file id.
        """
        file_id = self._id("file")
        recorded = path if path is not None else f"/app/backend/data/uploads/{file_id}_upload.txt"
        created = int(NOW - created_days * DAY)
        self.conn.execute(
            "INSERT INTO file (id, user_id, filename, meta, data, path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, owner, f"{SENTINEL}.txt", "{}", json.dumps({"content": SENTINEL}), recorded, created, created),
        )
        return file_id

    def message(self, chat_id: str, owner: str) -> str:
        """Add a message to a chat.

        Args:
            chat_id: The chat.
            owner: Its owner.

        Returns:
            The message id.
        """
        message_id = self._id("msg")
        self.conn.execute(
            "INSERT INTO chat_message (id, chat_id, user_id, role, content, created_at, updated_at) "
            "VALUES (?, ?, ?, 'user', ?, ?, ?)",
            (message_id, chat_id, owner, json.dumps(f"{SENTINEL} message"), NOW - DAY, NOW - DAY),
        )
        return message_id

    def related(self, chat_id: str, owner: str) -> None:
        """Add one row of every kind that belongs to a chat.

        A share and its access grant, a file link, a comment with a reaction, a
        notification, a rating, a legacy tag link, another account's saved view
        of it, and an automation run.

        Args:
            chat_id: The chat.
            owner: Its owner.
        """
        share = self._id("share")
        self.conn.execute(
            "INSERT INTO shared_chat (id, chat_id, user_id, title, chat, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (share, chat_id, owner, f"{SENTINEL} title", json.dumps({"title": SENTINEL}), NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, "
            "created_at) VALUES (?, 'shared_chat', ?, 'user', ?, 'read', ?)",
            (self._id("grant"), chat_id, BOB, NOW),
        )
        file_id = self._id("file")
        self.conn.execute(
            "INSERT INTO file (id, user_id, filename, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (file_id, owner, "synthetic-upload.txt", NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO chat_file (id, user_id, chat_id, file_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (self._id("link"), owner, chat_id, file_id, NOW, NOW),
        )
        comment = self._id("comment")
        self.conn.execute(
            "INSERT INTO comment (id, user_id, resource_type, resource_id, content, created_at, updated_at) "
            "VALUES (?, ?, 'chat', ?, ?, ?, ?)",
            (comment, owner, chat_id, f"{SENTINEL} comment", NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO comment_reaction (id, user_id, comment_id, name, created_at) VALUES (?, ?, ?, 'thumbsup', ?)",
            (self._id("reaction"), BOB, comment, NOW),
        )
        self.conn.execute(
            "INSERT INTO notification (id, user_id, event_type, event_key, actor_type, resource_type, resource_id, "
            "object_type, object_id, data, created_at, updated_at) "
            "VALUES (?, ?, 'comment', ?, 'user', 'chat', ?, 'comment', ?, ?, ?, ?)",
            (self._id("note"), owner, self._id("key"), chat_id, comment, json.dumps({"text": SENTINEL}), NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO feedback (id, user_id, version, type, data, meta, snapshot, created_at, updated_at) "
            "VALUES (?, ?, 0, 'rating', ?, ?, ?, ?, ?)",
            (
                self._id("feedback"),
                owner,
                json.dumps({"rating": 1, "comment": SENTINEL}),
                json.dumps({"chat_id": chat_id, "message_id": "m1"}),
                json.dumps({"chat": {"title": SENTINEL}}),
                NOW,
                NOW,
            ),
        )
        self.conn.execute(
            "INSERT INTO chatidtag (id, tag_name, chat_id, user_id, timestamp) VALUES (?, ?, ?, ?, ?)",
            (self._id("legacy"), SENTINEL, chat_id, owner, NOW),
        )
        self.conn.execute(
            "INSERT INTO chat_view_state (user_id, chat_id, state, updated_at) VALUES (?, ?, ?, ?)",
            (BOB, chat_id, json.dumps({"schemaVersion": 1, "panels": {"tabs": [{"label": SENTINEL}]}}), NOW),
        )
        self.conn.execute(
            "INSERT INTO automation_run (id, automation_id, chat_id, status, created_at) "
            "VALUES (?, ?, ?, 'success', ?)",
            (self._id("run"), "automation-0001", chat_id, NOW),
        )

    def tag(self, owner: str, tag_id: str) -> None:
        """Add a tag row.

        Args:
            owner: Its owner.
            tag_id: Its id.
        """
        self.conn.execute("INSERT INTO tag (id, name, user_id) VALUES (?, ?, ?)", (tag_id, tag_id, owner))

    def count(self, table: str, where: str = "1 = 1", params: tuple[object, ...] = ()) -> int:
        """Count rows.

        Args:
            table: The table.
            where: A condition.
            params: Its parameters.

        Returns:
            The count.
        """
        return int(self.conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}', params).fetchone()[0])

    def chat_ids(self) -> set[str]:
        """List every chat id.

        Returns:
            The ids.
        """
        return {str(row[0]) for row in self.conn.execute("SELECT id FROM chat")}

    def rows(self) -> dict[str, list[tuple[Any, ...]]]:
        """Read every row of every table, to compare a database before and after.

        Returns:
            Rows by table, sorted.
        """
        tables = [str(row[0]) for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {table: sorted(self.conn.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr) for table in tables}


class FailingStream(io.StringIO):
    """A stream that fails from its ``fail_at``-th line on, like a broken log pipe."""

    def __init__(self, fail_at: int) -> None:
        """Count lines from now on.

        Args:
            fail_at: The first line (1-based) that fails.
        """
        super().__init__()
        self._fail_at = fail_at
        self._lines = 0

    @override
    def write(self, s: str) -> int:
        """Fail once ``fail_at`` lines were attempted.

        Args:
            s: The text.

        Returns:
            Its length.

        Raises:
            OSError: From the ``fail_at``-th line on.
        """
        self._lines += 1
        if self._lines >= self._fail_at:
            raise OSError("synthetic write failure")
        return super().write(s)


def close_quietly(audit: AuditEmitter) -> None:
    """Close an emitter whose stream was made to fail, so its exit hook stays quiet.

    Args:
        audit: The emitter.
    """
    with contextlib.suppress(AuditWriteError, RuntimeError):
        audit.close()


def emitter(stream: io.StringIO | None = None) -> tuple[AuditEmitter, io.StringIO]:
    """Start an emitter on a captured stream.

    Args:
        stream: The stream; a fresh one by default.

    Returns:
        The started emitter and its stream.
    """
    out = stream if stream is not None else io.StringIO()
    audit = AuditEmitter(SERVICE, "0.9.0", stream=out, heartbeat_interval_s=0)
    audit.start()
    return audit, out


def events(stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse everything an emitter wrote.

    Args:
        stream: The emitter's stream.

    Returns:
        The events.
    """
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def chain_problems(stream: io.StringIO) -> list[object]:
    """Verify the chains an emitter wrote.

    Args:
        stream: The emitter's stream.

    Returns:
        The problems found (none when intact).
    """
    return list(verify_chain(events(stream)).problems)


def deletions(stream: io.StringIO) -> list[dict[str, Any]]:
    """Select the ``delete`` events.

    Args:
        stream: The emitter's stream.

    Returns:
        The events, in order.
    """
    return [event for event in events(stream) if event["action"] == "delete"]


def write_file(directory: Path, name: str, *, age_s: float, size: int = 16) -> Path:
    """Create a cache file last changed ``age_s`` seconds before :data:`NOW`.

    Args:
        directory: The cache directory.
        name: The file name.
        age_s: Its age.
        size: Its size in bytes.

    Returns:
        The file.
    """
    path = directory / name
    path.write_bytes(SENTINEL.encode() + b"x" * max(size - len(SENTINEL), 0))
    os.utime(path, (NOW - age_s, NOW - age_s))
    return path
