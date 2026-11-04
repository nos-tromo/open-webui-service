"""Print an Open WebUI database's schema, as ``fixtures/webui-schema.sql`` or canonically.

The tests build their databases from that fixture, so it must be the schema the
pinned image creates. Open WebUI's migrations rebuild some tables, and the order
in which they write a table's constraints and indexes varies from run to run, so
CI compares the canonical form, in which every table's columns and constraints
and every statement are sorted::

    python3 retention/tests/schema_dump.py <webui.db> > retention/tests/fixtures/webui-schema.sql
    python3 retention/tests/schema_dump.py --canonical <webui.db>
    python3 retention/tests/schema_dump.py --canonical --from-sql retention/tests/fixtures/webui-schema.sql

The first regenerates the fixture after an image bump (CLAUDE.md § Retention);
the other two print what CI compares. Stdlib only.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

_SPACE = re.compile(r"\s+")


def dump(conn: sqlite3.Connection) -> str:
    """Read a database's schema statements in creation order.

    Args:
        conn: The database.

    Returns:
        One statement per entry of ``sqlite_master``, each ending in ``;``.
    """
    rows = conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
    return "".join(f"{str(row[0]).strip()};\n" for row in rows)


def _top_level_parts(body: str) -> list[str]:
    """Split a table's definition list at the commas outside parentheses and quotes.

    Args:
        body: The text between a ``CREATE TABLE``'s outer parentheses.

    Returns:
        Its column and constraint definitions, whitespace collapsed.
    """
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for index, char in enumerate(body):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"`[":
            quote = "]" if char == "[" else char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:index])
            start = index + 1
    parts.append(body[start:])
    return [_SPACE.sub(" ", part).strip() for part in parts if part.strip()]


def _canonical_statement(kind: str, sql: str) -> str:
    """Normalize one schema statement so that order and spacing do not matter.

    Args:
        kind: ``table``, ``index``, ``view`` or ``trigger``.
        sql: Its ``CREATE`` statement.

    Returns:
        A table as its sorted definitions, one per line; anything else with
        its whitespace collapsed.
    """
    text = sql.strip()
    if kind != "table":
        return _SPACE.sub(" ", text)
    start, end = text.index("("), text.rindex(")")
    head = _SPACE.sub(" ", text[:start]).strip()
    tail = _SPACE.sub(" ", text[end + 1 :]).strip()
    lines = [f"{head} ({tail})" if tail else head]
    lines.extend(f"  {part}" for part in sorted(_top_level_parts(text[start + 1 : end])))
    return "\n".join(lines)


def canonical(conn: sqlite3.Connection) -> str:
    """Describe a schema independently of the order it was created in.

    Args:
        conn: The database.

    Returns:
        Every statement of ``sqlite_master``, normalized and sorted.
    """
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    statements = sorted((str(kind), str(name), _canonical_statement(str(kind), str(sql))) for kind, name, sql in rows)
    return "".join(f"{statement}\n" for _, _, statement in statements)


def main(argv: Sequence[str]) -> int:
    """Print a schema.

    Args:
        argv: ``[--canonical] [--from-sql] <path>``, without the program name:
            ``--from-sql`` reads a schema file instead of a database.

    Returns:
        The exit status.
    """
    flags = {arg for arg in argv if arg.startswith("--")}
    paths = [arg for arg in argv if not arg.startswith("--")]
    if len(paths) != 1 or not flags <= {"--canonical", "--from-sql"}:
        print("usage: schema_dump.py [--canonical [--from-sql]] <webui.db | schema.sql>", file=sys.stderr)
        return 2
    if "--from-sql" in flags:
        conn = sqlite3.connect(":memory:")
        conn.executescript(Path(paths[0]).read_text(encoding="utf-8"))
    else:
        conn = sqlite3.connect(f"file:{urllib.parse.quote(paths[0])}?mode=ro", uri=True)
    try:
        sys.stdout.write(canonical(conn) if "--canonical" in flags else dump(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
