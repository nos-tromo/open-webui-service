"""edge-guard's request maps: which method and path each refusal catches.

nginx evaluates a regex ``map`` top to bottom and takes the first match, so
the maps are parsed from the template and evaluated the same way here.

Run from the repo root: ``python3 -m unittest discover -s retention/tests -t retention``.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "nginx" / "edge-guard.conf.template"

ENTRY = re.compile(r'^\s*(?:"(?P<pattern>[^"]+)"|(?P<word>default))\s+(?P<value>\S+);\s*$')


def parse_map(text: str, variable: str) -> tuple[list[tuple[re.Pattern[str], str]], str]:
    """The regex entries, in order, and the default of the map that sets ``variable``."""
    block = re.search(
        r'^map "\$request_method \$uri" \$' + re.escape(variable) + r" \{\n(?P<body>.*?)^\}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if block is None:
        raise AssertionError(f"no map sets ${variable}")
    entries: list[tuple[re.Pattern[str], str]] = []
    default = ""
    for line in block.group("body").splitlines():
        match = ENTRY.match(line)
        if match is None:
            continue
        if match.group("word"):
            default = match.group("value")
            continue
        pattern = match.group("pattern")
        if not pattern.startswith("~*"):
            raise AssertionError(f"not a case-insensitive regex: {pattern}")
        entries.append((re.compile(pattern[2:], re.IGNORECASE), match.group("value")))
    return entries, default


def evaluate(entries: list[tuple[re.Pattern[str], str]], default: str, request: str) -> str:
    """The value nginx would give: the first matching regex, else the default."""
    for pattern, value in entries:
        if pattern.search(request):
            return value
    return default


@unittest.skipUnless(TEMPLATE.is_file(), "needs the repo's nginx/")
class CodeWritesTest(unittest.TestCase):
    """Creating or changing tools and functions is refused for everyone."""

    def setUp(self) -> None:
        """Parse the map that flags those requests."""
        self.text = TEMPLATE.read_text(encoding="utf-8")
        self.entries, self.default = parse_map(self.text, "owui_code")

    def flagged(self, request: str) -> bool:
        """Whether the request is refused."""
        return evaluate(self.entries, self.default, request) == "1"

    def test_writes_that_run_code_are_refused(self) -> None:
        """Every route that stores and so runs tool or function code, and the rest of their writes."""
        for request in (
            "POST /api/v1/tools/create",
            "POST /api/v1/tools/load/url",
            "POST /api/v1/tools/id/example/update",
            "POST /api/v1/tools/id/example/update/version",
            "POST /api/v1/tools/id/example/toggle",
            "POST /api/v1/tools/id/example/valves/update",
            "POST /api/v1/functions/create",
            "POST /api/v1/functions/sync",
            "POST /api/v1/functions/load/url",
            "POST /api/v1/functions/id/example/update",
            "POST /api/v1/functions/id/example/toggle/global",
            "PUT /api/v1/tools/create",
            "PATCH /api/v1/functions/id/example/update",
            "POST /api/v1/tools",
            "post /API/V1/Tools/create",
        ):
            with self.subTest(request=request):
                self.assertTrue(self.flagged(request))

    def test_reads_and_deletes_pass(self) -> None:
        """Listing, reading and deleting what exists still works, so old entries can be cleared."""
        for request in (
            "GET /api/v1/tools/",
            "GET /api/v1/tools/id/example",
            "GET /api/v1/functions/export",
            "DELETE /api/v1/tools/id/example/delete",
            "DELETE /api/v1/functions/id/example/delete",
            "DELETE /api/v1/functions/id/example/history/1",
        ):
            with self.subTest(request=request):
                self.assertFalse(self.flagged(request))

    def test_neighbouring_routes_pass(self) -> None:
        """Only the tools and functions routers are matched, not routes that merely start alike."""
        for request in (
            "POST /api/v1/toolset",
            "POST /api/v1/functionsx/create",
            "POST /api/v1/terminals/example/files/upload",
            "POST /api/v1/chats/new",
        ):
            with self.subTest(request=request):
                self.assertFalse(self.flagged(request))

    def test_a_flagged_request_answers_403(self) -> None:
        """The flag leads to a 403 with a JSON detail, like the other refusals."""
        handoff = re.search(r"if \(\$owui_code\) \{\s*return (\d{3});\s*\}", self.text)
        self.assertIsNotNone(handoff)
        assert handoff is not None
        location = re.search(
            r"error_page " + handoff.group(1) + r" = @(\w+);.*?location @\1 \{\s*default_type application/json;"
            r"\s*return 403 '\{\"detail\":\"[^\"]+\"\}';",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(location)


@unittest.skipUnless(TEMPLATE.is_file(), "needs the repo's nginx/")
class ToolConfigWritesTest(unittest.TestCase):
    """Tool and terminal servers come from .env; writing their configuration is refused for everyone."""

    def setUp(self) -> None:
        """Parse the map that flags those requests."""
        self.text = TEMPLATE.read_text(encoding="utf-8")
        self.entries, self.default = parse_map(self.text, "owui_toolconfig")

    def flagged(self, request: str) -> bool:
        """Whether the request is refused."""
        return evaluate(self.entries, self.default, request) == "1"

    def test_tool_server_config_writes_are_refused(self) -> None:
        """Every write under both routes, with verify and the orchestrator proxies, which call a URL given to them."""
        for request in (
            "POST /api/v1/configs/tool_servers",
            "POST /api/v1/configs/tool_servers/verify",
            "POST /api/v1/configs/terminal_servers",
            "POST /api/v1/configs/terminal_servers/verify",
            "POST /api/v1/configs/terminal_servers/policy",
            "POST /api/v1/configs/terminal_servers/lifecycle",
            "POST /api/v1/configs/terminal_servers/refresh",
            "PUT /api/v1/configs/terminal_servers",
            "PATCH /api/v1/configs/tool_servers",
            "DELETE /api/v1/configs/terminal_servers",
            "post /API/V1/Configs/Terminal_Servers/verify",
        ):
            with self.subTest(request=request):
                self.assertTrue(self.flagged(request))

    def test_tool_server_config_reads_pass(self) -> None:
        """Reading the configuration and writing other settings still pass."""
        for request in (
            "GET /api/v1/configs/tool_servers",
            "GET /api/v1/configs/terminal_servers",
            "POST /api/v1/configs/banners",
            "POST /api/v1/configs/connections",
            "POST /api/v1/configs/tool_serversx",
        ):
            with self.subTest(request=request):
                self.assertFalse(self.flagged(request))

    def test_a_flagged_request_answers_403(self) -> None:
        """The flag leads to a 403 with a JSON detail, by a hand-off code of its own."""
        handoff = re.search(r"if \(\$owui_toolconfig\) \{\s*return (\d{3});\s*\}", self.text)
        self.assertIsNotNone(handoff)
        assert handoff is not None
        location = re.search(
            r"error_page " + handoff.group(1) + r" = @(\w+);.*?location @\1 \{\s*default_type application/json;"
            r"\s*return 403 '\{\"detail\":\"([^\"]+)\"\}';",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(location)
        assert location is not None
        self.assertEqual(location.group(2), "Tool and terminal servers are configured in .env in this deployment.")
        codes = re.findall(r"error_page (\d{3}) = @", self.text)
        self.assertEqual(len(codes), len(set(codes)))

    def test_the_terminal_proxy_passes_every_refusal(self) -> None:
        """Chat attachments and the file browser reach workbench through the terminal proxy, past every map."""
        variables = re.findall(r'^map "\$request_method \$uri" \$(\w+)', self.text, re.MULTILINE)
        self.assertEqual(variables, ["owui_intake", "owui_rating", "owui_code", "owui_toolconfig"])
        maps = [parse_map(self.text, variable) for variable in variables]
        for request in (
            "POST /api/v1/terminals/workbench/files/upload",
            "POST /api/v1/terminals/workbench/chats/example/files/upload",
            "GET /api/v1/terminals/workbench/files/view",
            "POST /api/v1/terminals/workbench/tools/list_files",
        ):
            for entries, default in maps:
                with self.subTest(request=request):
                    self.assertEqual(evaluate(entries, default, request), "0")


@unittest.skipUnless(TEMPLATE.is_file(), "needs the repo's nginx/")
class IntakeTest(unittest.TestCase):
    """Uploads into Open WebUI's own stores are refused whatever the UI offers."""

    def test_uploads_into_open_webui_are_refused(self) -> None:
        """With the upload permission on, an attachment outside workbench still meets edge-guard's refusal."""
        entries, default = parse_map(TEMPLATE.read_text(encoding="utf-8"), "owui_intake")
        for request in ("POST /api/v1/files/", "POST /api/v1/files", "PUT /api/v1/files/example/data/content/update"):
            with self.subTest(request=request):
                self.assertEqual(evaluate(entries, default, request), "1")


@unittest.skipUnless(TEMPLATE.is_file(), "needs the repo's nginx/")
class ParserTest(unittest.TestCase):
    """The parser reads the existing maps the way nginx does."""

    def test_the_intake_map_keeps_its_exceptions_first(self) -> None:
        """A knowledge base's file removal passes although a broader knowledge rule follows it."""
        entries, default = parse_map(TEMPLATE.read_text(encoding="utf-8"), "owui_intake")
        self.assertEqual(evaluate(entries, default, "POST /api/v1/knowledge/kb/file/remove"), "0")
        self.assertEqual(evaluate(entries, default, "POST /api/v1/knowledge/create"), "1")
        self.assertEqual(evaluate(entries, default, "GET /api/v1/knowledge/"), "0")


if __name__ == "__main__":
    unittest.main()
