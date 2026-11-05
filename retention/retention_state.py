"""When each store's retention period took effect on this host.

A period that is switched on, or changed, deletes nothing for the notice period
after it took effect (``RETENTION_NOTICE_DAYS``), so everything that is already
older than the period when it ships gets the notice first. That moment has to
survive restarts, so it is kept on Open WebUI's data volume, next to its
database, as ``.retention-state.json``::

    {"stores": {"chats": {"days": 365, "since": 1793...}}}

A file that cannot be read or does not hold that shape refuses
(:class:`StateRefused`) instead of restarting the grace period. Stdlib only.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: The file's name on the data volume.
STATE_NAME: Final = ".retention-state.json"


class StateRefused(Exception):
    """The state file exists but cannot be used; nothing is swept."""


def deletion_allowed(*, since: float, notice_days: int, now: float) -> bool:
    """Decide whether the notice period after a period took effect has passed.

    Args:
        since: When the period took effect, seconds since the epoch.
        notice_days: The notice period.
        now: The time of the decision.

    Returns:
        Whether deletion may start.
    """
    return now >= since + notice_days * 86_400


@dataclass
class RetentionState:
    """The recorded periods, by store, and the file they live in."""

    path: Path
    stores: dict[str, dict[str, float | int]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> RetentionState:
        """Read the state; a missing file is an empty state.

        Args:
            path: The state file.

        Returns:
            The state.

        Raises:
            StateRefused: If the file cannot be read or is not the state.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise StateRefused(f"cannot read {path}: {exc}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise StateRefused(f"{path} is not JSON") from exc
        stores = data.get("stores") if isinstance(data, dict) else None
        if not isinstance(stores, dict):
            raise StateRefused(f"{path} holds no stores")
        for name, entry in stores.items():
            ok = (
                isinstance(entry, dict)
                and isinstance(entry.get("days"), int)
                and not isinstance(entry.get("days"), bool)
                and isinstance(entry.get("since"), (int, float))
                and not isinstance(entry.get("since"), bool)
            )
            if not ok:
                raise StateRefused(f"{path}: the entry of {name!r} is not a period and a time")
        return cls(path, dict(stores))

    def effective_since(self, store: str, period_days: int, now: float) -> float:
        """Return when a store's period took effect, recording ``now`` for a new or changed period.

        Args:
            store: ``chats``, ``notes`` or ``files``.
            period_days: The period in force.
            now: The current time.

        Returns:
            When that period took effect.

        Raises:
            OSError: If a new entry cannot be written.
        """
        entry = self.stores.get(store)
        if entry is not None and entry["days"] == period_days:
            return float(entry["since"])
        self.stores[store] = {"days": period_days, "since": now}
        self._write()
        return now

    def _write(self) -> None:
        """Write the state atomically, readable by its owner only."""
        body = json.dumps({"stores": self.stores}, sort_keys=True)
        fd, name = tempfile.mkstemp(prefix=".retention-state.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
