"""Cycle orchestration and digest construction.

The digest is the product's surface: accountants who reviewed both a structured
report and a chat interface preferred the report for reviewing and verifying
entries, with conversation reserved for explaining individual exceptions. The
web app renders this payload directly, so the figure on the page and the figure
in the JSON are one reconciliation rather than two that can drift.
"""
from __future__ import annotations

from typing import Optional

from models.transaction import CycleResult, Platform, ReconException, SettlementLine, Side
from services.audit import AuditTrail
from services.classification import RuleStore, is_never_rule
from services.journal import build_journal, build_payout_journals
from services.reconciliation import group_by_platform, group_key, reconcile


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
        stated_totals: Optional[dict[Platform, float]] = None,
    ) -> None:
        self.lines = lines
        # Two kinds of payout figure. One is stated — a platform's own payout
        # row, or what the firm typed from the bank — and is the payout. The
        # other is only what the file's rows add up to, and belongs to that file:
        # a second file for the same platform adds to it rather than replacing
        # it, or a one-line adjustments file would become the month's payout.
        self.stated = dict(stated_totals or {})
        self.reported = {**self.stated, **reported_payouts}
        self.derived = set(self.stated) - set(reported_payouts)
        self.store = store
        self.prior = prior_cycles or []
        self.cycle = cycle
        self.firm = firm
        self.firm_id = firm_id
        self.trail = AuditTrail()
        # What has already been sent to a ledger from this cycle. Without it the
        # screen offers Post again after a successful post, with nothing to say
        # it already happened — so the only way to find out is to go and look in
        # the ledger.
        self.posted: list[dict] = []
        self.resolutions: dict[str, tuple[str, Side]] = {}
        # Fynn's proposed treatment for each exception, made on its own once the
        # month is reconciled. Proposals only: each is still approved by hand.
        self.proposals: dict[str, dict] = {}
        # Exceptions already put to the model, whether or not it proposed
        # anything, so a page reload does not ask again about the same ones.
        self.proposed: set[str] = set()
        self.proposing = False
        self.proposal_error = ""
        self._results: dict[Platform, CycleResult] = {}
        # Exception key → the number the accountant sees. Assigned once and
        # never reused: see _assign_numbers.
        self._numbers: dict[str, int] = {}

        sources = {l.source_ref for l in lines if l.source_ref}
        for src in sorted(sources):
            self.trail.add("source", f"Settlement file {src} ingested")
        self.trail.add("source", f"{len(lines)} lines parsed across {len(set(l.platform for l in lines))} platforms")
        self._note_starter_coverage(lines)

    def _note_starter_coverage(self, lines: list[SettlementLine]) -> None:
        """Record what was classified by Fynn's rules rather than the firm's.

        These lines post without anyone looking at them, which is the whole
        point of a starter pack — so the trail has to say so, or the working
        paper would imply the firm reviewed treatments it never saw.
        """
        starter = {(r.platform, r.label.strip().lower()) for r in self.store.starter_rules}
        if not starter:
            return
        covered = [
            l for l in lines
            if (l.platform, l.label.strip().lower()) in starter
            or (None, l.label.strip().lower()) in starter
        ]
        if not covered:
            return
        labels = sorted({l.label for l in covered})
        self.trail.add(
            "classify",
            f"{len(covered)} lines classified by Fynn's starter rules, not by this firm "
            f"({len(labels)} labels: {', '.join(labels[:6])}"
            f"{'…' if len(labels) > 6 else ''}). "
            f"Override any of them under Settings → Rules.",
        )

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
            # And the same reconciliation split per bank deposit, for ledgers
            # that reconcile against a document rather than a journal.
            result.payout_journals = build_payout_journals(
                platform, self.cycle, plines, self.store, result, self.resolutions
            )
            self._results[platform] = result
        return self._results

    def add_lines(
        self,
        lines: list[SettlementLine],
        reported_payouts: Optional[dict[Platform, float]] = None,
        stated_totals: Optional[dict[Platform, float]] = None,
    ) -> dict[Platform, CycleResult]:
        """Fold another settlement file into the open cycle.

        A firm rarely has all three platforms in one export — files arrive one
        at a time, and each has to join the cycle already in progress without
        discarding decisions already made.
        """
        existing = {l.key for l in self.lines}
        added = [l for l in lines if l.key not in existing]
        self.lines.extend(added)

        for platform, total in (stated_totals or {}).items():
            new = [l for l in added if l.platform == platform]
            if not new:
                continue          # the same file again: nothing to add
            ours = [l for l in lines if l.platform == platform]
            # Only the part of the file not already in the cycle counts. The
            # row total where the whole file is new, since it can differ from
            # the components; the new lines' own amounts where it overlaps.
            if len(new) != len(ours):
                total = sum(l.amount for l in new)
            self.stated[platform] = round(self.stated.get(platform, 0.0) + total, 2)
            if platform in self.derived or platform not in self.reported:
                self.reported[platform] = self.stated[platform]
                self.derived.add(platform)

        if reported_payouts:
            self.reported.update(reported_payouts)
            self.derived -= set(reported_payouts)

        for src in sorted({l.source_ref for l in added if l.source_ref}):
            self.trail.add("source", f"Settlement file {src} ingested")
        if added:
            self.trail.add("source", f"{len(added)} further lines parsed")
            self._note_starter_coverage(added)
        return self.run()

    def results(self) -> dict[Platform, CycleResult]:
        """The current reconciliation, run only if there isn't one.

        Reconciling means classifying every line in the month and rebuilding
        each platform's journals, so a caller that only wants to read the
        current state must not reach for run() and pay for it again.
        """
        if not self._results:
            self.run()
        return self._results

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
        self, key: str, account: str, side: Side, actor: str, save_rule: bool = True,
        scope: str = "label", rerun: bool = True,
    ) -> dict[Platform, CycleResult]:
        """Record an accountant's decision, then re-reconcile.

        `rerun=False` records the decision and leaves the reconciliation to the
        caller. Several decisions arriving together are all known before any of
        them is reconciled, and running the whole month once after the last one
        gives the same answer as running it after each — for a fraction of the
        work, which on a month's worth of lines is the difference between a
        button that responds and one that appears to hang.

        Saving the rule is what makes the next cycle cheaper: the same label
        will not be raised again for this firm.

        scope="category" widens the rule to the platform's own classification,
        so the decision also covers the other fees filed under it and any new
        ones the platform adds later. Only offered where the file carries a
        classification — Lazada does, Shopee does not.
        """
        self.resolutions[key] = (account, side)
        # The key may address a single line or every line carrying one label,
        # so match on both — the rule is written from whichever line it finds.
        covered = [l for l in self.lines if key in (l.key, group_key(l))]
        line = covered[0] if covered else None

        spread = f" ({len(covered)} lines)" if len(covered) > 1 else ""
        self.trail.add("decision", f"Approved — {key} posts to {account}{spread}", actor=actor)

        if line is not None and is_never_rule(line.label):
            # A platform's catch-all bucket holds something different every
            # cycle. Saving a rule would post next month's unknown charge to
            # this month's account without anyone looking at it.
            save_rule = False
            self.trail.add(
                "rule",
                f'No rule saved — "{line.label}" is a catch-all; '
                "its contents change every cycle and must be read each time.",
                actor=actor,
            )

        existing = self.store.find(line) if line is not None else None
        # A decision about one named fee is more specific than a decision about
        # its whole classification, so it is worth saving even when a category
        # rule already covers the line — find() will prefer the label rule.
        overrides_category = scope == "label" and existing is not None and existing.category is not None

        if save_rule and line is not None and (existing is None or overrides_category):
            rule = self.store.add(line, account, side, decided_by=actor, scope=scope)
            target = (
                f'everything classified "{rule.category}"' if rule.category
                else f'"{line.label}"'
            )
            note = (
                f' — overrides the "{existing.category}" rule for this fee only'
                if overrides_category else ""
            )
            if scope == "category" and rule.category is None and line.category:
                # Asked to widen, but the classification is not homogeneous.
                note = (
                    f' — not widened: "{line.category}" holds fees that belong to '
                    "different accounts, so this covers the named fee only"
                )
            self.trail.add(
                "rule",
                f"Rule saved — {line.platform.value} {target} posts to {account}{note}",
                actor=actor,
            )
        return self.run() if rerun else self._results

    def digest(self) -> dict:
        """The payload behind the month-end review link."""
        if not self._results:
            self.run()
        total_lines = len(self.lines)
        open_exceptions = self.open_exceptions()
        return {
            "cycle": self.cycle,
            "firm": self.firm,
            "posted": list(self.posted),
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
                    "category": e.line.category if e.line else None,
                    "evidence": e.evidence.model_dump(mode="json") if e.evidence else None,
                }
                for e in open_exceptions
            ],
            "retention": self.trail.retention_note(),
            "proposals": {e.key: self.proposals[e.key] for e in open_exceptions
                          if e.key in self.proposals},
            "proposing": self.proposing,
            "proposal_error": self.proposal_error,
            "unproposed": len([e for e in open_exceptions if e.key not in self.proposed]),
        }
