"""Tests for schema_dump.py, which CI uses to compare the fixture with the pinned image's schema.

Run from the repo root: ``python3 -m unittest discover -s retention/tests -t retention``.
"""

from __future__ import annotations

import sqlite3
import unittest

from tests.schema_dump import canonical
from tests.synthetic import SCHEMA


def canonical_of(script: str) -> str:
    """Build an in-memory database from a schema script and describe it.

    Args:
        script: ``CREATE`` statements.

    Returns:
        Its canonical form.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(script)
        return canonical(conn)
    finally:
        conn.close()


class CanonicalTest(unittest.TestCase):
    """Order does not matter; content does."""

    def test_constraint_and_statement_order_is_ignored(self) -> None:
        """The same table written in another order, with its indexes swapped, is the same schema."""
        first = (
            "CREATE TABLE t (id TEXT NOT NULL, a TEXT, PRIMARY KEY (id), FOREIGN KEY(a) REFERENCES u (id));\n"
            "CREATE INDEX ix_a ON t (a);\nCREATE INDEX ix_id ON t (id);\n"
        )
        second = (
            "CREATE TABLE t (\n\tid TEXT NOT NULL, \n\ta TEXT, \n"
            "\tFOREIGN KEY(a) REFERENCES u (id), \n\tPRIMARY KEY (id)\n);\n"
            "CREATE INDEX ix_id ON t (id);\nCREATE INDEX ix_a ON t (a);\n"
        )
        self.assertEqual(canonical_of(first), canonical_of(second))

    def test_a_dropped_column_or_index_is_a_difference(self) -> None:
        """Removing anything from the pinned schema changes its canonical form."""
        pinned = canonical_of(SCHEMA)
        self.assertNotEqual(canonical_of(SCHEMA.replace("\tsnapshot JSON, \n", "", 1)), pinned)
        self.assertNotEqual(
            canonical_of(SCHEMA.replace("CREATE INDEX ix_chat_file_chat_id ON chat_file (chat_id);\n", "")), pinned
        )

    def test_commas_inside_parentheses_stay_together(self) -> None:
        """A multi-column constraint is one definition, not several."""
        text = canonical_of("CREATE TABLE t (a TEXT, b TEXT, CONSTRAINT uq UNIQUE (a, b));")
        self.assertIn("  CONSTRAINT uq UNIQUE (a, b)\n", text)


if __name__ == "__main__":
    unittest.main()
