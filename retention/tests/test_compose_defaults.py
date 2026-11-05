"""The compose file's retention defaults: on by default, notes off.

Run from the repo root: ``python3 -m unittest discover -s retention/tests -t retention``.
"""

from __future__ import annotations

import unittest
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "compose.yaml"


@unittest.skipUnless(COMPOSE.is_file(), "needs the repo's docker/")
class ComposeDefaultsTest(unittest.TestCase):
    """What compose sets when .env says nothing."""

    def setUp(self) -> None:
        """Read the compose file."""
        self.text = COMPOSE.read_text(encoding="utf-8")

    def test_retention_is_on_by_default(self) -> None:
        """Chats, notes and uploads go after 365 days; a new or changed period waits 30 days."""
        self.assertIn("OPEN_WEBUI_CHAT_RETENTION_DAYS: ${OPEN_WEBUI_CHAT_RETENTION_DAYS:-365}", self.text)
        self.assertIn("RETENTION_NOTICE_DAYS: ${RETENTION_NOTICE_DAYS:-30}", self.text)

    def test_notes_are_off(self) -> None:
        """Notes are switched off, and so is the permission to use them."""
        self.assertIn("ENABLE_NOTES: false", self.text)
        self.assertIn("USER_PERMISSIONS_FEATURES_NOTES: false", self.text)


if __name__ == "__main__":
    unittest.main()
