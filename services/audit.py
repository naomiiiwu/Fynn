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

    def add(self, kind: str, message: str, actor: str = "Fynn",
            at: datetime | None = None) -> None:
        self._records.append(
            AuditRecord(at=at or datetime.now(timezone.utc), kind=kind,
                        message=message, actor=actor)
        )

    def restore(self, since: datetime, events: list[tuple]) -> None:
        """Put a rebuilt month's history back in the order it happened.

        A month rebuilt after a restart re-reads its files now, so its trail
        would open with today's time and lose every decision made before. What
        is stored says when each thing happened: the files when they arrived,
        then each decision and post at its own time.
        """
        for record in self._records:
            record.at = since
        for at, kind, message, actor in events:
            self.add(kind, message, actor=actor, at=at)
        self._records.sort(key=lambda r: r.at)

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


def working_paper(cycle, accounts=None) -> str:
    """The whole close as one CSV a reviewer can read without the app.

    The trail alone is an event log — useful, and not a working paper. A
    reviewer needs to see the figure, where it came from, and what was decided:
    the payout each platform reported, what Fynn classified against it, what was
    left over, every exception and how it was resolved, and the journal lines
    with the ledger codes they will land on. Then the trail, as evidence.

    One file, because a working paper that arrives in five is not one document.
    Sections are separated by a blank line — CSV has no notion of sections, and
    every spreadsheet in the world renders this correctly.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    results = cycle.run()

    w.writerow(["Fynn working paper"])
    w.writerow(["Firm", getattr(cycle, "firm", "")])
    w.writerow(["Cycle", cycle.cycle])
    w.writerow(["Prepared", datetime.now(timezone.utc).isoformat()])
    w.writerow(["Retention", cycle.trail.retention_note()])
    w.writerow([])

    w.writerow(["RECONCILIATION"])
    w.writerow(["Platform", "Reported payout", "Classified", "Residual",
                "Open exceptions", "Ties out"])
    for platform, r in results.items():
        open_count = len([e for e in r.exceptions if not e.resolved])
        w.writerow([platform.value, f"{r.reported_payout:.2f}",
                    f"{r.classified_total:.2f}", f"{r.residual:.2f}",
                    open_count, "yes" if r.ties_out else "no"])
    w.writerow([])

    exceptions = [(p, e) for p, r in results.items() for e in r.exceptions]
    if exceptions:
        w.writerow(["EXCEPTIONS"])
        w.writerow(["Platform", "Kind", "Amount", "Status", "Resolved to", "Why"])
        for platform, e in exceptions:
            w.writerow([platform.value, e.kind, f"{e.amount:.2f}",
                        "resolved" if e.resolved else "OPEN",
                        getattr(e, "resolved_account", "") or "", e.why])
        w.writerow([])

    for platform, r in results.items():
        if not r.journal:
            continue
        w.writerow([f"JOURNAL — {platform.value}", r.journal.reference])
        w.writerow(["Account", "Side", "Amount", "Ledger code", "Tax"])
        for line in r.journal.lines:
            mapped = accounts.get(line.account) if accounts else None
            w.writerow([line.account, line.side.value, f"{line.amount:.2f}",
                        getattr(mapped, "code", "") or "",
                        getattr(mapped, "tax", "") or ""])
        w.writerow(["", "Debits", f"{r.journal.total_debit:.2f}",
                    "Credits", f"{r.journal.total_credit:.2f}"])
        w.writerow(["", "Balanced", "yes" if r.journal.balanced else "NO"])
        w.writerow([])

        # Only where the platform settles more than once a cycle: these are the
        # documents that will match individual bank deposits.
        if r.payout_journals:
            w.writerow([f"DOCUMENTS — {platform.value}",
                        f"{len(r.payout_journals)} payouts"])
            w.writerow(["Reference", "Settlement period", "Net"])
            for entry in r.payout_journals:
                net = next((l.amount for l in entry.lines
                            if l.account.lower().endswith("clearing account")), 0.0)
                w.writerow([entry.reference, entry.payout, f"{net:.2f}"])
            w.writerow([])

    w.writerow(["AUDIT TRAIL"])
    w.writerow(["timestamp", "type", "actor", "event"])
    for record in cycle.trail.records:
        w.writerow([record.at.isoformat(), record.kind, record.actor, record.message])

    return buf.getvalue()
