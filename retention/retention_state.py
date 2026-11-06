"""When each store's retention period took effect on this host.

A period that is switched on deletes nothing for the notice period after it
took effect (``RETENTION_NOTICE_DAYS``), so everything that is already older
than the period when it ships gets the notice first. A changed period never
postpones what the period in force already made due, and never deletes sooner
than announced: a longer one applies at once (within a running first notice,
at its end); a shorter one gives the notice only to what it makes due sooner,
never ending before a notice already running, while the rule in force still
deletes meanwhile. Changing the notice alone moves nothing. The state has to
survive restarts, so it is kept on Open WebUI's data volume, next to its
database, as ``.retention-state.json``::

    {"stores": {"chats": {"days": 30, "since": 1793..., "grace_until": 1796...,
                          "in_force_days": 365, "in_force_grace": 1790...}}}

An entry of the release before (``days`` and ``since`` only) is read as a
notice that started at ``since`` and rewritten with its end. A file that cannot
be read, does not hold that shape, or holds impossible values refuses
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


#: The longest notice an entry may record, in days (as ``RETENTION_NOTICE_DAYS``).
MAX_ENTRY_NOTICE_DAYS: Final = 365


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _days(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


@dataclass(frozen=True)
class StoreRule:
    """A store's retention rule as it stands.

    Attributes:
        since: When the period took effect.
        days: The period.
        grace_until: Until when the period does not delete on its own.
        in_force_days: The period in force before a change, deleting meanwhile.
        in_force_grace: Since when that period deletes.
    """

    since: float
    days: int
    grace_until: float
    in_force_days: int | None = None
    in_force_grace: float | None = None

    def sweep_days(self, now: float) -> int | None:
        """Return the age past which a sweep at ``now`` deletes, if it may delete at all.

        Args:
            now: The sweep's time.

        Returns:
            The period after the notice; meanwhile the longer of it and the
            period in force once that one deletes; ``None`` while nothing may go.
        """
        if now >= self.grace_until:
            return self.days
        if self.in_force_days is not None and self.in_force_grace is not None and now >= self.in_force_grace:
            return max(self.days, self.in_force_days)
        return None

    def entry(self) -> dict[str, float | int]:
        """Return the state file's entry for this rule.

        Returns:
            The entry.
        """
        out: dict[str, float | int] = {"days": self.days, "since": self.since, "grace_until": self.grace_until}
        if self.in_force_days is not None and self.in_force_grace is not None:
            out.update(in_force_days=self.in_force_days, in_force_grace=self.in_force_grace)
        return out


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
            if not (isinstance(entry, dict) and _days(entry.get("days")) and _number(entry.get("since"))):
                raise StateRefused(f"{path}: the entry of {name!r} is not a period and a time")
            since = float(entry["since"])
            grace = entry.get("grace_until", since)
            if not _number(grace) or not since <= float(grace) <= since + MAX_ENTRY_NOTICE_DAYS * 86_400:
                raise StateRefused(f"{path}: the entry of {name!r} holds an impossible notice end")
            has_days, has_grace = "in_force_days" in entry, "in_force_grace" in entry
            if has_days != has_grace or (
                has_days
                and not (
                    _days(entry["in_force_days"])
                    and _number(entry["in_force_grace"])
                    and float(entry["in_force_grace"]) <= float(grace)
                )
            ):
                raise StateRefused(f"{path}: the entry of {name!r} holds an impossible period in force")
        return cls(path, dict(stores))

    def rule(self, store: str, period_days: int, notice_days: int, now: float, *, record: bool = True) -> StoreRule:
        """Return a store's rule, moving to ``period_days`` when it is new or changed.

        Args:
            store: ``chats``, ``notes`` or ``files``.
            period_days: The period configured now.
            notice_days: The notice configured now.
            now: The current time.
            record: Write a new, changed or upgraded entry (the dry run only reads).

        Returns:
            The rule.

        Raises:
            OSError: If an entry cannot be written.
        """
        fresh = StoreRule(now, period_days, now + notice_days * 86_400)
        entry = self.stores.get(store)
        if entry is None or float(entry["since"]) > now:
            rule = fresh
        else:
            since, recorded = float(entry["since"]), int(entry["days"])
            legacy = "grace_until" not in entry
            grace = since + notice_days * 86_400 if legacy else float(entry["grace_until"])
            in_force = entry.get("in_force_days")
            in_force_grace = entry.get("in_force_grace")
            if recorded == period_days:
                rule = StoreRule(
                    since,
                    recorded,
                    grace,
                    None if in_force is None else int(in_force),
                    None if in_force_grace is None else float(in_force_grace),
                )
                if not (legacy and record):
                    return rule
            else:
                if now >= grace or in_force is None or in_force_grace is None:
                    effective, effective_grace = recorded, grace
                else:
                    effective, effective_grace = max(int(in_force), recorded), float(in_force_grace)
                if period_days >= effective:
                    rule = StoreRule(now, period_days, max(now, effective_grace))
                else:
                    running = grace if grace > now else now
                    rule = StoreRule(
                        now, period_days, max(fresh.grace_until, running), effective, effective_grace
                    )
        if record:
            self.stores[store] = rule.entry()
            self._write()
        return rule

    def forget(self, store: str) -> None:
        """Forget a store's entry (retention switched off), so switching on gives a fresh notice.

        Raises:
            OSError: If the state cannot be written.
        """
        if self.stores.pop(store, None) is not None:
            self._write()

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
