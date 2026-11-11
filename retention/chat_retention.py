"""Chat retention in Open WebUI's SQLite database (``webui.db``).

Open WebUI v0.13.3 has no retention setting for chats, and with admin access to
other users' chats closed nobody can expire them through its API, so the sweeper
deletes them in the database itself: every chat not changed for the configured
number of days, with everything that belongs only to it. What that is was read
from the pinned image's source (``models/chats.py`` ``delete_chat_by_id``,
``routers/chats.py``, ``models/comments.py``, ``models/shared_chats.py``). Where
upstream's own delete leaves copies behind, the sweeper removes them too:

* the chat, and the internal chats spawned from it (``meta.internal`` with
  ``meta.parent_chat_id``: sub-agent runs) at any depth, as upstream's delete
  route cascades;
* its messages (``chat_message``);
* its shared snapshots (``shared_chat``) and their access grants
  (``access_grant`` rows of ``resource_type='shared_chat'``), which upstream's
  delete leaves behind;
* its comments, their reactions, and the notifications about it;
* the ratings of its messages (``feedback`` rows whose ``meta.chat_id`` names
  it): each carries a full copy of the chat in ``snapshot``, and upstream keeps
  them;
* its file links (``chat_file``; Open WebUI never turns SQLite's foreign keys
  on, so the declared cascade does not fire) and legacy tag links
  (``chatidtag``);
* every viewer's saved working view of it (``chat_view_state``: open tabs,
  their labels, file paths and terminal folders), as upstream's delete does;
* the owner's tags that no remaining chat of theirs uses, as upstream's delete
  route does.

Automation runs that point at the chat keep their record and lose the link
(``chat_id`` set to NULL), as upstream does. Uploaded files belong to their
owner, not to a chat, and are not touched. An internal chat whose parent still
exists lives and dies with it; one whose parent is gone expires on its own age.
A chat's age is the time since it was started, its ``created_at``
(``updated_at`` where that is missing), in seconds, as Open WebUI writes both:
a chat that is continued keeps its oldest messages, so continuing it does not
keep it. A chat with neither time is never deleted. A note's chat (an internal
chat with ``meta.note_id``) goes with its note: once the note is gone it is
due, whatever its age.

Schema guard: before anything is read for deletion, and again inside every
write transaction, :func:`schema_problems` checks the database's migration
revision against :data:`VERIFIED_REVISIONS`, the columns every statement here
relies on, and that no table outside :data:`CHAT_ID_TABLES` refers to chats. A
newer Open WebUI image therefore stops chat retention with a clear error until
its schema has been verified, instead of deleting by an outdated picture.

Locking: Open WebUI keeps running and writes the same database (WAL mode). The
sweeper reads without a lock, then deletes in short ``BEGIN IMMEDIATE``
transactions of at most :data:`ROOTS_PER_BATCH` chats and :data:`IDS_PER_BATCH`
ids, waiting up to :data:`BUSY_TIMEOUT_S` for the write lock and pausing between
batches so Open WebUI's own writes get through. Each transaction writes its
``delete`` record before it commits and rolls back if the record cannot be
written. ``secure_delete`` is on, so the deleted rows are overwritten with zeros
in the database file instead of lingering in free pages. Earlier versions of
those rows can still sit in the write-ahead log (``webui.db-wal``), in frames
nothing has overwritten yet, so a sweep that deleted chats ends with a
``TRUNCATE`` checkpoint (:func:`truncate_wal`): the zeroed pages replace the old
ones in the database file and the log is emptied. Stdlib only.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from audit_event import AuditEmitter
from sweep_events import counted, emit_deletion, iso_utc

LOG = logging.getLogger("retention")

#: The migration revisions whose schema this module was verified against:
#: Open WebUI v0.13.3 (``alembic_version`` of a freshly migrated database).
VERIFIED_REVISIONS: Final = frozenset({"9f6c2a8d1b43"})
#: The columns the statements below read or write, by table.
REQUIRED_COLUMNS: Final[Mapping[str, frozenset[str]]] = {
    "alembic_version": frozenset({"version_num"}),
    "chat": frozenset({"id", "user_id", "created_at", "updated_at", "meta"}),
    "note": frozenset({"id", "user_id", "created_at"}),
    "pinned_note": frozenset({"note_id"}),
    "file": frozenset({"id", "user_id", "created_at", "path"}),
    "knowledge_file": frozenset({"file_id"}),
    "chat_message": frozenset({"chat_id"}),
    "shared_chat": frozenset({"chat_id"}),
    "access_grant": frozenset({"resource_type", "resource_id"}),
    "chat_file": frozenset({"chat_id", "file_id"}),
    "comment": frozenset({"id", "resource_type", "resource_id"}),
    "comment_reaction": frozenset({"comment_id"}),
    "notification": frozenset({"resource_type", "resource_id"}),
    "feedback": frozenset({"meta"}),
    "chatidtag": frozenset({"chat_id"}),
    "chat_view_state": frozenset({"chat_id"}),
    "automation_run": frozenset({"chat_id"}),
    "tag": frozenset({"id", "user_id"}),
}
#: Every table with a ``chat_id`` column or a foreign key to ``chat``; all of
#: them are cleaned up below. Any other is a schema this module does not know.
CHAT_ID_TABLES: Final = frozenset(
    {"automation_run", "chat_file", "chat_message", "chat_view_state", "chatidtag", "shared_chat"}
)
#: Chats a batch starts from, at most; their internal chats come with them.
ROOTS_PER_BATCH: Final = 50
#: Chat ids per batch, at most: one record's worth, so a batch is one event.
IDS_PER_BATCH: Final = 200
#: Batches per sweep, at most; the rest follow in the next round.
MAX_BATCHES: Final = 200
#: How long a write waits for Open WebUI to release the database.
BUSY_TIMEOUT_S: Final = 30.0
#: The pause between two write transactions.
BATCH_PAUSE_S: Final = 0.05
#: Ids bound per statement; longer lists run in several statements.
_BIND_CHUNK: Final = 500


def _json(path: str, column: str = "meta") -> str:
    """Build an SQL expression reading a JSON field that tolerates malformed JSON.

    Args:
        path: The JSON path, e.g. ``$.internal``.
        column: The column holding the JSON text.

    Returns:
        The field's value, or NULL when the column is not valid JSON.
    """
    return f"(CASE WHEN json_valid({column}) THEN json_extract({column}, '{path}') END)"


_INTERNAL: Final = f"{_json('$.internal')} IS 1"
#: A note's chat whose note no longer exists.
_ORPHANED_NOTE_CHAT: Final = (
    f"({_INTERNAL} AND {_json('$.note_id')} IS NOT NULL AND {_json('$.note_id')} NOT IN (SELECT id FROM note))"
)
#: A chat started before the cutoff (bound twice), or a note chat without its note.
_EXPIRED: Final = f"((created_at < ? OR (created_at IS NULL AND updated_at < ?)) OR {_ORPHANED_NOTE_CHAT})"
_STARTED: Final = "COALESCE(created_at, updated_at)"


@dataclass(frozen=True)
class Related:
    """Rows of another table that belong to the chats being deleted.

    Attributes:
        key: The count's name in the record's criteria.
        table: The table.
        where: The condition, with ``{ids}`` standing for the chat ids.
        unlink: For rows that stay: the assignment that drops the link.
    """

    key: str
    table: str
    where: str
    unlink: str | None = None


#: In order: reactions before the comments they belong to.
RELATED: Final = (
    Related("messages", "chat_message", "chat_id IN ({ids})"),
    Related("shares", "shared_chat", "chat_id IN ({ids})"),
    Related("share_grants", "access_grant", "resource_type = 'shared_chat' AND resource_id IN ({ids})"),
    Related("file_links", "chat_file", "chat_id IN ({ids})"),
    Related(
        "comment_reactions",
        "comment_reaction",
        "comment_id IN (SELECT id FROM comment WHERE resource_type = 'chat' AND resource_id IN ({ids}))",
    ),
    Related("comments", "comment", "resource_type = 'chat' AND resource_id IN ({ids})"),
    Related("notifications", "notification", "resource_type = 'chat' AND resource_id IN ({ids})"),
    Related("feedback", "feedback", f"{_json('$.chat_id')} IN ({{ids}})"),
    Related("legacy_tag_links", "chatidtag", "chat_id IN ({ids})"),
    Related("views", "chat_view_state", "chat_id IN ({ids})"),
    Related("automation_runs_unlinked", "automation_run", "chat_id IN ({ids})", unlink="chat_id = NULL"),
)


class SchemaRefused(Exception):
    """Raised when the database is not the schema chat retention was verified against.

    Attributes:
        problems: What does not match, by table and column name only.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        """Keep the problems.

        Args:
            problems: What does not match.
        """
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def open_database(path: Path, *, readonly: bool, busy_timeout_s: float = BUSY_TIMEOUT_S) -> sqlite3.Connection:
    """Open Open WebUI's database without ever creating it.

    Args:
        path: The database file.
        readonly: Open it read-only (the dry run and the schema check).
        busy_timeout_s: How long a statement waits for a lock.

    Returns:
        A connection in autocommit mode: every read is its own short
        transaction, and writes run in explicit ones.

    Raises:
        sqlite3.Error: If it cannot be opened.
    """
    uri = f"file:{urllib.parse.quote(str(path))}?mode={'ro' if readonly else 'rw'}"
    conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_s, isolation_level=None)
    try:
        # The container's /tmp is a small tmpfs: sort in memory, never in temp files.
        conn.execute("PRAGMA temp_store = MEMORY")
        if readonly:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA secure_delete = ON")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _quoted(name: str) -> str:
    """Quote an identifier for a PRAGMA.

    Args:
        name: A table name from the schema.

    Returns:
        The name in double quotes.
    """
    return '"' + name.replace('"', '""') + '"'


