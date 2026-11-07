"""Tests for owui_retention.py: settings, the serve loop, health, the dry run.

Run from the repo root: ``python3 -m unittest discover -s retention/tests -t retention``.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, override
from unittest import mock

import owui_retention
from audio_caches import AUDIO_CACHES
from chat_retention import ChatSweep
from owui_retention import CHAT_EVERY_S, Rounds, Settings, dry_run, health, schema_check, serve, verify
from retention_state import STATE_NAME

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
    write_file,
)

TRANSCRIPTIONS, SPEECH = AUDIO_CACHES


class SettingsTest(unittest.TestCase):
    """The operator's knobs and their defaults."""

    def test_defaults_keep_the_decisions(self) -> None:
        """Audio caches on at 60 minutes, chats on at 365 days with a 30-day notice, a 300-second round."""
        settings = Settings.from_env({})
        self.assertEqual((settings.audio_minutes, settings.chat_days, settings.interval_s), (60, 365, 300))
        self.assertEqual(settings.notice_days, 30)
        self.assertFalse(settings.external_database)

    def test_notice_days_are_whole_days(self) -> None:
        """RETENTION_NOTICE_DAYS takes 1 to 365 days, and never off."""
        self.assertEqual(Settings.from_env({"RETENTION_NOTICE_DAYS": "14"}).notice_days, 14)
        for value in ("0", "366", "off", "soon"):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                Settings.from_env({"RETENTION_NOTICE_DAYS": value})
            self.assertIn("RETENTION_NOTICE_DAYS", str(caught.exception))
            self.assertNotIn("or off", str(caught.exception))

    def test_periods_are_whole_numbers_or_off(self) -> None:
        """A period is set by a whole number and switched off only by an explicit off."""
        settings = Settings.from_env(
            {
                "OPEN_WEBUI_AUDIO_RETENTION_MINUTES": "off",
                "OPEN_WEBUI_CHAT_RETENTION_DAYS": " 90 ",
                "OPEN_WEBUI_RETENTION_INTERVAL_S": "60",
                "RETENTION_EXTERNAL_DATABASE": "true",
            }
        )
        self.assertEqual((settings.audio_minutes, settings.chat_days, settings.interval_s), (None, 90, 60))
        self.assertTrue(settings.external_database)
        self.assertEqual(Settings.from_env({"OPEN_WEBUI_AUDIO_RETENTION_MINUTES": ""}).audio_minutes, 60)
        self.assertIsNone(Settings.from_env({"OPEN_WEBUI_CHAT_RETENTION_DAYS": "OFF"}).chat_days)

    def test_invalid_values_are_refused(self) -> None:
        """Zero, negatives, fractions, words and out-of-range values stop the service."""
        for name, value in (
            ("OPEN_WEBUI_AUDIO_RETENTION_MINUTES", "0"),
            ("OPEN_WEBUI_AUDIO_RETENTION_MINUTES", "-5"),
            ("OPEN_WEBUI_CHAT_RETENTION_DAYS", "1.5"),
            ("OPEN_WEBUI_CHAT_RETENTION_DAYS", "never"),
            ("OPEN_WEBUI_CHAT_RETENTION_DAYS", "3651"),
            ("OPEN_WEBUI_RETENTION_INTERVAL_S", "5"),
            ("OPEN_WEBUI_RETENTION_INTERVAL_S", "off"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError) as caught:
                Settings.from_env({name: value})
            self.assertIn(name, str(caught.exception))

    def test_bad_configuration_exits_2(self) -> None:
        """The entry point refuses to start on a bad setting."""
        environ = {"OPEN_WEBUI_CHAT_RETENTION_DAYS": "soon"}
        with (
            mock.patch.dict(owui_retention.os.environ, environ),
            mock.patch.object(owui_retention.logging, "basicConfig"),
            self.assertLogs("retention", "ERROR"),
        ):
            self.assertEqual(owui_retention.main(["health"]), 2)


class Volume(unittest.TestCase):
    """A synthetic data volume: both caches, a database, a version file."""

    @override
    def setUp(self) -> None:
        """Create the volume and settings pointing at it."""
        self.tmp = Path(tempfile.mkdtemp(prefix="retention-serve-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        for cache in AUDIO_CACHES:
            (self.data / cache.relative).mkdir(parents=True)
        make_database(self.data / "webui.db")
        self.store = Store(self.data / "webui.db")
        self.addCleanup(self.store.close)
        version = self.tmp / "VERSION"
        version.write_text("0.9.0\n", encoding="utf-8")
        self.settings = Settings(
            chat_days=30,
            interval_s=10,
            data_dir=self.data,
            version_file=version,
            status_file=self.tmp / "status.json",
        )
        self.since(chats=(30, NOW - 400 * DAY), notes=(30, NOW - 400 * DAY), files=(30, NOW - 400 * DAY))

    def since(self, **stores: tuple[int, float]) -> None:
        """Record when stores' periods took effect, as an earlier run would have.

        Args:
            **stores: Per store, its period in days and when it took effect.
        """
        body = {"stores": {name: {"days": days, "since": since} for name, (days, since) in stores.items()}}
        (self.data / STATE_NAME).write_text(json.dumps(body), encoding="utf-8")

    def serve_one_round(self, settings: Settings, stream: io.StringIO | None = None) -> tuple[int, io.StringIO]:
        """Run ``serve`` for exactly one round, at the test clock.

        Args:
            settings: The settings.
            stream: The audit stream; a fresh one by default.

        Returns:
            The exit status and the stream.
        """
        out = stream if stream is not None else io.StringIO()
        stop = threading.Event()
        real_run = Rounds.run

        def run_once(rounds: Rounds, now: float) -> None:
            try:
                real_run(rounds, now)
            finally:
                stop.set()

        with (
            mock.patch.object(owui_retention, "open_audit_stream", return_value=out),
            mock.patch.object(owui_retention.time, "time", return_value=float(NOW)),
            mock.patch.object(Rounds, "run", run_once),
        ):
            status = serve(settings, "container", stop)
        return status, out


class ServeTest(Volume):
    """The long-running service, one round at a time."""

    def test_one_round_sweeps_everything_configured_and_records_it(self) -> None:
        """start, config.audit, the deletions, shutdown: one intact chain; status says healthy."""
        write_file(self.data / TRANSCRIPTIONS.relative, "rec-0001.webm", age_s=2 * 3600)
        write_file(self.data / SPEECH.relative, "f" * 64 + ".mp3", age_s=2 * 3600)
        expired = self.store.chat(ALICE, age_days=45)
        kept = self.store.chat(BOB, age_days=2)
        status, stream = self.serve_one_round(self.settings)
        self.assertEqual(status, 0)
        self.assertEqual(self.store.chat_ids(), {kept})
        self.assertNotIn(expired, self.store.chat_ids())
        for cache in AUDIO_CACHES:
            self.assertEqual(list((self.data / cache.relative).iterdir()), [])
        written = events(stream)
        self.assertEqual(
            [(event["action"], event["operation"]) for event in written],
            [
                ("lifecycle", "start"),
                ("admin", "config.audit"),
                ("delete", "retention.sweep"),
                ("delete", "retention.sweep"),
                ("delete", "retention.sweep"),
                ("lifecycle", "shutdown"),
            ],
        )
        self.assertEqual({event["service"] for event in written}, {"open-webui-service"})
        self.assertEqual(
            written[1]["criteria"],
            {
                "audio_max_age_minutes": 60,
                "chat_retention_days": 30,
                "chat_retention_since": "2025-08-16T12:00:00Z",
                "chat_retention_grace_until": "2025-09-15T12:00:00Z",
                "chat_retention_in_force_days": None,
                "note_retention_since": "2025-08-16T12:00:00Z",
                "note_retention_grace_until": "2025-09-15T12:00:00Z",
                "note_retention_in_force_days": None,
                "file_retention_since": "2025-08-16T12:00:00Z",
                "file_retention_grace_until": "2025-09-15T12:00:00Z",
                "file_retention_in_force_days": None,
                "notice_days": 30,
                "interval_s": 10,
                "chat_every_s": CHAT_EVERY_S,
                "chat_external_database": False,
            },
        )
        self.assertEqual(
            [event["criteria"]["store"] for event in deletions(stream)],
            [
                "audio.transcriptions",
                "audio.speech",
                "chats",
            ],
        )
        self.assertEqual(chain_problems(stream), [])
        self.assertEqual(health(self.settings, now=NOW + 1), 0)

    def test_off_deletes_no_chat_and_says_so(self) -> None:
        """With chat retention off, the oldest chat stays, a warning is logged, and the chain states it."""
        self.store.chat(ALICE, age_days=4000)
        before = self.store.rows()
        settings = replace(self.settings, chat_days=None)
        with self.assertLogs("retention", "WARNING") as logged:
            status, stream = self.serve_one_round(settings)
        self.assertEqual(status, 0)
        self.assertEqual(self.store.rows(), before)
        self.assertEqual(deletions(stream), [])
        self.assertIsNone(events(stream)[1]["criteria"]["chat_retention_days"])
        self.assertTrue(any("chat retention is off" in line for line in logged.output))
        state = json.loads((self.data / STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["stores"], {})

    def test_config_states_each_stores_notice_end_and_the_period_in_force(self) -> None:
        """The chain says when deletion starts and which period still deletes meanwhile."""
        body = {
            "stores": {
                "chats": {
                    "days": 30,
                    "since": NOW - 5 * DAY,
                    "grace_until": NOW + 25 * DAY,
                    "in_force_days": 365,
                    "in_force_grace": NOW - 400 * DAY,
                }
            }
        }
        (self.data / STATE_NAME).write_text(json.dumps(body), encoding="utf-8")
        _, stream = self.serve_one_round(self.settings)
        criteria = events(stream)[1]["criteria"]
        self.assertEqual(criteria["chat_retention_grace_until"], "2026-10-15T12:00:00Z")
        self.assertEqual(criteria["chat_retention_in_force_days"], 365)

    def test_config_states_the_notice_and_when_chats_took_effect(self) -> None:
        """The chain's config event carries the notice days and the time chat retention took effect."""
        _, stream = self.serve_one_round(self.settings)
        criteria = events(stream)[1]["criteria"]
        self.assertEqual(criteria["notice_days"], 30)
        self.assertEqual(criteria["chat_retention_since"], "2025-08-16T12:00:00Z")
        self.assertEqual(criteria["note_retention_since"], "2025-08-16T12:00:00Z")
        self.assertEqual(criteria["file_retention_since"], "2025-08-16T12:00:00Z")

    def test_audio_off_leaves_the_caches(self) -> None:
        """OPEN_WEBUI_AUDIO_RETENTION_MINUTES=off keeps every recording."""
        recording = write_file(self.data / TRANSCRIPTIONS.relative, "rec-0001.webm", age_s=99 * 3600)
        status, stream = self.serve_one_round(replace(self.settings, audio_minutes=None, chat_days=None))
        self.assertEqual(status, 0)
        self.assertTrue(recording.exists())
        self.assertEqual(deletions(stream), [])

    def test_failed_record_stops_the_service(self) -> None:
        """When a deletion's record cannot be written, serve exits 1 and nothing of it is deleted."""
        recording = write_file(self.data / TRANSCRIPTIONS.relative, "rec-0001.webm", age_s=2 * 3600)
        expired = self.store.chat(ALICE, age_days=45)
        with self.assertLogs("retention", "ERROR"):
            status, _ = self.serve_one_round(self.settings, FailingStream(fail_at=3))
        self.assertEqual(status, 1)
        self.assertTrue(recording.exists())
        self.assertIn(expired, self.store.chat_ids())

    def test_unwritable_audit_stream_deletes_nothing(self) -> None:
        """If the stream cannot even be opened, serve exits 1 before any sweep."""
        recording = write_file(self.data / TRANSCRIPTIONS.relative, "rec-0001.webm", age_s=2 * 3600)
        with (
            mock.patch.object(owui_retention, "open_audit_stream", side_effect=OSError("no stdout")),
            self.assertLogs("retention", "ERROR"),
        ):
            self.assertEqual(serve(self.settings, "container", threading.Event()), 1)
        self.assertTrue(recording.exists())


class RefusalTest(Volume):
    """A store that must not be swept is refused, recorded once, and reported unhealthy."""

    def rounds(self, settings: Settings) -> tuple[Rounds, io.StringIO]:
        """Build the rounds on a captured stream.

        Args:
            settings: The settings.

        Returns:
            The rounds and the stream.
        """
        audit, stream = emitter()
        self.addCleanup(close_quietly, audit)
        return Rounds(audit, settings, threading.Event()), stream

    def test_unverified_schema_refuses_chats_but_not_audio(self) -> None:
        """An image bump stops chat and note retention, says so once each, and the audio caches keep being swept."""
        self.store.chat(ALICE, age_days=45)
        self.store.conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        recording = write_file(self.data / TRANSCRIPTIONS.relative, "rec-0001.webm", age_s=2 * 3600)
        rounds, stream = self.rounds(self.settings)
        with self.assertLogs("retention", "ERROR") as logged:
            rounds.run(NOW)
            rounds.run(NOW + 60)
        self.assertIn("schema revision ffffffffffff", "\n".join(logged.output))
        self.assertFalse(recording.exists())
        self.assertEqual(len(self.store.chat_ids()), 1)
        refused = [event for event in events(stream) if event["operation"] == "retention.refused"]
        self.assertEqual(
            [event["criteria"] for event in refused],
            [
                {"store": "chats", "reason": "schema"},
                {"store": "notes", "reason": "schema"},
                {"store": "files", "reason": "schema"},
            ],
        )
        self.assertEqual(rounds.stores["chats"].state, "refused")
        owui_retention.write_status(self.settings.status_file, owui_retention.Status(NOW + 60, 10, rounds.stores))
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(health(self.settings, now=NOW + 60), 1)
        self.assertIn("chats refused (schema)", err.getvalue())

    def test_verified_schema_again_resumes(self) -> None:
        """Once the schema is verified again (a release that knows it), sweeping resumes, recorded."""
        self.store.conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        rounds, stream = self.rounds(self.settings)
        with self.assertLogs("retention", "INFO"):
            rounds.run(NOW)
            self.store.conn.execute("UPDATE alembic_version SET version_num = 'e42c8f710a36'")
            rounds.run(NOW + 60)
        operations = [event["operation"] for event in events(stream)]
        self.assertEqual(operations.count("retention.refused"), 3)
        self.assertEqual(operations.count("retention.resumed"), 3)
        self.assertEqual(rounds.stores["chats"].state, "ok")
        self.assertEqual(chain_problems(stream), [])

    def test_own_database_url_refuses_chats(self) -> None:
        """When Open WebUI keeps its chats elsewhere, chat retention refuses instead of sweeping webui.db."""
        self.store.chat(ALICE, age_days=45)
        rounds, stream = self.rounds(replace(self.settings, external_database=True))
        with self.assertLogs("retention", "ERROR"):
            rounds.run(NOW)
        self.assertEqual(len(self.store.chat_ids()), 1)
        refused = [event for event in events(stream) if event["operation"] == "retention.refused"]
        self.assertEqual(refused[0]["criteria"], {"store": "chats", "reason": "external_database"})

    def test_locked_database_is_retried_next_round(self) -> None:
        """A sweep that cannot get the lock is an error for this round, then retried at once."""
        self.store.chat(ALICE, age_days=45)
        rounds, _ = self.rounds(self.settings)
        with (
            mock.patch.object(owui_retention.ChatSweeper, "sweep", side_effect=owui_retention.sqlite3.OperationalError),
            self.assertLogs("retention", "WARNING"),
        ):
            rounds.run(NOW)
        self.assertEqual(rounds.stores["chats"].state, "error")
        rounds.run(NOW + 10)
        self.assertEqual(rounds.stores["chats"].state, "ok")
        self.assertEqual(self.store.chat_ids(), set())

    def test_first_round_records_the_period_and_waits(self) -> None:
        """A period seen for the first time takes effect now: nothing is deleted for the notice period."""
        (self.data / STATE_NAME).unlink()
        old = self.store.chat(ALICE, age_days=1, created_days=400)
        rounds, stream = self.rounds(self.settings)
        with self.assertLogs("retention", "INFO") as logged:
            rounds.run(NOW)
        self.assertIn(old, self.store.chat_ids())
        self.assertEqual(deletions(stream), [])
        state = json.loads((self.data / STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["stores"]["chats"], {"days": 30, "since": NOW, "grace_until": NOW + 30 * DAY})
        self.assertTrue(any("2026-10-20" in line for line in logged.output), logged.output)
        self.assertEqual(rounds.stores["chats"].state, "ok")

    def test_nothing_is_deleted_during_the_grace_period(self) -> None:
        """29 days after the period took effect a 400-day-old chat stays; at 30 days it goes."""
        self.since(chats=(30, NOW - 29 * DAY))
        old = self.store.chat(ALICE, age_days=1, created_days=400)
        rounds, _ = self.rounds(self.settings)
        rounds.run(NOW)
        self.assertIn(old, self.store.chat_ids())
        later, _ = self.rounds(self.settings)
        later.run(NOW + DAY)
        self.assertNotIn(old, self.store.chat_ids())

    def test_a_shorter_period_spares_only_what_falls_due_sooner(self) -> None:
        """Lowering the period: what the period in force made due still goes; what it makes due waits."""
        self.since(chats=(365, NOW - 400 * DAY))
        due = self.store.chat(ALICE, age_days=1, created_days=400)
        newly_due = self.store.chat(ALICE, age_days=1, created_days=200)
        rounds, _ = self.rounds(replace(self.settings, chat_days=180))
        rounds.run(NOW)
        self.assertEqual((due in self.store.chat_ids(), newly_due in self.store.chat_ids()), (False, True))

    def test_the_schema_is_checked_during_the_grace_too(self) -> None:
        """An unverified schema is refused at once, not only when the notice ends."""
        self.since(chats=(30, NOW - DAY))
        self.store.conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        rounds, _ = self.rounds(self.settings)
        with self.assertLogs("retention", "ERROR"):
            rounds.run(NOW)
        self.assertEqual((rounds.stores["chats"].state, rounds.stores["chats"].reason), ("refused", "schema"))

    def test_no_state_is_written_before_the_database_exists(self) -> None:
        """On a fresh volume the chats store waits for Open WebUI's database; no period is recorded."""
        (self.data / STATE_NAME).unlink()
        self.store.close()
        for name in ("webui.db", "webui.db-wal", "webui.db-shm"):
            (self.data / name).unlink(missing_ok=True)
        rounds, _ = self.rounds(self.settings)
        rounds.run(NOW)
        self.assertFalse((self.data / STATE_NAME).exists())

    def test_a_damaged_state_refuses_chats(self) -> None:
        """A state file that cannot be used refuses chat retention instead of restarting the grace."""
        (self.data / STATE_NAME).write_text("not json", encoding="utf-8")
        old = self.store.chat(ALICE, age_days=1, created_days=400)
        rounds, stream = self.rounds(self.settings)
        with self.assertLogs("retention", "ERROR"):
            rounds.run(NOW)
        self.assertIn(old, self.store.chat_ids())
        self.assertEqual((rounds.stores["chats"].state, rounds.stores["chats"].reason), ("refused", "state"))
        refused = [event for event in events(stream) if event["operation"] == "retention.refused"]
        self.assertEqual(refused[0]["criteria"], {"store": "chats", "reason": "state"})

    def test_old_notes_go_with_the_chats_period(self) -> None:
        """Notes are swept in the same round as chats, by the chats' period, and recorded."""
        old = self.store.note(ALICE, created_days=40)
        young = self.store.note(ALICE, created_days=10)
        rounds, stream = self.rounds(self.settings)
        rounds.run(NOW)
        remaining = {str(row[0]) for row in self.store.conn.execute("SELECT id FROM note")}
        self.assertEqual(remaining, {young})
        self.assertEqual([event["target"]["kind"] for event in deletions(stream)], ["note"])
        self.assertNotIn(old, remaining)
        self.assertEqual(rounds.stores["notes"].state, "ok")

    def test_old_uploads_go_with_the_chats_period(self) -> None:
        """Uploads from before the intake was closed are swept in the same round, row and file."""
        old = self.store.upload(ALICE, created_days=40)
        (self.data / "uploads").mkdir()
        disk = self.data / "uploads" / f"{old}_upload.txt"
        disk.write_text("x", encoding="utf-8")
        rounds, stream = self.rounds(self.settings)
        rounds.run(NOW)
        self.assertEqual(self.store.count("file", "id = ?", (old,)), 0)
        self.assertFalse(disk.exists())
        self.assertIn("upload", [event["target"]["kind"] for event in deletions(stream)])
        self.assertEqual(rounds.stores["files"].state, "ok")

    def test_notes_have_their_own_grace(self) -> None:
        """A notes period seen for the first time waits its notice, even when chats are past theirs."""
        self.since(chats=(30, NOW - 400 * DAY))
        old = self.store.note(ALICE, created_days=400)
        rounds, _ = self.rounds(self.settings)
        rounds.run(NOW)
        self.assertEqual(self.store.count("note", "id = ?", (old,)), 1)
        state = json.loads((self.data / STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["stores"]["notes"], {"days": 30, "since": NOW, "grace_until": NOW + 30 * DAY})

    def test_chats_are_swept_hourly(self) -> None:
        """After a complete chat sweep the next one waits an hour; the audio caches do not."""
        rounds, _ = self.rounds(self.settings)
        with mock.patch.object(owui_retention.ChatSweeper, "sweep", autospec=True) as sweep:
            sweep.return_value = ChatSweep()
            rounds.run(NOW)
            rounds.run(NOW + 300)
            rounds.run(NOW + CHAT_EVERY_S)
        self.assertEqual(sweep.call_count, 2)


class HealthTest(Volume):
    """The healthcheck reads what serve reported."""

    def report(self, **stores: dict[str, Any]) -> None:
        """Write a status file as of the test clock.

        Args:
            **stores: Each store's status.
        """
        data = {"version": 1, "written_at": NOW, "interval_s": 10, "stores": stores}
        self.settings.status_file.write_text(json.dumps(data), encoding="utf-8")

    def check(self, now: float) -> int:
        """Run the healthcheck quietly.

        Args:
            now: The time.

        Returns:
            Its exit status.
        """
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            return health(self.settings, now=now)

    def test_no_status_is_unhealthy(self) -> None:
        """Before the first round there is nothing to vouch for."""
        self.assertEqual(self.check(NOW), 1)

    def test_recent_sweeps_are_healthy(self) -> None:
        """Every configured store swept recently, off ones ignored."""
        self.report(
            **{
                "audio.transcriptions": {"state": "ok", "last_ok": NOW},
                "audio.speech": {"state": "error", "last_ok": NOW - 20},
                "chats": {"state": "off", "last_ok": None},
            }
        )
        self.assertEqual(self.check(NOW + 5), 0)

    def test_stale_loop_is_unhealthy(self) -> None:
        """A loop that stopped reporting is unhealthy."""
        self.report(**{"audio.transcriptions": {"state": "ok", "last_ok": NOW}})
        self.assertEqual(self.check(NOW + 61), 1)

    def test_store_failing_for_three_rounds_is_unhealthy(self) -> None:
        """Errors are tolerated for three rounds (a minute at least), then reported."""
        self.report(**{"audio.speech": {"state": "error", "last_ok": NOW - 61}})
        self.assertEqual(self.check(NOW), 1)

    def test_chats_have_the_hourly_schedule_to_spare(self) -> None:
        """A chat sweep an hour ago is fine; one an hour and a few rounds ago is not."""
        self.report(**{"chats": {"state": "ok", "last_ok": NOW - CHAT_EVERY_S}})
        self.assertEqual(self.check(NOW), 0)
        self.report(**{"chats": {"state": "ok", "last_ok": NOW - CHAT_EVERY_S - 61}})
        self.assertEqual(self.check(NOW), 1)


class DryRunTest(Volume):
    """The dry run reports counts and ages; it records and deletes nothing."""

    def report(self, **kwargs: Any) -> tuple[int, str]:
        """Run the dry run.

        Args:
            **kwargs: ``audio_minutes`` and ``chat_days``.

        Returns:
            Its exit status and report.
        """
        out = io.StringIO()
        status = dry_run(self.settings, out, now=NOW, **kwargs)
        return status, out.getvalue()

    def test_report_counts_without_changing_anything(self) -> None:
        """What would go, with ages and counts, and nothing that names or quotes anyone."""
        write_file(self.data / TRANSCRIPTIONS.relative, "rec-0001.webm", age_s=3 * 3600, size=2048)
        write_file(self.data / TRANSCRIPTIONS.relative, "rec-0001.json", age_s=2 * 3600, size=1024)
        write_file(self.data / TRANSCRIPTIONS.relative, "rec-0002.webm", age_s=60)
        parent = self.store.chat(ALICE, age_days=95, tags=["topic"])
        self.store.internal_chat(ALICE, parent, age_days=1)
        self.store.message(parent, ALICE)
        self.store.related(parent, ALICE)
        self.store.chat(BOB, age_days=40)
        self.store.chat(BOB, age_days=3)
        before = self.store.rows()
        files = sorted(path.name for path in (self.data / TRANSCRIPTIONS.relative).iterdir())
        status, text = self.report(audio_minutes=60, chat_days=30)
        self.assertEqual(status, 0)
        self.assertEqual(self.store.rows(), before)
        self.assertEqual(sorted(path.name for path in (self.data / TRANSCRIPTIONS.relative).iterdir()), files)
        self.assertIn("would delete: 2 files, 3.0 KiB, last changed 2 h 0 min to 3 h 0 min ago", text)
        self.assertIn("keeps: 1 newer file\n", text)
        self.assertIn("would delete: 3 chats of 2 accounts, started 40 d to 95 d ago", text)
        self.assertIn("of them internal chats going with their parent: 1", text)
        self.assertIn("messages 1", text)
        self.assertIn("ratings 1", text)
        self.assertIn("keeps: 1 chat\n", text)
        self.assertIn("automation runs that keep their record and lose the link: 1", text)
        for secret in (SENTINEL, parent, ALICE, BOB, "alice@example.org", "rec-0001", "topic"):
            self.assertNotIn(secret, text)

    def test_configured_periods_are_the_default(self) -> None:
        """Without overrides the report shows the configured rule; chat retention off says how to preview."""
        out = io.StringIO()
        status = dry_run(replace(self.settings, chat_days=None), out, audio_minutes=60, chat_days=None, now=NOW)
        text = out.getvalue()
        self.assertEqual(status, 0)
        self.assertIn("rule: files older than 60 minutes", text)
        self.assertIn("Chats (webui.db)\n  retention: off; nothing is deleted", text)
        self.assertIn("make retention-dry-run CHAT_DAYS=<n>", text)

    def test_dry_run_counts_uploads_and_their_vectors(self) -> None:
        """Uploads are counted with the chats' period; the report says how to clear their vectors."""
        self.store.upload(ALICE, created_days=40)
        self.store.upload(BOB, created_days=5)
        status, text = self.report(audio_minutes=60, chat_days=30)
        self.assertEqual(status, 0)
        self.assertIn("Uploads (webui.db and uploads/; from before the intake was closed)\n", text)
        self.assertIn("  would delete: 1 upload\n  keeps: 1 upload\n", text)
        self.assertIn("Reset vector storage", text)
        self.assertNotIn(SENTINEL, text)

    def test_dry_run_counts_notes(self) -> None:
        """Notes are counted with the chats' period, and named by count only."""
        self.store.note(ALICE, created_days=40)
        self.store.note(BOB, created_days=50)
        self.store.note(BOB, created_days=5)
        status, text = self.report(audio_minutes=60, chat_days=30)
        self.assertEqual(status, 0)
        self.assertIn("Notes (webui.db; switched off, those written before)\n", text)
        self.assertIn("  would delete: 2 notes\n  keeps: 1 note\n", text)
        self.assertNotIn(SENTINEL, text)

    def test_dry_run_names_the_first_deletion_date(self) -> None:
        """Inside the grace period the report says from when chats are deleted."""
        self.since(chats=(30, NOW - 10 * DAY))
        status, text = self.report(audio_minutes=60, chat_days=30)
        self.assertEqual(status, 0)
        self.assertIn("deletion starts: 2026-10-10T12:00:00Z (the notice period after the period took effect)", text)
        status, text = self.report(audio_minutes=60, chat_days=90)
        self.assertIn("deletion starts: 2026-10-20T12:00:00Z", text)

    def test_notes_and_uploads_name_their_first_deletion_date_too(self) -> None:
        """Inside their own grace the report says from when notes and uploads are deleted."""
        self.since(chats=(30, NOW - 400 * DAY), notes=(30, NOW - 10 * DAY), files=(30, NOW - 10 * DAY))
        _, text = self.report(audio_minutes=60, chat_days=30)
        notes = text.split("Notes (")[1].split("Uploads (")[0]
        uploads = text.split("Uploads (")[1]
        self.assertIn("deletion starts: 2026-10-10T12:00:00Z", notes)
        self.assertIn("deletion starts: 2026-10-10T12:00:00Z", uploads)

    def test_the_report_says_when_the_period_in_force_starts_deleting(self) -> None:
        """During a shortened period's notice, the date the period in force deletes from is named too."""
        body = {
            "stores": {
                "chats": {
                    "days": 30,
                    "since": NOW - 5 * DAY,
                    "grace_until": NOW + 25 * DAY,
                    "in_force_days": 365,
                    "in_force_grace": NOW + 5 * DAY,
                }
            }
        }
        (self.data / STATE_NAME).write_text(json.dumps(body), encoding="utf-8")
        _, text = self.report(audio_minutes=60, chat_days=30)
        chats = text.split("Chats (")[1].split("Notes (")[0]
        self.assertIn("older than 365 days goes from 2026-09-25T12:00:00Z", chats)

    def test_an_upload_outside_the_upload_directory_is_counted_as_kept(self) -> None:
        """The sweep never deletes a path outside uploads/, so the report does not count it as going."""
        self.store.upload(ALICE, created_days=40, path="/etc/zz-not-an-upload")
        _, text = self.report(audio_minutes=60, chat_days=30)
        self.assertIn("  would delete: nothing\n  keeps: 1 upload\n", text)

    def test_refused_schema_is_reported(self) -> None:
        """An unverified schema is reported with what does not match, and exit 1."""
        self.store.conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        status, text = self.report(audio_minutes=None, chat_days=30)
        self.assertEqual(status, 1)
        self.assertIn("refused: the database is not the schema this release was verified against", text)
        self.assertIn("schema revision ffffffffffff", text)

    def test_cli_overrides_the_configured_period(self) -> None:
        """The CHAT_DAYS of make retention-dry-run evaluates a period before anyone configures it."""
        self.store.chat(ALICE, age_days=40)
        out = io.StringIO()
        configured = replace(self.settings, chat_days=None)
        with (
            mock.patch.dict(owui_retention.os.environ, {}, clear=True),
            mock.patch.object(Settings, "from_env", return_value=configured),
            mock.patch.object(owui_retention.logging, "basicConfig"),
            mock.patch("sys.stdout", out),
            mock.patch.object(owui_retention.time, "time", return_value=float(NOW)),
        ):
            self.assertEqual(owui_retention.main(["dry-run", "--chat-days", "30", "--audio-minutes", "off"]), 0)
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(owui_retention.main(["dry-run", "--chat-days", "0"]), 2)
        self.assertIn("would delete: 1 chat of 1 account, started 40 d ago", out.getvalue())
        self.assertIn("(cache/audio/transcriptions)\n  retention: off; nothing is deleted", out.getvalue())


class SchemaCheckTest(Volume):
    """The schema subcommand CI runs against the pinned image's own database."""

    def test_verdicts(self) -> None:
        """0 for the verified schema, 1 for another or none."""
        out = io.StringIO()
        self.assertEqual(schema_check(self.data / "webui.db", out), 0)
        self.store.conn.execute("CREATE TABLE chat_star (chat_id TEXT)")
        self.assertEqual(schema_check(self.data / "webui.db", out), 1)
        self.assertIn("table chat_star refers to chats", out.getvalue())
        self.assertEqual(schema_check(self.tmp / "absent.db", out), 1)
        self.assertFalse((self.tmp / "absent.db").exists())


class VerifyTest(unittest.TestCase):
    """The smoke check over a container's log."""

    def test_intact_and_broken_chains(self) -> None:
        """Lines with or without a compose prefix verify; a dropped line does not."""
        audit, stream = emitter()
        audit.emit("admin", "config.audit", owui_retention.SYSTEM)
        audit.close()
        lines = [f"retention-1  | {line}" for line in stream.getvalue().splitlines()]
        out = io.StringIO()
        self.assertEqual(verify(["ops log line", *lines], out), 0)
        self.assertIn("3 audit events in 1 chains, 0 problems", out.getvalue())
        self.assertEqual(verify(lines[:1] + lines[2:], io.StringIO()), 1)
        self.assertEqual(verify([], io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
