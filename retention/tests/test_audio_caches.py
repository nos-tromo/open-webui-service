"""Tests for audio_caches.py against synthetic cache directories.

Run from the repo root: ``python3 -m unittest discover -s retention/tests -t retention``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, override
from unittest import mock

import audio_caches
from audio_caches import AUDIO_CACHES, CacheRefused, scan_cache, sweep_cache
from audit_event import AuditWriteError

from tests.synthetic import NOW, SENTINEL, FailingStream, chain_problems, close_quietly, deletions, emitter, write_file

TRANSCRIPTIONS, SPEECH = AUDIO_CACHES
HOUR = 3600


class Workspace(unittest.TestCase):
    """A synthetic data volume with both cache directories."""

    @override
    def setUp(self) -> None:
        """Create the volume."""
        self.tmp = Path(tempfile.mkdtemp(prefix="retention-audio-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.transcriptions = self.data / TRANSCRIPTIONS.relative
        self.speech = self.data / SPEECH.relative
        self.transcriptions.mkdir(parents=True)
        self.speech.mkdir(parents=True)

    def names(self, directory: Path) -> set[str]:
        """List a directory.

        Args:
            directory: The directory.

        Returns:
            Its entries' names.
        """
        return {path.name for path in directory.iterdir()}


class SweepTest(Workspace):
    """What a sweep deletes, keeps and records."""

    def test_recordings_and_transcripts_older_than_the_age_go(self) -> None:
        """Every regular file past the age goes, oldest first, in one record naming each."""
        old = [
            write_file(self.transcriptions, "3f2b-0001.webm", age_s=3 * HOUR, size=100),
            write_file(self.transcriptions, "3f2b-0001.json", age_s=3 * HOUR - 5, size=20),
            write_file(self.transcriptions, "3f2b-0002_chunk_0.json", age_s=2 * HOUR, size=30),
        ]
        fresh = write_file(self.transcriptions, "3f2b-0003.wav", age_s=59 * 60)
        audit, stream = emitter()
        result = sweep_cache(audit, self.data, TRANSCRIPTIONS, 60, NOW)
        audit.close()
        self.assertEqual((result.deleted, result.failed, result.bytes_deleted), (3, 0, 150))
        self.assertEqual(self.names(self.transcriptions), {fresh.name})
        (event,) = deletions(stream)
        self.assertEqual((event["operation"], event["outcome"]), ("retention.sweep", "ok"))
        self.assertEqual(event["actor"], {"user": None, "via": "system"})
        self.assertEqual(
            event["target"],
            {"kind": "file", "ids": [path.name for path in old], "count": 3, "container": "cache/audio/transcriptions"},
        )
        self.assertEqual(
            event["criteria"],
            {"store": "audio.transcriptions", "max_age_minutes": 60, "cutoff": "2026-09-20T11:00:00Z", "bytes": 150},
        )
        self.assertNotIn(SENTINEL, stream.getvalue())
        self.assertEqual(chain_problems(stream), [])

    def test_speech_cache_is_swept_the_same_way(self) -> None:
        """Text-to-speech output and the requests stored beside it go too."""
        write_file(self.speech, "a" * 64 + ".mp3", age_s=2 * HOUR)
        write_file(self.speech, "a" * 64 + ".json", age_s=2 * HOUR)
        audit, stream = emitter()
        result = sweep_cache(audit, self.data, SPEECH, 60, NOW)
        audit.close()
        self.assertEqual(result.deleted, 2)
        self.assertEqual(self.names(self.speech), set())
        (event,) = deletions(stream)
        self.assertEqual(event["target"]["container"], "cache/audio/speech")
        self.assertEqual(event["criteria"]["store"], "audio.speech")

    def test_batches_hold_one_records_worth(self) -> None:
        """A large backlog goes in batches of 200 files, each recorded before it is deleted."""
        for index in range(450):
            write_file(self.transcriptions, f"rec-{index:04d}.json", age_s=2 * HOUR + index)
        audit, stream = emitter()
        result = sweep_cache(audit, self.data, TRANSCRIPTIONS, 60, NOW)
        audit.close()
        self.assertEqual(result.deleted, 450)
        self.assertEqual([event["target"]["count"] for event in deletions(stream)], [200, 200, 50])
        self.assertTrue(all("ids_truncated" not in event["target"] for event in deletions(stream)))
        self.assertEqual(chain_problems(stream), [])

    def test_only_regular_files_are_touched(self) -> None:
        """Directories, symlinks and what they point to are left alone."""
        outside = write_file(self.tmp, "outside.txt", age_s=9 * HOUR)
        link = self.transcriptions / "link.json"
        link.symlink_to(outside)
        os.utime(link, (NOW - 9 * HOUR, NOW - 9 * HOUR), follow_symlinks=False)
        nested = self.transcriptions / "nested"
        nested.mkdir()
        write_file(nested, "deep.json", age_s=9 * HOUR)
        os.utime(nested, (NOW - 9 * HOUR, NOW - 9 * HOUR))
        audit, stream = emitter()
        result = sweep_cache(audit, self.data, TRANSCRIPTIONS, 60, NOW)
        audit.close()
        self.assertEqual(result.deleted, 0)
        self.assertEqual(deletions(stream), [])
        self.assertTrue(outside.exists() and link.is_symlink() and (nested / "deep.json").exists())

    def test_symlinked_cache_directory_is_refused(self) -> None:
        """A cache directory pointing off the volume is never swept."""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        victim = write_file(elsewhere, "keep.json", age_s=9 * HOUR)
        shutil.rmtree(self.transcriptions)
        self.transcriptions.symlink_to(elsewhere)
        audit, stream = emitter()
        with self.assertRaises(CacheRefused):
            sweep_cache(audit, self.data, TRANSCRIPTIONS, 60, NOW)
        audit.close()
        self.assertTrue(victim.exists())
        self.assertEqual(deletions(stream), [])

    def test_missing_directory_is_nothing_to_do(self) -> None:
        """Before the first transcription the directory does not exist; nothing happens."""
        shutil.rmtree(self.transcriptions)
        audit, stream = emitter()
        result = sweep_cache(audit, self.data, TRANSCRIPTIONS, 60, NOW)
        audit.close()
        self.assertEqual(result.deleted, 0)
        self.assertFalse(self.transcriptions.exists())
        self.assertEqual(deletions(stream), [])

    def test_stop_ends_the_sweep_between_batches(self) -> None:
        """A stop request finishes the batch in hand and leaves the rest for later."""
        for index in range(250):
            write_file(self.transcriptions, f"rec-{index:04d}.json", age_s=2 * HOUR)
        stop = threading.Event()
        audit, stream = emitter()
        real = audio_caches.emit_deletion

        def emit_then_stop(*args: Any, **kwargs: Any) -> dict[str, object]:
            stop.set()
            return real(*args, **kwargs)

        with mock.patch.object(audio_caches, "emit_deletion", emit_then_stop):
            result = sweep_cache(audit, self.data, TRANSCRIPTIONS, 60, NOW, stop=stop)
        audit.close()
        self.assertEqual(result.deleted, 200)
        self.assertEqual(len(self.names(self.transcriptions)), 50)
        self.assertEqual(len(deletions(stream)), 1)


class FailureTest(Workspace):
    """Record first, delete after; what could not go is recorded again."""

    def test_nothing_is_deleted_without_its_record(self) -> None:
        """When the record cannot be written, the batch's files stay."""
        write_file(self.transcriptions, "rec-0001.webm", age_s=2 * HOUR)
        audit, _ = emitter(FailingStream(fail_at=2))
        with self.assertRaises(AuditWriteError):
            sweep_cache(audit, self.data, TRANSCRIPTIONS, 60, NOW)
        close_quietly(audit)
        self.assertEqual(self.names(self.transcriptions), {"rec-0001.webm"})

    def test_files_that_could_not_be_deleted_are_recorded_as_error(self) -> None:
        """A failed unlink yields an error record for that file, linked to the batch's record."""
        write_file(self.transcriptions, "rec-0001.webm", age_s=3 * HOUR)
        write_file(self.transcriptions, "rec-0002.webm", age_s=2 * HOUR)
        real_unlink = os.unlink

        def unlink(path: str, *, dir_fd: int | None = None) -> None:
            if path == "rec-0001.webm":
                raise PermissionError("synthetic")
            real_unlink(path, dir_fd=dir_fd)

        audit, stream = emitter()
        with mock.patch.object(audio_caches.os, "unlink", unlink), self.assertLogs("retention", "WARNING"):
            result = sweep_cache(audit, self.data, TRANSCRIPTIONS, 60, NOW)
        audit.close()
        self.assertEqual((result.deleted, result.failed), (1, 1))
        self.assertEqual(self.names(self.transcriptions), {"rec-0001.webm"})
        primary, failure = deletions(stream)
        self.assertEqual(primary["target"]["ids"], ["rec-0001.webm", "rec-0002.webm"])
        self.assertEqual(failure["outcome"], "error")
        self.assertEqual(failure["parent_event_id"], primary["event_id"])
        self.assertEqual(failure["target"]["ids"], ["rec-0001.webm"])
        self.assertEqual(chain_problems(stream), [])

    def test_a_file_already_gone_counts_as_deleted(self) -> None:
        """Open WebUI removing a file between the scan and the unlink is not a failure."""
        write_file(self.transcriptions, "rec-0001_chunk_0.mp3", age_s=2 * HOUR)
        real_unlink = os.unlink

        def unlink(path: str, *, dir_fd: int | None = None) -> None:
            real_unlink(path, dir_fd=dir_fd)
            raise FileNotFoundError(path)

        audit, stream = emitter()
        with mock.patch.object(audio_caches.os, "unlink", unlink):
            result = sweep_cache(audit, self.data, TRANSCRIPTIONS, 60, NOW)
        audit.close()
        self.assertEqual((result.deleted, result.failed), (1, 0))
        self.assertEqual([event["outcome"] for event in deletions(stream)], ["ok"])


class ScanTest(Workspace):
    """The dry run's view: counts and ages, nothing changed."""

    def test_scan_counts_without_deleting(self) -> None:
        """Expired files are listed oldest first; the newer and the odd ones are counted."""
        write_file(self.transcriptions, "b.json", age_s=2 * HOUR, size=10)
        write_file(self.transcriptions, "a.webm", age_s=5 * HOUR, size=20)
        write_file(self.transcriptions, "c.webm", age_s=10)
        (self.transcriptions / "nested").mkdir()
        scan = scan_cache(self.data, TRANSCRIPTIONS, NOW - HOUR)
        self.assertEqual([item.name for item in scan.expired], ["a.webm", "b.json"])
        self.assertEqual((scan.kept, scan.skipped), (1, 1))
        self.assertEqual(len(self.names(self.transcriptions)), 4)

    def test_scan_of_a_missing_directory_is_empty(self) -> None:
        """No directory, nothing to report."""
        shutil.rmtree(self.speech)
        scan = scan_cache(self.data, SPEECH, NOW)
        self.assertEqual((scan.expired, scan.kept, scan.skipped), ([], 0, 0))


if __name__ == "__main__":
    unittest.main()