def truncate_wal(conn: sqlite3.Connection) -> bool:
    """Copy the write-ahead log into the database file and empty it.

    ``secure_delete`` zeroes the pages a transaction writes, but the log can
    still hold earlier versions of the deleted rows. A ``TRUNCATE`` checkpoint
    copies every frame into the database file, where the zeroed pages replace
    the old ones, and truncates the log to zero bytes. It waits for Open
    WebUI's readers and writers up to the connection's busy timeout, and holds
    off its writers while it copies.

    Args:
        conn: A read-write connection, outside any transaction.

    Returns:
        Whether the log was emptied: ``False`` when a reader or writer kept it
        busy, so the next sweep's checkpoint or Open WebUI's own carries the
        frames on. A database that is not in WAL mode has no log; ``True``.
    """
    busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return busy == 0


def schema_problems(conn: sqlite3.Connection) -> list[str]:
    """Check the database against the schema this module was verified with.

    Args:
        conn: A connection to the database.

    Returns:
        What does not match (empty when it all does), naming tables and
        columns only.
    """
    tables = sorted(str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
    known = set(tables)
    problems: list[str] = []
    if "alembic_version" in known:
        revisions = sorted(str(row[0]) for row in conn.execute("SELECT version_num FROM alembic_version"))
        if len(revisions) != 1 or revisions[0] not in VERIFIED_REVISIONS:
            problems.append(
                f"schema revision {', '.join(revisions) or 'none'} is not one this release was verified against "
                f"({', '.join(sorted(VERIFIED_REVISIONS))})"
            )
    columns: dict[str, set[str]] = {}
    for table in tables:
        columns[table] = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quoted(table)})")}
    for table, required in REQUIRED_COLUMNS.items():
        if table not in known:
            problems.append(f"table {table} is missing")
            continue
        missing = sorted(required - columns[table])
        if missing:
            problems.append(f"table {table} lacks column {', '.join(missing)}")
    for table in tables:
        references = {str(row[2]) for row in conn.execute(f"PRAGMA foreign_key_list({_quoted(table)})")}
        if ("chat_id" in columns[table] or "chat" in references) and table not in CHAT_ID_TABLES:
            problems.append(f"table {table} refers to chats and is not cleaned up with them")
    return problems


