"""Open WebUI's audio caches: the files the sweeper deletes and how it records them.

Open WebUI v0.13.0 keeps two audio caches on its data volume and deletes from
neither (``backend/open_webui/routers/audio/__init__.py``):

``cache/audio/transcriptions``
    Every recording sent to speech-to-text, as ``<id>.<ext>``; the copies it
    makes on the way (``<id>.mp3``, ``<id>_compressed.mp3``, chunks); and each
    transcript as ``<name>.json`` beside them.
``cache/audio/speech``
    Text-to-speech output, ``<sha256>.mp3`` or ``.wav``, with the request that
    produced it, including the text spoken, as ``<sha256>.json`` (and in the
    slim image its media type as ``<sha256>.mime``).

Every regular file in these directories last changed before the cutoff is
deleted, whatever its name; directories, symlinks and anything else are left
alone. Files have no transactions, so each batch is recorded first and deleted
after, and whatever could not be deleted is recorded again with
``outcome="error"``. The directory is opened once and every entry is listed and
unlinked relative to it, so a path swapped underneath cannot redirect a
deletion. Stdlib only.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from audit_event import AuditEmitter
from sweep_events import counted, emit_deletion, iso_utc

LOG = logging.getLogger("retention")

#: Files per record: one event's worth of ids, so no batch needs a continuation.
FILE_BATCH: Final = 200


@dataclass(frozen=True)
class AudioCache:
    """One cache directory on Open WebUI's data volume.

    Attributes:
        store: The name records and the status use, e.g. ``audio.transcriptions``.
        relative: Its path below the volume's root.
        label: How the dry run names it.
    """

    store: str
    relative: str
    label: str


AUDIO_CACHES: Final = (
    AudioCache("audio.transcriptions", "cache/audio/transcriptions", "Speech-to-text recordings and transcripts"),
    AudioCache("audio.speech", "cache/audio/speech", "Text-to-speech output"),
)


class CacheRefused(Exception):
    """Raised when a cache directory is not a plain directory on the volume."""


@dataclass(frozen=True)
class CacheFile:
    """A regular file in a cache directory.

    Attributes:
        name: Its name in the directory.
        mtime: When it was last changed, in seconds since the epoch.
        size: Its size in bytes.
    """

    name: str
    mtime: float
    size: int


@dataclass(frozen=True)
class CacheScan:
    """What a cache directory holds, measured against a cutoff.

    Attributes:
        expired: The files last changed before the cutoff, oldest first.
        kept: How many regular files are newer.
        skipped: Entries that are not regular files (left alone).
    """

    expired: list[CacheFile] = field(default_factory=list)
    kept: int = 0
    skipped: int = 0


@dataclass
class CacheSweep:
    """What one sweep of a cache did.

    Attributes:
        deleted: Files deleted.
        failed: Files recorded for deletion that could not be deleted.
        bytes_deleted: Their combined size.
    """

    deleted: int = 0
    failed: int = 0
    bytes_deleted: int = 0


class _OpenDirectory:
    """A cache directory opened for listing and unlinking relative to itself."""

    def __init__(self, data_dir: Path, cache: AudioCache) -> None:
        """Open the directory, refusing one that is not where it should be.

        Args:
            data_dir: The root of Open WebUI's data volume.
            cache: The cache.

        Raises:
            FileNotFoundError: If Open WebUI has not created the directory yet.
            CacheRefused: If the directory is a symlink or resolves outside the
                volume.
            OSError: If it cannot be opened.
        """
        path = data_dir / cache.relative
        root = data_dir.resolve()
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise CacheRefused(f"{cache.relative} is not a directory on the data volume")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            self.fd = os.open(path, flags)
        except NotADirectoryError:
            raise CacheRefused(f"{cache.relative} is not a directory on the data volume") from None

    def __enter__(self) -> _OpenDirectory:
        """Use the directory.

        Returns:
            Itself.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the directory.

        Args:
            *exc: The exception, if any (not suppressed).
        """
        os.close(self.fd)

    def scan(self, cutoff: float) -> CacheScan:
        """List the directory's regular files against a cutoff.

        Args:
            cutoff: Files last changed before this (seconds since the epoch) expire.

        Returns:
            The scan.
        """
        expired: list[CacheFile] = []
        kept = skipped = 0
        with os.scandir(self.fd) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    skipped += 1
                elif info.st_mtime < cutoff:
                    expired.append(CacheFile(entry.name, info.st_mtime, info.st_size))
                else:
                    kept += 1
        expired.sort(key=lambda item: (item.mtime, item.name))
        return CacheScan(expired=expired, kept=kept, skipped=skipped)

    def unlink(self, name: str) -> None:
        """Delete one entry of the directory.

        Args:
            name: The entry's name.

        Raises:
            OSError: If it could not be deleted (a file already gone is not an error).
        """
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            pass


def scan_cache(data_dir: Path, cache: AudioCache, cutoff: float) -> CacheScan:
    """Measure a cache against a cutoff without changing anything (the dry run).

    Args:
        data_dir: The root of Open WebUI's data volume.
        cache: The cache.
        cutoff: Files last changed before this expire.

    Returns:
        The scan; empty when Open WebUI has not created the directory yet.

    Raises:
        CacheRefused: If the directory is not a plain directory on the volume.
        OSError: If it cannot be read.
    """
    try:
        directory = _OpenDirectory(data_dir, cache)
    except FileNotFoundError:
        return CacheScan()
    with directory:
        return directory.scan(cutoff)


def _batches(files: list[CacheFile], size: int) -> Iterator[list[CacheFile]]:
    """Split the expired files into batches, oldest first.

    Args:
        files: The files.
        size: The most files per batch.

    Yields:
        The batches.
    """
    for start in range(0, len(files), size):
        yield files[start : start + size]


def sweep_cache(
    emitter: AuditEmitter,
    data_dir: Path,
    cache: AudioCache,
    minutes: int,
    now: float,
    *,
    batch: int = FILE_BATCH,
    stop: threading.Event | None = None,
) -> CacheSweep:
    """Delete a cache's files older than ``minutes``, recording each batch first.

    Args:
        emitter: The started emitter.
        data_dir: The root of Open WebUI's data volume.
        cache: The cache.
        minutes: The age at which a file is deleted.
        now: The sweep's time, in seconds since the epoch.
        batch: The most files per record.
        stop: Ends the sweep after the current batch when set.

    Returns:
        What was deleted.

    Raises:
        CacheRefused: If the directory is not a plain directory on the volume.
        OSError: If the directory cannot be read.
        AuditWriteError: If a record could not be written. Nothing of its batch
            has been deleted then.
        RuntimeError: If the emitter is closed.
    """
    cutoff = now - minutes * 60
    rule: dict[str, object] = {"store": cache.store, "max_age_minutes": minutes, "cutoff": iso_utc(cutoff)}
    result = CacheSweep()
    try:
        directory = _OpenDirectory(data_dir, cache)
    except FileNotFoundError:
        return result
    with directory:
        scan = directory.scan(cutoff)
        for files in _batches(scan.expired, batch):
            if stop is not None and stop.is_set():
                break
            size = sum(item.size for item in files)
            record = emit_deletion(
                emitter,
                "file",
                [item.name for item in files],
                {**rule, "bytes": size},
                container=cache.relative,
            )
            failed: list[CacheFile] = []
            for item in files:
                try:
                    directory.unlink(item.name)
                except OSError:
                    failed.append(item)
            if failed:
                LOG.warning("%s: %d of %s could not be deleted", cache.store, len(failed), counted(len(files), "files"))
                emit_deletion(
                    emitter,
                    "file",
                    [item.name for item in failed],
                    rule,
                    container=cache.relative,
                    outcome="error",
                    parent_event_id=str(record["event_id"]),
                )
            result.deleted += len(files) - len(failed)
            result.failed += len(failed)
            result.bytes_deleted += size - sum(item.size for item in failed)
    if result.deleted:
        LOG.info(
            "%s: deleted %s (%d bytes) older than %s",
            cache.store,
            counted(result.deleted, "files"),
            result.bytes_deleted,
            counted(minutes, "minutes"),
        )
    return result
