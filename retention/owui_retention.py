"""Open WebUI retention: deletes what Open WebUI itself never deletes, and records it.

The ``retention`` service of ``docker/compose.yaml`` runs this module next to
Open WebUI, on the same data volume and as the same user (CLAUDE.md
§ Retention). It sweeps:

* the audio caches (``audio_caches.py``): speech-to-text recordings with their
  transcripts, and text-to-speech output, deleted once they are older than
  ``OPEN_WEBUI_AUDIO_RETENTION_MINUTES`` (default 60; ``off`` stops it);
* chats (``chat_retention.py``), deleted with everything that belongs only to
  them once they have not changed for ``OPEN_WEBUI_CHAT_RETENTION_DAYS``
  (unset or ``off`` by default: no chat is deleted).

Every deletion is a ``delete`` / ``retention.sweep`` event of the federation's
data-protection audit log (``audit_event.py``), written by the system actor
before the files go and inside the transaction that deletes the chats. A
record that cannot be written stops the service: nothing more is deleted until
the container, restarted, can record again.

Subcommands:

``serve``
    The long-running service: one round at start, then every
    ``OPEN_WEBUI_RETENTION_INTERVAL_S`` seconds (default 300) for the audio
    caches; chats at start and then hourly.
``dry-run [--audio-minutes N|off] [--chat-days N|off]``
    Report what the configured periods (or the ones given) would delete now:
    counts and ages, never names or content. Records and deletes nothing.
``schema [--database PATH]``
    Exit 0 when the database is the schema chat retention was verified against.
``health``
    Exit 0 when every configured sweep ran recently and none is refused (the
    compose healthcheck).
``verify``
    Check the hash chains in audit lines read from stdin, e.g. this container's
    ``docker compose logs``.

Stdlib only; Python 3.12.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Final, TextIO

from audio_caches import AUDIO_CACHES, CacheRefused, scan_cache, sweep_cache
from audit_event import STREAM_MARKER, AuditEmitter, AuditWriteError, verify_chain
from audit_runtime import open_audit_stream
from chat_retention import ChatSweeper, SchemaRefused, open_database, preview, schema_problems
from note_retention import NS, NoteSweeper, count_notes
from retention_state import STATE_NAME, RetentionState, StateRefused, deletion_allowed
from sweep_events import SERVICE, SYSTEM, counted, iso_utc

LOG = logging.getLogger("retention")

DEFAULT_DATA_DIR: Final = Path("/data")
DEFAULT_VERSION_FILE: Final = Path("/etc/open-webui-service/VERSION")
DEFAULT_STATUS_FILE: Final = Path("/tmp/retention-status.json")
#: Open WebUI's default database (``DATABASE_URL`` unset), below the data volume.
DATABASE_NAME: Final = "webui.db"
DEFAULT_AUDIO_MINUTES: Final = 60
MAX_AUDIO_MINUTES: Final = 527_040
MAX_CHAT_DAYS: Final = 3_650
DEFAULT_CHAT_DAYS: Final = 365
DEFAULT_NOTICE_DAYS: Final = 30
MAX_NOTICE_DAYS: Final = 365
DEFAULT_INTERVAL_S: Final = 300
MIN_INTERVAL_S: Final = 10
MAX_INTERVAL_S: Final = 86_400
#: Chats are swept at start and then this often; retention in days needs no more.
CHAT_EVERY_S: Final = 3_600
CHATS: Final = "chats"
NOTES: Final = "notes"
#: The stores in Open WebUI's database, swept in this order at the chats'
#: schedule and by the chats' period, each with its own grace.
CONTENT_STORES: Final = (CHATS, NOTES)
STATUS_VERSION: Final = 1

_VERSION: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")


def parse_period(value: str | None, *, name: str, unit: str, default: int | None, maximum: int) -> int | None:
    """Parse a retention period.

    Args:
        value: Whole ``unit``, ``off``, or ``None``/blank for the default.
        name: The setting, for the error message.
        unit: ``minutes`` or ``days``.
        default: The period when unset; ``None`` means off.
        maximum: The longest period accepted.

    Returns:
        The period, or ``None`` when off.

    Raises:
        ValueError: If it is neither ``off`` nor a whole number from 1 to ``maximum``.
    """
    text = (value or "").strip().lower()
    if not text:
        return default
    if text == "off":
        return None
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= maximum:
        raise ValueError(f"{name} must be whole {unit} from 1 to {maximum}, or off")
    return int(text)


def parse_notice(value: str | None) -> int:
    """Parse ``RETENTION_NOTICE_DAYS``.

    Args:
        value: Whole days, or ``None``/blank for the default.

    Returns:
        The notice period in days.

    Raises:
        ValueError: If it is not a whole number from 1 to :data:`MAX_NOTICE_DAYS`.
    """
    days = parse_period(
        value, name="RETENTION_NOTICE_DAYS", unit="days", default=DEFAULT_NOTICE_DAYS, maximum=MAX_NOTICE_DAYS
    )
    if days is None:
        raise ValueError(f"RETENTION_NOTICE_DAYS must be whole days from 1 to {MAX_NOTICE_DAYS}, or off")
    return days


def period_since(settings: Settings, store: str, now: float) -> float | None:
    """Return when a store's period took effect, recording ``now`` for a new or changed period.

    Args:
        settings: The settings.
        store: One of :data:`CONTENT_STORES`.
        now: The current time.

    Returns:
        The time, or ``None`` when chat retention cannot run: it is off, Open
        WebUI keeps its own database, or has not created ``webui.db`` yet (no
        period is recorded then).

    Raises:
        StateRefused: If the state file cannot be used.
        OSError: If it cannot be written.
    """
    if settings.chat_days is None or settings.external_database or not settings.database.is_file():
        return None
    return RetentionState.load(settings.state_file).effective_since(store, settings.chat_days, now)


def parse_interval(value: str | None) -> int:
    """Parse ``OPEN_WEBUI_RETENTION_INTERVAL_S``.

    Args:
        value: Whole seconds, or ``None``/blank for the default.

    Returns:
        The interval.

    Raises:
        ValueError: If it is not a whole number in range.
    """
    text = (value or "").strip()
    if not text:
        return DEFAULT_INTERVAL_S
    if not text.isascii() or not text.isdigit() or not MIN_INTERVAL_S <= int(text) <= MAX_INTERVAL_S:
        raise ValueError(
            f"OPEN_WEBUI_RETENTION_INTERVAL_S must be whole seconds from {MIN_INTERVAL_S} to {MAX_INTERVAL_S}"
        )
    return int(text)


@dataclass(frozen=True)
class Settings:
    """The sidecar's configuration.

    Attributes:
        audio_minutes: The audio caches' period; ``None`` is off.
        chat_days: The chats' period; ``None`` is off (development only).
        notice_days: How long a period that took effect, or changed, deletes
            nothing (``RETENTION_NOTICE_DAYS``).
        interval_s: Seconds between rounds.
        external_database: Open WebUI was given its own ``DATABASE_URL``, so
            ``webui.db`` is not where its chats are.
        data_dir: Where Open WebUI's data volume is mounted.
        version_file: The repo's ``VERSION`` file.
        status_file: Where ``serve`` reports for ``health``.
    """

    audio_minutes: int | None = DEFAULT_AUDIO_MINUTES
    chat_days: int | None = DEFAULT_CHAT_DAYS
    notice_days: int = DEFAULT_NOTICE_DAYS
    interval_s: int = DEFAULT_INTERVAL_S
    external_database: bool = False
    data_dir: Path = DEFAULT_DATA_DIR
    version_file: Path = DEFAULT_VERSION_FILE
    status_file: Path = DEFAULT_STATUS_FILE

    @property
    def database(self) -> Path:
        """Open WebUI's SQLite database file."""
        return self.data_dir / DATABASE_NAME

    @property
    def state_file(self) -> Path:
        """Where the time each period took effect is kept (:mod:`retention_state`)."""
        return self.data_dir / STATE_NAME

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Read the operator's settings; paths are the container's.

        Args:
            environ: The process environment.

        Returns:
            The settings.

        Raises:
            ValueError: If a setting is not valid.
        """
        return cls(
            audio_minutes=parse_period(
                environ.get("OPEN_WEBUI_AUDIO_RETENTION_MINUTES"),
                name="OPEN_WEBUI_AUDIO_RETENTION_MINUTES",
                unit="minutes",
                default=DEFAULT_AUDIO_MINUTES,
                maximum=MAX_AUDIO_MINUTES,
            ),
            chat_days=parse_period(
                environ.get("OPEN_WEBUI_CHAT_RETENTION_DAYS"),
                name="OPEN_WEBUI_CHAT_RETENTION_DAYS",
                unit="days",
                default=DEFAULT_CHAT_DAYS,
                maximum=MAX_CHAT_DAYS,
            ),
            notice_days=parse_notice(environ.get("RETENTION_NOTICE_DAYS")),
            interval_s=parse_interval(environ.get("OPEN_WEBUI_RETENTION_INTERVAL_S")),
            external_database=(environ.get("RETENTION_EXTERNAL_DATABASE") or "").strip().lower() == "true",
        )


def read_version(path: Path) -> str:
    """Read the release this checkout is (the repo's ``VERSION`` file).

    Args:
        path: The mounted version file.

    Returns:
        The version, e.g. ``0.9.0``.

    Raises:
        ValueError: If the file is missing or holds no version.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read the open-webui-service version from {path}") from exc
    if _VERSION.fullmatch(text) is None:
        raise ValueError(f"{path} holds no version")
    return text


@dataclass
class StoreStatus:
    """How one store's sweeping stands.

    Attributes:
        state: ``off``, ``pending`` (not swept yet), ``ok``, ``error`` (the last
            attempt failed; it is retried every round) or ``refused`` (it will
            not run until fixed).
        last_ok: When it last ran to the end, in seconds since the epoch.
        reason: Why it is refused: ``schema``, ``external_database`` or
            ``not_a_directory``.
    """

    state: str
    last_ok: float | None = None
    reason: str | None = None


@dataclass
class Status:
    """What ``serve`` reports to ``health`` after every round.

    Attributes:
        written_at: When it was written.
        interval_s: Seconds between rounds.
        stores: By store name.
    """

    written_at: float
    interval_s: int
    stores: dict[str, StoreStatus] = field(default_factory=dict)


def write_status(path: Path, status: Status) -> None:
    """Replace the status file atomically.

    Args:
        path: The status file.
        status: The status.

    Raises:
        OSError: If it cannot be written.
    """
    data = {
        "version": STATUS_VERSION,
        "written_at": status.written_at,
        "interval_s": status.interval_s,
        "stores": {
            name: {"state": store.state, "last_ok": store.last_ok, "reason": store.reason}
            for name, store in status.stores.items()
        },
    }
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    temp.replace(path)


def stale_after(store: str, interval_s: int) -> float:
    """How long a store may go without a completed sweep and still count as healthy.

    Args:
        store: The store name.
        interval_s: Seconds between rounds.

    Returns:
        Seconds: three rounds (at least a minute), plus the chat schedule for chats.
    """
    grace = max(3 * interval_s, 60)
    return grace + CHAT_EVERY_S if store in CONTENT_STORES else grace


def health(settings: Settings, now: float | None = None) -> int:
    """Report whether every configured sweep ran recently and none is refused.

    Args:
        settings: The settings.
        now: The current time, for tests.

    Returns:
        0 when healthy, else 1 (with the reason on stderr).
    """
    moment = time.time() if now is None else now
    try:
        data = json.loads(settings.status_file.read_text(encoding="utf-8"))
        written_at = float(data["written_at"])
        interval_s = int(data["interval_s"])
        stores = data["stores"]
        if not isinstance(stores, dict):
            raise TypeError("stores")
    except (OSError, ValueError, KeyError, TypeError):
        print("retention: no status yet", file=sys.stderr)
        return 1
    if moment - written_at > max(3 * interval_s, 60):
        print("retention: the sweep loop has not reported recently", file=sys.stderr)
        return 1
    healthy = True
    for name, raw in sorted(stores.items()):
        state = raw.get("state") if isinstance(raw, dict) else None
        if state == "off":
            continue
        last_ok = raw.get("last_ok") if isinstance(raw, dict) else None
        if state == "refused":
            print(f"retention: {name} refused ({raw.get('reason')})", file=sys.stderr)
            healthy = False
        elif not isinstance(last_ok, int | float) or moment - last_ok > stale_after(str(name), interval_s):
            print(f"retention: {name} has not been swept recently", file=sys.stderr)
            healthy = False
    return 0 if healthy else 1


class Rounds:
    """The work of ``serve``: one round sweeps every configured store."""

    def __init__(self, emitter: AuditEmitter, settings: Settings, stop: threading.Event) -> None:
        """Prepare the rounds; nothing is swept until :meth:`run`.

        Args:
            emitter: The started emitter.
            settings: The settings.
            stop: Ends a sweep after its current batch when set.
        """
        self._emitter = emitter
        self._settings = settings
        self._stop = stop
        audio = "off" if settings.audio_minutes is None else "pending"
        self.stores: dict[str, StoreStatus] = {cache.store: StoreStatus(audio) for cache in AUDIO_CACHES}
        for store in CONTENT_STORES:
            self.stores[store] = StoreStatus("off" if settings.chat_days is None else "pending")
        self._next_content = 0.0
        self._grace_logged: set[str] = set()

    def run(self, now: float) -> None:
        """Sweep every configured store once (chats only when due).

        Args:
            now: The round's time.

        Raises:
            AuditWriteError: If a record could not be written.
            RuntimeError: If the emitter is closed.
        """
        minutes = self._settings.audio_minutes
        if minutes is not None:
            for cache in AUDIO_CACHES:
                if self._stop.is_set():
                    return
                try:
                    sweep_cache(self._emitter, self._settings.data_dir, cache, minutes, now, stop=self._stop)
                except CacheRefused as exc:
                    self._refuse(cache.store, "not_a_directory", str(exc))
                except OSError as exc:
                    self._fail(cache.store, exc)
                else:
                    self._ok(cache.store, now)
        days = self._settings.chat_days
        if days is None or now < self._next_content or self._stop.is_set():
            return
        complete = True
        for store in CONTENT_STORES:
            if self._stop.is_set():
                return
            complete = self._sweep_content(store, days, now) and complete
        self._next_content = now + CHAT_EVERY_S if complete else now

    def _sweep_content(self, store: str, days: int, now: float) -> bool:
        """Sweep one store of Open WebUI's database, once its period's notice has passed.

        Args:
            store: One of :data:`CONTENT_STORES`.
            days: The period.
            now: The round's time.

        Returns:
            Whether the store can wait for the next scheduled sweep: ``False``
            when it was refused, failed or stopped at the batch cap, so it is
            tried again next round (a refusal is recorded only once).

        Raises:
            AuditWriteError: If a record could not be written.
            RuntimeError: If the emitter is closed.
        """
        if self._settings.external_database:
            self._refuse(store, "external_database", "Open WebUI has its own DATABASE_URL; only webui.db is swept")
            return False
        if not self._settings.database.is_file():
            LOG.info("%s: Open WebUI has not created its database yet", store)
            self._ok(store, now)
            return True
        try:
            since = period_since(self._settings, store, now)
        except StateRefused as exc:
            self._refuse(store, "state", str(exc))
            return False
        except OSError as exc:
            self._fail(store, exc)
            return False
        notice = self._settings.notice_days
        if since is not None and not deletion_allowed(since=since, notice_days=notice, now=now):
            if store not in self._grace_logged:
                LOG.info(
                    "%s: the retention period takes effect on %s; until then nothing is deleted",
                    store,
                    iso_utc(since + notice * 86_400),
                )
                self._grace_logged.add(store)
            self._ok(store, now)
            return True
        sweeper = (ChatSweeper if store == CHATS else NoteSweeper)(
            self._emitter, self._settings.database, days, stop=self._stop
        )
        try:
            result = sweeper.sweep(now)
        except SchemaRefused as exc:
            self._refuse(store, "schema", "; ".join(exc.problems))
            return False
        except sqlite3.Error as exc:
            self._fail(store, exc)
            return False
        self._ok(store, now)
        return result.complete

    def _ok(self, store: str, now: float) -> None:
        """Note a completed sweep, and record the end of a refusal.

        Args:
            store: The store.
            now: The round's time.
        """
        status = self.stores[store]
        if status.state == "refused":
            LOG.info("%s: no longer refused; sweeping again", store)
            self._emitter.emit("admin", "retention.resumed", SYSTEM, criteria={"store": store})
        self.stores[store] = StoreStatus("ok", last_ok=now)

    def _fail(self, store: str, exc: Exception) -> None:
        """Note a failed attempt; it is retried next round.

        Args:
            store: The store.
            exc: What went wrong (logged, never recorded).
        """
        status = self.stores[store]
        LOG.warning("%s: sweep failed (%s); retrying next round", store, exc)
        if status.state != "refused":
            self.stores[store] = StoreStatus("error", last_ok=status.last_ok)

    def _refuse(self, store: str, reason: str, detail: str) -> None:
        """Stop sweeping a store until the cause is fixed, and record that once.

        Args:
            store: The store.
            reason: ``schema``, ``external_database``, ``not_a_directory`` or
                ``state`` (the state file cannot be used).
            detail: What is wrong, by table, column or path name only.
        """
        status = self.stores[store]
        if status.state != "refused":
            LOG.error("%s: retention refused, nothing is deleted from it: %s", store, detail)
            self._emitter.emit(
                "admin", "retention.refused", SYSTEM, outcome="error", criteria={"store": store, "reason": reason}
            )
        self.stores[store] = StoreStatus("refused", last_ok=status.last_ok, reason=reason)


def config_criteria(settings: Settings, since: Mapping[str, float | None] | None = None) -> dict[str, object]:
    """Describe the operating mode for the ``config.audit`` event.

    Args:
        settings: The settings.
        since: When each content store's period took effect, where known.

    Returns:
        The periods (``None`` when off), when the chat period took effect,
        the notice period and the schedule.
    """
    return {
        "audio_max_age_minutes": settings.audio_minutes,
        "chat_retention_days": settings.chat_days,
        "chat_retention_since": _since_text((since or {}).get(CHATS)),
        "note_retention_since": _since_text((since or {}).get(NOTES)),
        "notice_days": settings.notice_days,
        "interval_s": settings.interval_s,
        "chat_every_s": CHAT_EVERY_S,
        "chat_external_database": settings.external_database,
    }


def _since_text(moment: float | None) -> str | None:
    """Format when a period took effect, for the config event.

    Args:
        moment: The time, if known.

    Returns:
        The UTC time, or ``None``.
    """
    return None if moment is None else iso_utc(moment)


def serve(settings: Settings, audit_output: str | None, stop: threading.Event | None = None) -> int:
    """Sweep until SIGTERM/SIGINT (or ``stop``).

    Args:
        settings: The settings.
        audit_output: ``AUDIT_OUTPUT``.
        stop: Ends the loop when set; signal handlers set it in the container.

    Returns:
        The exit status: 0 after a clean stop, 1 when a record could not be
        written (the container restarts; nothing is deleted until it can
        record), 2 on bad configuration.
    """
    try:
        version = read_version(settings.version_file)
    except ValueError as exc:
        LOG.error("%s", exc)
        return 2
    try:
        stream = open_audit_stream(audit_output)
    except (OSError, ValueError) as exc:
        LOG.error("cannot open the audit stream (%s); nothing is deleted", exc)
        return 1
    stop = stop or threading.Event()
    emitter = AuditEmitter(SERVICE, version, stream=stream)
    status = 0
    try:
        emitter.start()
        since: dict[str, float | None] = {}
        for store in CONTENT_STORES:
            try:
                since[store] = period_since(settings, store, time.time())
            except (StateRefused, OSError):
                since[store] = None  # the first round refuses or retries the store, and says why
        emitter.emit("admin", "config.audit", SYSTEM, criteria=config_criteria(settings, since))
        LOG.info(
            "audio caches: %s; chats: %s",
            "off" if settings.audio_minutes is None else f"deleted after {counted(settings.audio_minutes, 'minutes')}",
            "off"
            if settings.chat_days is None
            else f"deleted {counted(settings.chat_days, 'days')} after they were started",
        )
        if settings.chat_days is None:
            LOG.warning("chat retention is off: no chat is ever deleted (OPEN_WEBUI_CHAT_RETENTION_DAYS=off)")
        rounds = Rounds(emitter, settings, stop)
        while not stop.is_set():
            now = time.time()
            rounds.run(now)
            try:
                write_status(settings.status_file, Status(now, settings.interval_s, rounds.stores))
            except OSError as exc:
                LOG.warning("cannot write the status file (%s)", exc)
            stop.wait(settings.interval_s)
    except (AuditWriteError, RuntimeError) as exc:
        LOG.error("an audit record could not be written (%s); stopping, nothing more is deleted", exc)
        status = 1
    try:
        emitter.close()
    except (AuditWriteError, RuntimeError):
        LOG.error("the shutdown event could not be written")
        status = 1
    return status


#: How the dry run names the rows deleted with a chat, by criteria key.
_RELATED_LABELS: Final = {
    "messages": "messages",
    "shares": "shared copies",
    "share_grants": "share access grants",
    "file_links": "file links",
    "comments": "comments",
    "comment_reactions": "comment reactions",
    "notifications": "notifications",
    "feedback": "ratings",
    "legacy_tag_links": "legacy tag links",
}


def _age(seconds: float) -> str:
    """Format an age for the dry run.

    Args:
        seconds: The age.

    Returns:
        E.g. ``45 min``, ``5 h 10 min`` or ``93 d``.
    """
    minutes = max(int(seconds // 60), 0)
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 2 * 1440:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes // 1440} d"


def _size(count: int) -> str:
    """Format a byte count for the dry run.

    Args:
        count: Bytes.

    Returns:
        E.g. ``512 B`` or ``3.4 MiB``.
    """
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{count} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{count} B"


def _ages(now: float, newest: float, oldest: float) -> str:
    """Describe how long ago the most and least recently changed items changed.

    Args:
        now: The report's time.
        newest: The most recent change.
        oldest: The oldest change.

    Returns:
        E.g. ``last changed 2 h 0 min to 3 h 0 min ago``.
    """
    if _age(now - newest) == _age(now - oldest):
        return f"last changed {_age(now - newest)} ago"
    return f"last changed {_age(now - newest)} to {_age(now - oldest)} ago"


def _dry_run_audio(settings: Settings, minutes: int | None, now: float, out: TextIO) -> bool:
    """Report the audio caches.

    Args:
        settings: The settings.
        minutes: The period to evaluate; ``None`` is off.
        now: The report's time.
        out: Where the report goes.

    Returns:
        Whether every cache could be evaluated.
    """
    complete = True
    for cache in AUDIO_CACHES:
        print(f"\n{cache.label} ({cache.relative})", file=out)
        if minutes is None:
            print("  retention: off; nothing is deleted", file=out)
            continue
        cutoff = now - minutes * 60
        print(
            f"  rule: files older than {counted(minutes, 'minutes')} (last changed before {iso_utc(cutoff)})", file=out
        )
        try:
            scan = scan_cache(settings.data_dir, cache, cutoff)
        except (CacheRefused, OSError) as exc:
            print(f"  cannot be evaluated: {exc}", file=out)
            complete = False
            continue
        if scan.expired:
            size = sum(item.size for item in scan.expired)
            oldest, newest = scan.expired[0].mtime, scan.expired[-1].mtime
            print(
                f"  would delete: {counted(len(scan.expired), 'files')}, {_size(size)}, {_ages(now, newest, oldest)}",
                file=out,
            )
        else:
            print("  would delete: nothing", file=out)
        print(f"  keeps: {counted(scan.kept, 'newer files')}", file=out)
    return complete


def _dry_run_chats(settings: Settings, days: int | None, now: float, out: TextIO) -> bool:
    """Report the chats.

    Args:
        settings: The settings.
        days: The period to evaluate; ``None`` is off.
        now: The report's time.
        out: Where the report goes.

    Returns:
        Whether the chats could be evaluated.
    """
    print(f"\nChats ({DATABASE_NAME})", file=out)
    if days is None:
        print("  retention: off; nothing is deleted", file=out)
        print("  (make retention-dry-run CHAT_DAYS=<n> shows what a period would delete)", file=out)
        return True
    if settings.external_database:
        print("  refused: Open WebUI has its own DATABASE_URL, and only webui.db is swept", file=out)
        return False
    cutoff = int(now) - days * 86_400
    print(f"  rule: chats started more than {counted(days, 'days')} ago (before {iso_utc(cutoff)})", file=out)
    try:
        state = RetentionState.load(settings.state_file)
    except StateRefused as exc:
        print(f"  refused: {exc}", file=out)
        return False
    entry = state.stores.get(CHATS)
    since = float(entry["since"]) if entry is not None and entry["days"] == days else now
    start = since + settings.notice_days * 86_400
    if now < start:
        print(f"  deletion starts: {iso_utc(start)} (the notice period after the period took effect)", file=out)
    if not settings.database.is_file():
        print("  would delete: nothing (Open WebUI has not created its database yet)", file=out)
        return True
    try:
        conn = open_database(settings.database, readonly=True)
        try:
            problems = schema_problems(conn)
            if problems:
                print("  refused: the database is not the schema this release was verified against:", file=out)
                for problem in problems:
                    print(f"    - {problem}", file=out)
                return False
            counts = preview(conn, cutoff)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"  cannot be evaluated: {exc}", file=out)
        return False
    if counts.chats:
        ages = ""
        if counts.oldest is not None and counts.newest is not None:
            ages = ", " + _ages(now, counts.newest, counts.oldest).replace("last changed", "started", 1)
        print(
            f"  would delete: {counted(counts.chats, 'chats')} of {counted(counts.owners, 'accounts')}{ages}", file=out
        )
        print(f"    of them internal chats going with their parent: {counts.internal}", file=out)
        related = ", ".join(f"{_RELATED_LABELS[key]} {counts.related[key]}" for key in _RELATED_LABELS)
        print(f"    with them: {related}, tags no other chat uses {counts.tags}", file=out)
        unlinked = counts.related["automation_runs_unlinked"]
        print(f"    automation runs that keep their record and lose the link: {unlinked}", file=out)
    else:
        print("  would delete: nothing", file=out)
    print(f"  keeps: {counted(counts.kept, 'chats')}", file=out)
    return True


def _dry_run_notes(settings: Settings, days: int | None, now: float, out: TextIO) -> bool:
    """Report the notes (switched off; those written before are swept with the chats' period).

    Args:
        settings: The settings.
        days: The period to evaluate; ``None`` is off.
        now: The report's time.
        out: Where the report goes.

    Returns:
        Whether the notes could be evaluated.
    """
    print(f"\nNotes ({DATABASE_NAME}; switched off, those written before)", file=out)
    if days is None:
        print("  retention: off; nothing is deleted", file=out)
        return True
    if settings.external_database or not settings.database.is_file():
        print("  would delete: nothing (no webui.db to sweep)", file=out)
        return not settings.external_database
    cutoff = int(now) - days * 86_400
    try:
        conn = open_database(settings.database, readonly=True)
        try:
            if schema_problems(conn):
                print("  refused: the database is not the schema this release was verified against", file=out)
                return False
            due, kept = count_notes(conn, cutoff * NS)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"  cannot be evaluated: {exc}", file=out)
        return False
    print(f"  would delete: {counted(due, 'notes')}" if due else "  would delete: nothing", file=out)
    print(f"  keeps: {counted(kept, 'notes')}", file=out)
    return True


def dry_run(
    settings: Settings,
    out: TextIO,
    *,
    audio_minutes: int | None,
    chat_days: int | None,
    now: float | None = None,
) -> int:
    """Report what the periods would delete now; record and delete nothing.

    Args:
        settings: The settings (where the data volume is).
        out: Where the report goes.
        audio_minutes: The audio period to evaluate; ``None`` is off.
        chat_days: The chat period to evaluate; ``None`` is off.
        now: The report's time, for tests.

    Returns:
        0 when everything could be evaluated, else 1.
    """
    moment = time.time() if now is None else now
    print(f"Open WebUI retention, dry run at {iso_utc(moment)}: nothing is deleted or recorded.", file=out)
    audio_ok = _dry_run_audio(settings, audio_minutes, moment, out)
    chats_ok = _dry_run_chats(settings, chat_days, moment, out)
    notes_ok = _dry_run_notes(settings, chat_days, moment, out)
    return 0 if audio_ok and chats_ok and notes_ok else 1


def schema_check(database: Path, out: TextIO) -> int:
    """Check a database against the schema chat retention was verified with.

    Args:
        database: The database file.
        out: Where the verdict goes.

    Returns:
        0 when it matches, else 1.
    """
    if not database.is_file():
        print(f"no database at {database}", file=out)
        return 1
    try:
        conn = open_database(database, readonly=True)
        try:
            problems = schema_problems(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"cannot read the database: {exc}", file=out)
        return 1
    if problems:
        print("the database is not the schema chat retention was verified against:", file=out)
        for problem in problems:
            print(f"  - {problem}", file=out)
        return 1
    print("the database is the schema chat retention was verified against", file=out)
    return 0


def verify(lines: Iterable[str], out: TextIO) -> int:
    """Check the hash chains of the audit lines among ``lines``.

    Args:
        lines: Log lines; only those carrying the audit marker are read.
        out: Where the summary goes.

    Returns:
        0 when there were events and every chain verified, else 1.
    """
    marker = f'"stream":"{STREAM_MARKER}"'
    events: list[object] = []
    for line in lines:
        if marker not in line:
            continue
        start = line.find("{")
        try:
            events.append(json.loads(line[start:] if start > 0 else line))
        except ValueError:
            events.append(line)  # reported as a schema problem
    report = verify_chain(events)
    print(f"{report.events} audit events in {len(report.heads)} chains, {len(report.problems)} problems", file=out)
    for problem in report.problems:
        print(f"  {problem.kind}: {problem.service} {problem.instance} seq {problem.seq}: {problem.detail}", file=out)
    return 0 if report.events and report.ok else 1


def _install_stop_handlers(stop: threading.Event) -> None:
    """End ``serve`` cleanly on SIGTERM (``docker stop``) and SIGINT.

    As PID 1, the process would otherwise ignore SIGTERM.

    Args:
        stop: The event ``serve`` waits on.
    """

    def handler(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a subcommand.

    Args:
        argv: The arguments, without the program name.

    Returns:
        The exit status.
    """
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        prog="owui_retention.py", description="Open WebUI retention (CLAUDE.md: Retention)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="sweep until stopped")
    report = commands.add_parser("dry-run", help="report what the periods would delete now; change nothing")
    report.add_argument("--audio-minutes", help="evaluate this audio period instead of the configured one (or off)")
    report.add_argument("--chat-days", help="evaluate this chat period instead of the configured one (or off)")
    check = commands.add_parser("schema", help="exit 0 when the database is the verified schema")
    check.add_argument("--database", type=Path, help=f"the database file (default: the data volume's {DATABASE_NAME})")
    commands.add_parser("health", help="exit 0 when every configured sweep ran recently")
    commands.add_parser("verify", help="check the hash chains of the audit lines on stdin")
    args = parser.parse_args(argv)
    command: str = args.command
    try:
        settings = Settings.from_env(os.environ)
    except ValueError as exc:
        LOG.error("%s", exc)
        return 2
    if command == "serve":
        stop = threading.Event()
        _install_stop_handlers(stop)
        return serve(settings, os.environ.get("AUDIT_OUTPUT"), stop)
    if command == "dry-run":
        try:
            audio = settings.audio_minutes
            if args.audio_minutes is not None:
                audio = parse_period(
                    args.audio_minutes, name="--audio-minutes", unit="minutes", default=None, maximum=MAX_AUDIO_MINUTES
                )
            chats = settings.chat_days
            if args.chat_days is not None:
                chats = parse_period(
                    args.chat_days, name="--chat-days", unit="days", default=None, maximum=MAX_CHAT_DAYS
                )
        except ValueError as exc:
            print(f"retention: {exc}", file=sys.stderr)
            return 2
        return dry_run(settings, sys.stdout, audio_minutes=audio, chat_days=chats)
    if command == "schema":
        database: Path | None = args.database
        return schema_check(database or settings.database, sys.stdout)
    if command == "health":
        return health(settings)
    return verify(sys.stdin, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