def _chunks(ids: Sequence[str]) -> Iterator[Sequence[str]]:
    """Split ids into lists short enough to bind in one statement.

    Args:
        ids: The ids.

    Yields:
        Consecutive slices of at most :data:`_BIND_CHUNK` ids.
    """
    for start in range(0, len(ids), _BIND_CHUNK):
        yield ids[start : start + _BIND_CHUNK]


def _placeholders(count: int) -> str:
    """Build a bind-parameter list.

    Args:
        count: How many parameters.

    Returns:
        E.g. ``?, ?, ?``.
    """
    return ", ".join("?" * count)


def _existing(conn: sqlite3.Connection, sql: str, ids: Sequence[str], before: Sequence[object] = ()) -> set[str]:
    """Run a ``SELECT id`` restricted to ``ids``, in bind-sized pieces.

    Args:
        conn: The connection.
        sql: The query, with ``{ids}`` standing for the id list.
        ids: The ids.
        before: Parameters bound before the ids.

    Returns:
        The ids the query returned.
    """
    found: set[str] = set()
    for chunk in _chunks(ids):
        rows = conn.execute(sql.format(ids=_placeholders(len(chunk))), (*before, *chunk))
        found.update(str(row[0]) for row in rows)
    return found


def _count(conn: sqlite3.Connection, related: Related, ids: Sequence[str]) -> int:
    """Count a related table's rows for ``ids``.

    Args:
        conn: The connection.
        related: The rows.
        ids: The chat ids.

    Returns:
        How many rows there are.
    """
    total = 0
    for chunk in _chunks(ids):
        sql = f"SELECT COUNT(*) FROM {related.table} WHERE {related.where.format(ids=_placeholders(len(chunk)))}"
        total += int(conn.execute(sql, tuple(chunk)).fetchone()[0])
    return total


