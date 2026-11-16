"""The compose file's defaults: retention on, notes and in-process code off, workbench the only tool server.

Run from the repo root: ``python3 -m unittest discover -s retention/tests -t retention``.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests.test_edge_guard import TEMPLATE, evaluate, parse_map

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "compose.yaml"


def service(text: str, name: str) -> str:
    """The lines of one service in the compose file, up to the next line indented like its name."""
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)^  (?=\S)", text)
    if match is None:
        raise AssertionError(f"no service {name}")
    return match.group(1)


def upstream_switch(value: str) -> bool:
    """How the pinned image's env.py reads ENABLE_TOOL_SERVERS once compose has set it.

    ``os.getenv('ENABLE_TOOL_SERVERS', 'True').lower() == 'true'``: the default applies only to an unset variable, and
    compose always sets it, to "" when the key is unset.
    """
    return value.lower() == "true"


@unittest.skipUnless(COMPOSE.is_file(), "needs the repo's docker/")
class ComposeDefaultsTest(unittest.TestCase):
    """What compose sets when .env says nothing."""

    def setUp(self) -> None:
        """Read the compose file."""
        self.text = COMPOSE.read_text(encoding="utf-8")

    def test_retention_is_on_by_default(self) -> None:
        """Chats, notes and uploads go after 365 days; a new period waits 30 days."""
        self.assertIn("OPEN_WEBUI_CHAT_RETENTION_DAYS: ${OPEN_WEBUI_CHAT_RETENTION_DAYS:-365}", self.text)
        self.assertIn("RETENTION_NOTICE_DAYS: ${RETENTION_NOTICE_DAYS:-30}", self.text)

    def test_notes_are_off(self) -> None:
        """Notes are switched off, and so is the permission to use them."""
        self.assertIn("ENABLE_NOTES: false", self.text)
        self.assertIn("USER_PERMISSIONS_FEATURES_NOTES: false", self.text)

    def test_no_code_runs_inside_open_webui(self) -> None:
        """Tools, functions, the code interpreter, code execution and sub-agents are off."""
        for line in (
            "ENABLE_TOOLS: false",
            "ENABLE_FUNCTIONS: false",
            "ENABLE_CODE_INTERPRETER: false",
            "ENABLE_CODE_EXECUTION: false",
            "USER_PERMISSIONS_FEATURES_AUTOMATIONS: false",
            "ENABLE_SUBAGENTS: false",
        ):
            with self.subTest(line=line):
                self.assertRegex(self.text, rf"(?m)^ +{re.escape(line)}$")

    def test_the_banners_name_the_periods_in_english_and_german(self) -> None:
        """WEBUI_BANNERS is two info banners: the chats' period and the workspace's, each from its own variable."""
        match = re.search(r"^ +WEBUI_BANNERS: '(.*)'$", self.text, re.MULTILINE)
        self.assertIsNotNone(match)
        assert match is not None
        raw = match.group(1)
        self.assertEqual(raw.count("${OPEN_WEBUI_CHAT_RETENTION_DAYS:-365}"), 3)
        self.assertEqual(raw.count("${WORKBENCH_RETENTION_DAYS:-7}"), 3)
        chats, workspace = json.loads(
            raw.replace("${OPEN_WEBUI_CHAT_RETENTION_DAYS:-365}", "365").replace("${WORKBENCH_RETENTION_DAYS:-7}", "7")
        )
        self.assertEqual(
            {key: chats[key] for key in ("id", "type", "dismissible")},
            {"id": "retention-period-365", "type": "info", "dismissible": True},
        )
        self.assertIsInstance(chats["timestamp"], int)
        self.assertEqual(chats["content"], "Chats are deleted automatically 365 days after they were started.")
        self.assertEqual(
            chats["i18n"], {"de-DE": {"content": "Chats werden 365 Tage nach ihrem Beginn automatisch gelöscht."}}
        )
        self.assertEqual(
            {key: workspace[key] for key in ("id", "type", "dismissible")},
            {"id": "workspace-period-7", "type": "info", "dismissible": True},
        )
        self.assertIsInstance(workspace["timestamp"], int)
        self.assertNotEqual(workspace["timestamp"], chats["timestamp"])
        self.assertEqual(workspace["content"], "Files in the workspace are deleted 7 days after they were added.")
        self.assertEqual(
            workspace["i18n"],
            {"de-DE": {"content": "Dateien im Arbeitsbereich werden 7 Tage nach dem Hinzufügen gelöscht."}},
        )

    def test_tool_servers_follow_the_key(self) -> None:
        """Tool servers are on only with workbench's key; unset, the switch is empty, which upstream reads as off."""
        self.assertRegex(self.text, r"(?m)^ +ENABLE_TOOL_SERVERS: \$\{OPEN_WEBUI_WORKBENCH_KEY:\+true\}$")
        self.assertNotIn("ENABLE_TOOL_SERVERS: false", self.text)
        self.assertFalse(upstream_switch(""))
        self.assertTrue(upstream_switch("true"))
        # ENABLE_TOOL_SERVERS derives from ENABLE_PLUGINS (env.py), which stays at its default.
        self.assertNotRegex(self.text, r"(?m)^ +ENABLE_PLUGINS:")

    def test_one_workbench_connection_with_filesystem_uploads(self) -> None:
        """TERMINAL_SERVER_CONNECTIONS is workbench alone: bearer key, chat uploads into the chat's folder, every user."""
        match = re.search(r"^ +TERMINAL_SERVER_CONNECTIONS: '(.*)'$", self.text, re.MULTILINE)
        self.assertIsNotNone(match)
        assert match is not None
        raw = (
            match.group(1)
            .replace("${WORKBENCH_URL:-http://workbench-api:8000}", "http://workbench-api:8000")
            .replace("${OPEN_WEBUI_WORKBENCH_KEY:-}", "synthetic-key")
        )
        self.assertNotIn("$", raw)
        (connection,) = json.loads(raw)
        self.assertEqual(
            connection,
            {
                "id": "workbench",
                "name": "Workbench",
                "enabled": True,
                "server_type": "orchestrator",
                "url": "http://workbench-api:8000",
                "path": "/openapi.json",
                "key": "synthetic-key",
                "auth_type": "bearer",
                "config": {
                    "chat_uploads": "filesystem",
                    "contexts": {"chat": {"context_id": "chat_id"}},
                    "access_grants": [{"principal_type": "user", "principal_id": "*", "permission": "read"}],
                },
            },
        )

    def test_workbench_is_preselected_and_shows_files_inline(self) -> None:
        """Every model selects workbench for a new chat, and every account sees displayed files in the answer."""
        for name, expected in (
            ("DEFAULT_MODEL_METADATA", {"terminalId": "workbench"}),
            ("DEFAULT_INTERFACE_SETTINGS", {"terminalFileDisplay": "inline"}),
        ):
            match = re.search(rf"(?m)^ +{name}: '(.*)'$", self.text)
            self.assertIsNotNone(match, name)
            assert match is not None
            self.assertEqual(json.loads(match.group(1)), expected)

    def test_terminal_proxy_is_excluded_from_open_webui_audit(self) -> None:
        """Open WebUI's audit skips the terminal proxy, whose URLs carry file names, and nothing else."""
        match = re.search(r'(?m)^ +AUDIT_EXCLUDED_PATHS: "(.*)"$', self.text)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), "/terminals")
        # The pinned image's utils/audit.py skips what this pattern, built from the list, matches.
        paths = [path.strip().lstrip("/") for path in match.group(1).split(",") if path.strip().lstrip("/")]
        skipped = re.compile(r"^/api(?:/v1)?/(" + "|".join(paths) + r")\b")
        for path in ("/api/v1/terminals/workbench/files/view", "/api/v1/terminals/", "/api/terminals/workbench"):
            with self.subTest(path=path):
                self.assertIsNotNone(skipped.match(path))
        for path in ("/api/v1/chats/new", "/api/v1/folders/", "/api/v1/configs/terminal_servers"):
            with self.subTest(path=path):
                self.assertIsNone(skipped.match(path))

    def test_the_access_log_stays_off(self) -> None:
        """uvicorn's access log, which would print every terminal URL with its query and so file names, is off."""
        block = service(self.text, "open-webui")
        match = re.search(r"(?m)^    command: (\[.*\])$", block)
        self.assertIsNotNone(match)
        assert match is not None
        # Arguments replace start.sh's own (--workers ${UVICORN_WORKERS:-1} --ws-per-message-deflate
        # ${UVICORN_WS_PER_MESSAGE_DEFLATE:-true}), so those are repeated.
        self.assertEqual(
            json.loads(match.group(1)),
            ["bash", "start.sh", "--workers", "1", "--ws-per-message-deflate", "true", "--no-access-log"],
        )
        # At startup Open WebUI hands the loggers listed here (default uvicorn.access) a handler of its own, which
        # would switch the access log back on.
        self.assertRegex(block, r'(?m)^ +AUDIT_UVICORN_LOGGER_NAMES: ""$')

    def test_tool_call_backstop_exceeds_workbench_limits(self) -> None:
        """Open WebUI waits for a tool call longer than workbench can take to answer it."""
        match = re.search(r'(?m)^ +AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER: "(\d+)"$', self.text)
        self.assertIsNotNone(match)
        assert match is not None
        # workbench docs/configuration.md: queue wait 60 s + wall time 120 s + the 300 s it waits for the executor's
        # answer after that.
        self.assertGreaterEqual(int(match.group(1)), 60 + 120 + 300)

    @unittest.skipUnless(TEMPLATE.is_file(), "needs the repo's nginx/")
    def test_users_may_attach_files_which_go_to_workbench(self) -> None:
        """The upload permission only shows the control; edge-guard still refuses uploads into Open WebUI's stores."""
        # The pinned UI attaches, into workbench too, only with this permission.
        self.assertRegex(self.text, r"(?m)^ +USER_PERMISSIONS_CHAT_FILE_UPLOAD: true$")
        self.assertRegex(self.text, r"(?m)^ +USER_PERMISSIONS_CHAT_WEB_UPLOAD: false$")
        # An attachment in a chat whose terminal is not Workbench goes to /api/v1/files and gets edge-guard's 403.
        entries, default = parse_map(TEMPLATE.read_text(encoding="utf-8"), "owui_intake")
        self.assertEqual(evaluate(entries, default, "POST /api/v1/files/"), "1")
        self.assertEqual(evaluate(entries, default, "POST /api/v1/terminals/workbench/files/upload"), "0")

    def test_open_webui_joins_workbench_net(self) -> None:
        """Open WebUI reaches workbench over the external workbench-net, under the name .env may set."""
        block = service(self.text, "open-webui")
        networks = re.search(r"(?ms)^    networks:\n(.*?)(?=^    \S|\Z)", block)
        self.assertIsNotNone(networks)
        assert networks is not None
        self.assertRegex(networks.group(1), r"(?m)^      workbench-net: \{\}$")
        self.assertRegex(
            self.text,
            r"(?m)^  workbench-net:\n    external: true\n    name: \$\{WORKBENCH_NET:-workbench-net\}$",
        )

    def test_direct_tool_servers_stay_off(self) -> None:
        """Users cannot connect tool servers of their own from the browser."""
        self.assertRegex(self.text, r"(?m)^ +USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS: false$")


if __name__ == "__main__":
    unittest.main()
