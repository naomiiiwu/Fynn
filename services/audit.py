"""Audit trail.

Every accountant consulted raised traceability independently. The requirement
is that any posted figure can be walked back to the source settlement file, and
that every decision carries a timestamp and a named actor.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from models.transaction import AuditRecord

# Statutory retention. Verify the exact period for the jurisdiction you operate
# in before relying on this constant.
RETENTION_YEARS = 5


class AuditTrail:
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def add(self, kind: str, message: str, actor: str = "Fynn") -> None:
        self._records.append(
            AuditRecord(at=datetime.now(timezone.utc), kind=kind,
                        message=message, actor=actor)
        )

    @property
    def records(self) -> list[AuditRecord]:
        return list(self._records)

    def to_dicts(self) -> list[dict]:
        return [r.model_dump(mode="json") for r in self._records]

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "type", "actor", "event"])
        for r in self._records:
            writer.writerow([r.at.isoformat(), r.kind, r.actor, r.message])
        return buf.getvalue()

    def retention_note(self) -> str:
        return (
            f"Source settlement files and this trail are retained for "
            f"{RETENTION_YEARS} years from the cycle date."
        )