def _remove(conn: sqlite3.Connection, related: Related, ids: Sequence[str]) -> int:
    """Delete (or unlink) a related table's rows for ``ids``.

    Args:
        conn: The connection, inside a write transaction.
        related: The rows.
        ids: The chat ids.

    Returns:
        How many rows were changed.
    """
    total = 0
    for chunk in _chunks(ids):
        where = related.where.format(ids=_placeholders(len(chunk)))
        if related.unlink is None:
            sql = f"DELETE FROM {related.table} WHERE {where}"
        else:
            sql = f"UPDATE {related.table} SET {related.unlink} WHERE {where}"
        total += conn.execute(sql, tuple(chunk)).rowcount
    return total


def _normalized_tag(value: str) -> str:
    """Turn a tag name into the id Open WebUI stores it under.

    Args:
        value: A tag name or id.

    Returns:
        The id: spaces as underscores, lowercase.
    """
    return value.replace(" ", "_").lower()


def _tags_of(conn: sqlite3.Connection, ids: Sequence[str]) -> set[str]:
    """Collect the tag ids the chats ``ids`` carry in ``meta.tags``.

    Args:
        conn: The connection.
        ids: The chat ids.

    Returns:
        The tag ids.
    """
    tags: set[str] = set()
    for chunk in _chunks(ids):
        sql = (
            "SELECT tags.value FROM chat, json_each(CASE WHEN json_valid(chat.meta) THEN chat.meta ELSE '{}' END, "
            f"'$.tags') AS tags WHERE tags.type = 'text' AND chat.id IN ({_placeholders(len(chunk))})"
        )
        tags.update(_normalized_tag(str(row[0])) for row in conn.execute(sql, tuple(chunk)))
    return tags


