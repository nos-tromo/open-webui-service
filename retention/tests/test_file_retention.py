"""Tests for file_retention.py: uploads from before the intake was closed go a period after they were made.

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
from file_retention import FileSweep, FileSweeper

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


class FileSweepTest(unittest.TestCase):
    """A synthetic data volume with an uploads directory."""

    @override
    def setUp(self) -> None:
        """Create the volume, the database and a writer for it."""
        self.tmp = Path(tempfile.mkdtemp(prefix="retention-files-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.uploads = self.data / "uploads"
        self.uploads.mkdir(parents=True)
        self.database = self.data / "webui.db"
        make_database(self.database)
        self.store = Store(self.database)
        self.addCleanup(self.store.close)

    def upload(self, owner: str, created_days: float, *, on_disk: bool = True) -> tuple[str, Path]:
        """Add an upload's row and, by default, its file.

        Args:
            owner: The owner.
            created_days: Days since it was uploaded.
            on_disk: Whether its file exists.

        Returns:
            The file id and the file's path on the volume.
        """
        file_id = self.store.upload(owner, created_days=created_days)
        path = self.uploads / f"{file_id}_upload.txt"
        if on_disk:
            path.write_text(SENTINEL, encoding="utf-8")
        return file_id, path

    def sweep(self, days: int = 365, stream: io.StringIO | None = None, **kwargs: Any) -> tuple[FileSweep, io.StringIO]:
        """Run one sweep on a captured stream.

        Args:
            days: The retention period.
            stream: The audit stream; a fresh one by default.
            **kwargs: Passed to :class:`FileSweeper`.

        Returns:
            The result and the stream.
        """
        audit, out = emitter(stream)
        try:
            result = FileSweeper(audit, self.database, self.data, days, pause_s=0, **kwargs).sweep(NOW)
        finally:
            close_quietly(audit)
        return result, out

    def files(self) -> set[str]:
        """List the file rows left.

        Returns:
            Their ids.
        """
        return {str(row[0]) for row in self.store.conn.execute("SELECT id FROM file")}

    def test_old_upload_goes_with_its_file_and_links(self) -> None:
        """The row, its chat and knowledge links, its grants and its file on disk go; a young one stays."""
        old, old_path = self.upload(ALICE, 400)
        young, young_path = self.upload(BOB, 10)
        for file_id in (old, young):
            self.store.conn.execute(
                "INSERT INTO chat_file (id, user_id, chat_id, file_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (f"link-{file_id}", ALICE, f"chat-{file_id}", file_id, NOW, NOW),
            )
            self.store.conn.execute(
                "INSERT INTO knowledge_file (id, user_id, knowledge_id, file_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"kf-{file_id}", ALICE, "kb-1", file_id, NOW, NOW),
            )
            self.store.conn.execute(
                "INSERT INTO access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, "
                "created_at) VALUES (?, 'file', ?, 'user', ?, 'read', ?)",
                (f"grant-{file_id}", file_id, BOB, NOW),
            )
        result, stream = self.sweep(365)
        self.assertEqual((result.files, result.removed_from_disk), (1, 1))
        self.assertEqual(self.files(), {young})
        self.assertFalse(old_path.exists())
        self.assertTrue(young_path.exists())
        for table, column in (("chat_file", "file_id"), ("knowledge_file", "file_id"), ("access_grant", "resource_id")):
            self.assertEqual(self.store.count(table, f"{column} = ?", (old,)), 0, table)
            self.assertEqual(self.store.count(table, f"{column} = ?", (young,)), 1, table)
        (event,) = deletions(stream)
        self.assertEqual(event["target"], {"kind": "upload", "ids": [old], "count": 1})
        self.assertEqual(event["criteria"]["owner"], ALICE)
        self.assertEqual(chain_problems(stream), [])

    def test_record_holds_no_file_name_or_content(self) -> None:
        """Uploads are named by id only: a file name can name a person."""
        self.upload(ALICE, 400)
        _, stream = self.sweep(365)
        self.assertNotIn(SENTINEL, stream.getvalue())
        self.assertNotIn("upload.txt", stream.getvalue())

    def test_path_outside_uploads_is_refused_and_row_kept(self) -> None:
        """A recorded path that is not under the image's upload directory is never followed; the row stays."""
        outside = self.tmp / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        strays = [
            self.store.upload(ALICE, created_days=400, path=str(outside)),
            self.store.upload(ALICE, created_days=400, path="/app/backend/data/uploads/../webui.db"),
            self.store.upload(ALICE, created_days=400, path="/app/backend/data/uploads/sub/x.txt"),
        ]
        with self.assertLogs("retention", "WARNING"):
            result, stream = self.sweep(365)
        self.assertEqual(result.files, 0)
        self.assertEqual(self.files(), set(strays))
        self.assertTrue(outside.exists())
        self.assertTrue(self.database.exists())
        self.assertEqual(deletions(stream), [])

    def test_symlinked_upload_is_not_followed(self) -> None:
        """An upload that is a symbolic link: the link goes, its target stays."""
        file_id = self.store.upload(ALICE, created_days=400)
        target = self.tmp / "target.txt"
        target.write_text("keep", encoding="utf-8")
        (self.uploads / f"{file_id}_upload.txt").symlink_to(target)
        self.sweep(365)
        self.assertTrue(target.exists())
        self.assertFalse((self.uploads / f"{file_id}_upload.txt").is_symlink())
        self.assertEqual(self.files(), set())

    def test_missing_file_on_disk_still_removes_the_row(self) -> None:
        """A row whose file is already gone is deleted all the same."""
        file_id, _ = self.upload(ALICE, 400, on_disk=False)
        result, _ = self.sweep(365)
        self.assertEqual((result.files, result.removed_from_disk), (1, 0))
        self.assertNotIn(file_id, self.files())

    def test_failed_record_deletes_nothing(self) -> None:
        """When the record cannot be written, the row and the file stay."""
        file_id, path = self.upload(ALICE, 400)
        with self.assertRaises(AuditWriteError):
            self.sweep(365, stream=FailingStream(fail_at=2))
        self.assertEqual(self.files(), {file_id})
        self.assertTrue(path.exists())

    def test_the_sweep_empties_the_write_ahead_log(self) -> None:
        """After deleting, neither the database file nor its log holds the upload's extracted text."""
        self.upload(ALICE, 400)
        wal = self.database.with_name(f"{self.database.name}-wal")
        result, _ = self.sweep(365)
        self.assertTrue(result.log_emptied)
        self.assertEqual(wal.stat().st_size, 0)
        self.assertNotIn(SENTINEL.encode(), self.database.read_bytes())


if __name__ == "__main__":
    unittest.main()
