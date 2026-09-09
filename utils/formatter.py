"""Cycle orchestration and digest construction.

The digest is the primary surface: accountants who reviewed both a structured
report and a chat interface preferred the report for reviewing and verifying
entries, with conversation reserved for explaining individual exceptions.

The WhatsApp renderings below are deliberately thin views over the same digest.
Nothing is computed twice — the message an accountant reads on their phone is
built from the payload the API returns.
"""
from __future__ import annotations

from typing import Optional

from models.transaction import CycleResult, Platform, ReconException, SettlementLine, Side
from services.audit import AuditTrail
from services.classification import RuleStore
from services.journal import build_journal
from services.reconciliation import group_by_platform, reconcile


class Cycle:
    """One month-end close across every connected platform."""

    def __init__(
        self,
        lines: list[SettlementLine],
        reported_payouts: dict[Platform, float],
        store: RuleStore,
        prior_cycles: Optional[list[SettlementLine]] = None,
        cycle: str = "2026-01",
        firm: str = "Your firm",
        firm_id: Optional[str] = None,
    ) -> None:
        self.lines = lines
        self.reported = reported_payouts
        self.store = store
        self.prior = prior_cycles or []
        self.cycle = cycle
        self.firm = firm
        self.firm_id = firm_id
        self.trail = AuditTrail()
        self.resolutions: dict[str, tuple[str, Side]] = {}
        self._results: dict[Platform, CycleResult] = {}
        # Exception key → the number the accountant sees. Assigned once and
        # never reused: see _assign_numbers.
        self._numbers: dict[str, int] = {}

        sources = {l.source_ref for l in lines if l.source_ref}
        for src in sorted(sources):
            self.trail.add("source", f"Settlement file {src} ingested")
        self.trail.add("source", f"{len(lines)} lines parsed across {len(set(l.platform for l in lines))} platforms")

    def run(self) -> dict[Platform, CycleResult]:
        self._results = {}
        grouped = group_by_platform(self.lines)
        for platform, plines in grouped.items():
            result = reconcile(
                platform=platform, cycle=self.cycle, lines=plines,
                reported_payout=self.reported.get(platform, 0.0),
                store=self.store, prior_cycles=self.prior,
                resolutions=self.resolutions,
            )
            result.journal = build_journal(
                platform, self.cycle, plines, self.store, result, self.resolutions
            )
            self._results[platform] = result
        return self._results

    def add_lines(
        self,
        lines: list[SettlementLine],
        reported_payouts: Optional[dict[Platform, float]] = None,
    ) -> dict[Platform, CycleResult]:
        """Fold another settlement file into the open cycle.

        A firm rarely sends all three platforms at once — files arrive one at a
        time over WhatsApp, and each one has to join the cycle already in
        progress without discarding decisions already made.
        """
        existing = {l.key for l in self.lines}
        added = [l for l in lines if l.key not in existing]
        self.lines.extend(added)
        if reported_payouts:
            self.reported.update(reported_payouts)

        for src in sorted({l.source_ref for l in added if l.source_ref}):
            self.trail.add("source", f"Settlement file {src} ingested")
        if added:
            self.trail.add("source", f"{len(added)} further lines parsed")
        return self.run()

    def open_exceptions(self) -> list[ReconException]:
        """Every unresolved exception, in a stable order across re-runs.

        Sorted by key rather than by whatever order the platforms happened to be
        grouped in, so the list an accountant reads twice reads the same twice.
        """
        if not self._results:
            self.run()
        openings = sorted(
            (e for r in self._results.values() for e in r.exceptions if not e.resolved),
            key=lambda e: e.key,
        )
        self._assign_numbers(openings)
        return openings

    def _assign_numbers(self, openings: list[ReconException]) -> None:
        """Give each exception a number that is never reassigned.

        Numbering the open list positionally would renumber it every time an
        exception is resolved — so an accountant who reads the digest and then
        sends two approvals would have the second one land on a different line
        than the one they read. Numbers are handed out once, on first sight, and
        the resolved ones simply stop appearing.
        """
        for exc in openings:
            if exc.key not in self._numbers:
                self._numbers[exc.key] = len(self._numbers) + 1

    def number_of(self, exc: ReconException) -> int:
        if exc.key not in self._numbers:
            self._assign_numbers([exc])
        return self._numbers[exc.key]

    def exception_at(self, index: int) -> Optional[ReconException]:
        """Look one up by the number shown in the digest."""
        for exc in self.open_exceptions():
            if self._numbers.get(exc.key) == index:
                return exc
        return None

    def approve(
        self, key: str, account: str, side: Side, actor: str, save_rule: bool = True
    ) -> dict[Platform, CycleResult]:
        """Record an accountant's decision, then re-reconcile.

        Saving the rule is what makes the next cycle cheaper: the same label
        will not be raised again for this firm.
        """
        self.resolutions[key] = (account, side)
        line = next((l for l in self.lines if l.key == key), None)
        self.trail.add("decision", f"Approved — {key} posts to {account}", actor=actor)
        if save_rule and line is not None and self.store.find(line) is None:
            self.store.add(line, account, side, decided_by=actor)
            self.trail.add(
                "rule",
                f'Rule saved — {line.platform.value} "{line.label}" posts to {account}',
                actor=actor,
            )
        return self.run()

    def digest(self) -> dict:
        """The payload behind the month-end review link."""
        if not self._results:
            self.run()
        total_lines = len(self.lines)
        open_exceptions = self.open_exceptions()
        return {
            "cycle": self.cycle,
            "firm": self.firm,
            "lines_total": total_lines,
            "lines_classified": total_lines - len(open_exceptions),
            "open_exceptions": len(open_exceptions),
            "platforms": [
                {
                    "platform": p.value,
                    "classified_total": r.classified_total,
                    "reported_payout": r.reported_payout,
                    "residual": r.residual,
                    "ties_out": r.ties_out,
                    "journal_balanced": r.journal.balanced if r.journal else None,
                    "journal_reference": r.journal.reference if r.journal else None,
                }
                for p, r in self._results.items()
            ],
            "exceptions": [
                {
                    "index": self._numbers[e.key],
                    "key": e.key,
                    "platform": e.platform.value,
                    "kind": e.kind,
                    "amount": e.amount,
                    "why": e.why,
                    "evidence": e.evidence.model_dump(mode="json") if e.evidence else None,
                }
                for e in open_exceptions
            ],
            "retention": self.trail.retention_note(),
        }

    # ── WhatsApp renderings ───────────────────────────────────────────────────

    def whatsapp_summary(self) -> str:
        d = self.digest()
        n = d["open_exceptions"]
        if n == 0:
            return (
                f"Cycle {d['cycle']} is ready. All {d['lines_total']} transactions "
                f"classified and every platform ties out. Nothing needs your input."
            )
        return (
            f"Cycle {d['cycle']} is ready. {d['lines_classified']} of {d['lines_total']} "
            f"transactions are classified. {n} need your call."
        )

    def whatsapp_digest(self) -> str:
        """The full month-end review message: totals, then what needs a decision."""
        d = self.digest()
        parts = [f"*Cycle {d['cycle']}* — {d['firm']}", ""]

        for p in d["platforms"]:
            tick = "✅" if p["ties_out"] else "⚠️"
            parts.append(
                f"{tick} *{p['platform']}* — classified {p['classified_total']:,.2f} "
                f"vs reported {p['reported_payout']:,.2f}"
            )
            if abs(p["residual"]) >= 0.005:
                parts.append(f"    residual {p['residual']:,.2f}")

        if not d["exceptions"]:
            parts += ["", "Every platform ties out. Reply *post* to send the journals."]
            return "\n".join(parts)

        parts += ["", f"*{d['open_exceptions']} need your call:*"]
        for e in d["exceptions"]:
            parts.append(f"{e['index']}. {e['platform']} · {e['amount']:,.2f} — {e['why']}")
            ev = e["evidence"]
            if ev:
                suggestion = ev.get("suggested_account")
                if suggestion:
                    parts.append(
                        f"    Suggested: {suggestion} ({ev['confidence']}% confidence)"
                    )

        parts += [
            "",
            "Reply *approve 1* to accept a suggestion, "
            "*approve 1 Marketing Expense* to choose the account, "
            "or *why 1* for the reasoning.",
        ]
        return "\n".join(parts)

    def whatsapp_exception(self, index: int) -> str:
        """One exception in full, with whatever evidence exists for it."""
        exc = self.exception_at(index)
        if exc is None:
            return f"There's no open exception {index}. Reply *digest* for the current list."

        parts = [
            f"*Exception {index}* — {exc.platform.value}",
            f"Amount: {exc.amount:,.2f}",
            f"Kind: {exc.kind.replace('_', ' ')}",
            "",
            exc.why,
        ]
        if exc.evidence:
            parts += ["", f"_{exc.evidence.summary}_"]
            if exc.evidence.suggested_account:
                side = exc.evidence.suggested_side.value if exc.evidence.suggested_side else "debit"
                parts.append(
                    f"Suggested: {exc.evidence.suggested_account} ({side}) "
                    f"— {exc.evidence.confidence}% confidence"
                )
            parts.append(f"Source: {exc.evidence.source.replace('_', ' ')}")
        else:
            parts += ["", "No suggestion available — this one needs your judgement."]

        parts += ["", f"Reply *approve {index} <account>* to decide."]
        return "\n".join(parts)