def _tag_usage(conn: sqlite3.Connection, owner: str | None) -> dict[str, set[str]]:
    """Map each tag id of an owner's visible chats to the chats carrying it.

    Internal chats do not count, as in upstream's own orphan check.

    Args:
        conn: The connection.
        owner: The owner.

    Returns:
        Tag id to chat ids.
    """
    sql = (
        "SELECT chat.id, tags.value FROM chat, json_each(CASE WHEN json_valid(chat.meta) THEN chat.meta ELSE '{}' "
        "END, '$.tags') AS tags WHERE tags.type = 'text' AND chat.user_id IS ? "
        f"AND NOT ({_json('$.internal', 'chat.meta')} IS 1)"
    )
    usage: dict[str, set[str]] = {}
    for chat_id, value in conn.execute(sql, (owner,)):
        usage.setdefault(_normalized_tag(str(value)), set()).add(str(chat_id))
    return usage


def _orphaned_tags(usage: Mapping[str, set[str]], candidates: Iterable[str], deleted: set[str]) -> list[str]:
    """Pick the candidate tags no chat outside ``deleted`` still carries.

    Args:
        usage: Tag id to chat ids, from :func:`_tag_usage`.
        candidates: The tags the deleted chats carried.
        deleted: The chats being deleted.

    Returns:
        The orphaned tag ids, sorted.
    """
    return sorted(tag for tag in set(candidates) if not usage.get(tag, set()) - deleted)


@dataclass(frozen=True)
class Family:
    """An expired chat and the internal chats that go with it.

    Attributes:
        root: The expired chat.
        ids: The root first, then its internal descendants.
        started_at: When the root was started, in seconds since the epoch.
    """

    root: str
    ids: tuple[str, ...]
    started_at: int | None


def list_owners(conn: sqlite3.Connection, cutoff: int) -> list[str | None]:
    """List the accounts that have chats started before the cutoff, or note chats without their note.

    Args:
        conn: The connection.
        cutoff: Seconds since the epoch.

    Returns:
        Their user ids, sorted (``None`` for chats without an owner, first).
    """
    # Two queries instead of one with OR, so each is answered from an index
    # without reading the chat rows, which hold whole conversations.
    owners: set[str | None] = set()
    for sql in (
        "SELECT DISTINCT user_id FROM chat WHERE created_at < ?",
        "SELECT DISTINCT user_id FROM chat WHERE created_at IS NULL AND updated_at < ?",
    ):
        owners.update(None if row[0] is None else str(row[0]) for row in conn.execute(sql, (cutoff,)))
    owners.update(
        None if row[0] is None else str(row[0])
        for row in conn.execute(f"SELECT DISTINCT user_id FROM chat WHERE {_ORPHANED_NOTE_CHAT}")
    )
    return sorted(owners, key=lambda owner: (owner is not None, owner or ""))


def plan_owner(conn: sqlite3.Connection, owner: str | None, cutoff: int) -> list[Family]:
    """Work out which of an owner's chats expire, each with its internal chats.

    Reads without a lock; :class:`ChatSweeper` checks every family again inside
    the transaction that deletes it.

    Args:
        conn: The connection.
        owner: The owner.
        cutoff: Seconds since the epoch.

    Returns:
        The expired chats, oldest first, each with its descendants. Internal
        chats whose parent still exists are not listed on their own.
    """
    owned = {str(row[0]) for row in conn.execute("SELECT id FROM chat WHERE user_id IS ?", (owner,))}
    parents: dict[str, str] = {}
    for chat_id, parent in conn.execute(
        f"SELECT id, {_json('$.parent_chat_id')} FROM chat WHERE user_id IS ? AND {_INTERNAL}", (owner,)
    ):
        if isinstance(parent, str) and parent in owned and parent != str(chat_id):
            parents[str(chat_id)] = parent
    children: dict[str, list[str]] = {}
    for child, parent in sorted(parents.items()):
        children.setdefault(parent, []).append(child)
    families: list[Family] = []
    rows = conn.execute(
        f"SELECT id, {_STARTED} FROM chat WHERE user_id IS ? AND {_EXPIRED} ORDER BY {_STARTED}, id",
        (owner, cutoff, cutoff),
    )
    for chat_id, started_at in rows:
        root = str(chat_id)
        if root in parents:
            continue
        ids = [root]
        seen = {root}
        queue = list(children.get(root, []))
        while queue:
            child = queue.pop(0)
            if child in seen:
                continue
            seen.add(child)
            ids.append(child)
            queue.extend(children.get(child, []))
        families.append(Family(root, tuple(ids), None if started_at is None else int(started_at)))
    return families


