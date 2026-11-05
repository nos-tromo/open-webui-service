"""What every retention record shares: who deletes, the operation, the time format.

The sweeper deletes on its own schedule, with no caller, so every record names
the system as its actor and carries no purpose or request id. A deletion is one
``delete`` / ``retention.sweep`` event per batch; ids beyond one event's worth go
into ``retention.sweep.ids`` continuation events pointing at it. Stdlib only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

from audit_event import Actor, AuditEmitter, Target
from audit_runtime import id_chunks

#: The compose project: every chain the sidecar writes names it as its service.
SERVICE: Final = "open-webui-service"
#: The sweeper acts on its own schedule; no caller, no purpose.
SYSTEM: Final = Actor(None, "system")
SWEEP: Final = "retention.sweep"
SWEEP_IDS: Final = "retention.sweep.ids"


def counted(count: int, noun: str) -> str:
    """Write a count with its noun, singular or plural.

    Args:
        count: How many.
        noun: The noun's plural, ending in ``s``.

    Returns:
        E.g. ``1 chat`` or ``3 chats``.
    """
    return f"{count} {noun[:-1] if count == 1 else noun}"


def iso_utc(moment: float) -> str:
    """Format a Unix time the way the events do.

    Args:
        moment: Seconds since the epoch.

    Returns:
        E.g. ``2026-01-31T12:00:00Z``.
    """
    return datetime.fromtimestamp(moment, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def emit_deletion(
    emitter: AuditEmitter,
    kind: str,
    ids: Sequence[str],
    criteria: Mapping[str, object],
    *,
    container: str | None = None,
    outcome: str = "ok",
    parent_event_id: str | None = None,
) -> dict[str, object]:
    """Record one batch of deletions, splitting the ids instead of truncating them.

    Args:
        emitter: The started emitter.
        kind: What was deleted: ``file`` or ``chat``.
        ids: The references of everything deleted, in order.
        criteria: The rule (period, cutoff) and the batch's facts.
        container: Where the objects lived, e.g. ``cache/audio/transcriptions``.
        outcome: ``ok``, or ``error`` for what could not be deleted.
        parent_event_id: The record an ``error`` event reports on.

    Returns:
        The primary event as written; its continuations follow it.

    Raises:
        AuditWriteError: If an event could not be written.
        RuntimeError: If the emitter is closed.
    """
    chunks = id_chunks(ids) or [[]]
    primary = emitter.emit(
        "delete",
        SWEEP,
        SYSTEM,
        outcome=outcome,
        parent_event_id=parent_event_id,
        target=Target(kind=kind, ids=chunks[0], count=len(ids), container=container),
        criteria=criteria,
    )
    for chunk in chunks[1:]:
        emitter.emit(
            "delete",
            SWEEP_IDS,
            SYSTEM,
            outcome=outcome,
            parent_event_id=str(primary["event_id"]),
            target=Target(kind=kind, ids=chunk, count=len(chunk), container=container),
        )
    return primary