def batches(families: Sequence[Family]) -> Iterator[list[Family]]:
    """Group families into batches of at most :data:`ROOTS_PER_BATCH` and :data:`IDS_PER_BATCH`.

    A family larger than a batch is a batch of its own.

    Args:
        families: An owner's families, oldest first.

    Yields:
        The batches.
    """
    batch: list[Family] = []
    size = 0
    for family in families:
        if batch and (len(batch) == ROOTS_PER_BATCH or size + len(family.ids) > IDS_PER_BATCH):
            yield batch
            batch = []
            size = 0
        batch.append(family)
        size += len(family.ids)
    if batch:
        yield batch


@dataclass
class ChatPreview:
    """What a period would delete now (the dry run).

    Attributes:
        chats: Chats that would be deleted, internal ones included.
        internal: How many of them are internal chats going with their parent.
        owners: The accounts they belong to.
        oldest: When the earliest-started one was started.
        newest: When the latest-started one was started.
        related: The related rows, by criteria key.
        tags: Tags no remaining chat would use.
        kept: Chats that would stay.
    """

    chats: int = 0
    internal: int = 0
    owners: int = 0
    oldest: int | None = None
    newest: int | None = None
    related: dict[str, int] = field(default_factory=dict)
    tags: int = 0
    kept: int = 0


def preview(conn: sqlite3.Connection, cutoff: int) -> ChatPreview:
    """Count what a sweep with this cutoff would delete; change nothing.

    Args:
        conn: A read-only connection.
        cutoff: Seconds since the epoch.

    Returns:
        The counts.
    """
    result = ChatPreview(related={related.key: 0 for related in RELATED})
    total = int(conn.execute("SELECT COUNT(*) FROM chat").fetchone()[0])
    for owner in list_owners(conn, cutoff):
        families = plan_owner(conn, owner, cutoff)
        if not families:
            continue
        ids = [chat_id for family in families for chat_id in family.ids]
        result.owners += 1
        result.chats += len(ids)
        result.internal += len(ids) - len(families)
        for family in families:
            if family.started_at is not None:
                result.oldest = min(result.oldest or family.started_at, family.started_at)
                result.newest = max(result.newest or family.started_at, family.started_at)
        for related in RELATED:
            result.related[related.key] += _count(conn, related, ids)
        result.tags += len(_orphaned_tags(_tag_usage(conn, owner), _tags_of(conn, ids), set(ids)))
    result.kept = total - result.chats
    return result


@dataclass
class ChatSweep:
    """What one chat sweep did.

    Attributes:
        chats: Chats deleted, internal ones included.
        owners: The accounts they belonged to.
        batches: Write transactions committed.
        complete: Whether everything expired was handled (else the batch cap
            or a stop ended the sweep early).
        database: Whether the database existed.
        log_emptied: Whether the write-ahead log was emptied after the
            deletions; ``None`` when nothing was deleted.
    """

    chats: int = 0
    owners: int = 0
    batches: int = 0
    complete: bool = True
    database: bool = True
    log_emptied: bool | None = None


class ChatSweeper:
    """Deletes expired chats in bounded, recorded transactions."""

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

    def sweep(self, now: float) -> ChatSweep:
        """Delete every chat started more than the period ago, oldest first, per owner.

        Args:
            now: The sweep's time, in seconds since the epoch.

        Returns:
            What was deleted.

        Raises:
            SchemaRefused: If the database is not the verified schema; nothing
                has been deleted in that transaction.
            sqlite3.Error: If the database could not be read or written; the
                failed batch was rolled back (and recorded as failed if its
                commit failed).
            AuditWriteError: If a record could not be written; its batch was
                rolled back.
            RuntimeError: If the emitter is closed.
        """
        result = ChatSweep()
        if not self._database.is_file():
            result.database = False
            return result
        cutoff = int(now) - self._days * 86_400
        conn = open_database(self._database, readonly=False, busy_timeout_s=self._busy_timeout_s)
        try:
            problems = schema_problems(conn)
            if problems:
                raise SchemaRefused(problems)
            for owner in list_owners(conn, cutoff):
                deleted_for_owner = 0
                for batch in batches(plan_owner(conn, owner, cutoff)):
                    if result.batches >= self._max_batches or self._stop.is_set():
                        result.complete = False
                        break
                    deleted = self._delete_batch(conn, owner, batch, cutoff)
                    if deleted:
                        result.batches += 1
                        result.chats += deleted
                        deleted_for_owner += deleted
                    self._stop.wait(self._pause_s)
                if deleted_for_owner:
                    result.owners += 1
                if not result.complete:
                    break
            if result.chats:
                result.log_emptied = truncate_wal(conn)
                if not result.log_emptied:
                    LOG.warning(
                        "chats: the write-ahead log stayed busy, so earlier versions of the deleted rows may stay "
                        "in it until a later checkpoint"
                    )
        finally:
            conn.close()
        if result.chats:
            LOG.info(
                "chats: deleted %s of %s started more than %s ago, in %s",
                counted(result.chats, "chats"),
                counted(result.owners, "accounts"),
                counted(self._days, "days"),
                counted(result.batches, "batches"),
            )
        return result

    def _delete_batch(self, conn: sqlite3.Connection, owner: str | None, batch: Sequence[Family], cutoff: int) -> int:
        """Delete one batch of an owner's chats in one recorded transaction.

        Args:
            conn: The connection, outside any transaction.
            owner: The owner.
            batch: The families to delete.
            cutoff: Seconds since the epoch.

        Returns:
            How many chats were deleted.
        """
        rule: dict[str, object] = {"store": "chats", "period_days": self._days, "cutoff": iso_utc(cutoff)}
        conn.execute("BEGIN IMMEDIATE")
        try:
            problems = schema_problems(conn)
            if problems:
                raise SchemaRefused(problems)
            # Planned without a lock: only chats still due (and still the owner's) go.
            roots = [family.root for family in batch]
            still = _existing(
                conn,
                f"SELECT id FROM chat WHERE user_id IS ? AND {_EXPIRED} AND id IN ({{ids}})",
                roots,
                (owner, cutoff, cutoff),
            )
            wanted = [chat_id for family in batch if family.root in still for chat_id in family.ids]
            present = _existing(conn, "SELECT id FROM chat WHERE user_id IS ? AND id IN ({ids})", wanted, (owner,))
            ids = list(dict.fromkeys(chat_id for chat_id in wanted if chat_id in present))
            if not ids:
                conn.execute("ROLLBACK")
                return 0
            tags = _tags_of(conn, ids)
            counts = {related.key: _remove(conn, related, ids) for related in RELATED}
            chats = 0
            for chunk in _chunks(ids):
                chats += conn.execute(
                    f"DELETE FROM chat WHERE user_id IS ? AND id IN ({_placeholders(len(chunk))})", (owner, *chunk)
                ).rowcount
            orphans = _orphaned_tags(_tag_usage(conn, owner), tags, set(ids))
            removed_tags = 0
            for chunk in _chunks(orphans):
                removed_tags += conn.execute(
                    f"DELETE FROM tag WHERE user_id IS ? AND id IN ({_placeholders(len(chunk))})", (owner, *chunk)
                ).rowcount
            # The owner by Open WebUI's opaque account id only: the record needs
            # the reference, not the person's address.
            criteria: dict[str, object] = {**rule, "owner": owner, "internal_chats": len(ids) - len(still)}
            criteria.update(counts)
            criteria["tags"] = removed_tags
            record = emit_deletion(self._emitter, "chat", ids, criteria)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            LOG.error("chats: a batch of %s could not be committed and was rolled back", counted(len(ids), "chats"))
            emit_deletion(
                self._emitter,
                "chat",
                ids,
                {**rule, "owner": owner},
                outcome="error",
                parent_event_id=str(record["event_id"]),
            )
            raise
        return chats
