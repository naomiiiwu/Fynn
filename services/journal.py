"""Journal entry construction.

One summarised entry per platform per cycle, not one per transaction — posting
45 separate entries would clutter the ledger and defeat the purpose of a
clearing account. The clearing account carries the net, so that when the real
deposit arrives Xero/QBO's own bank reconciliation can match against it.
"""
from __future__ import annotations

from collections import defaultdict

from models.transaction import (
    CycleResult,
    JournalEntry,
    JournalLine,
    Platform,
    SettlementLine,
    Side,
)
from services.classification import RuleStore


def build_journal(
    platform: Platform,
    cycle: str,
    lines: list[SettlementLine],
    store: RuleStore,
    result: CycleResult,
    resolutions: dict[str, tuple[str, Side]] | None = None,
    reference: str | None = None,
) -> JournalEntry:
    resolutions = resolutions or {}
    blocked = {e.key for e in result.exceptions if not e.resolved}

    buckets: dict[tuple[str, Side], float] = defaultdict(float)
    net = 0.0

    for line in lines:
        if line.key in blocked:
            continue
        if line.key in resolutions:
            account, side = resolutions[line.key]
        else:
            rule = store.find(line)
            if rule is None:
                continue
            account, side = rule.account, rule.side
        buckets[(account, side)] += abs(line.amount)
        net += line.amount

    # The clearing account leads the entry: it carries the net, and it is the
    # line the ledger's own bank reconciliation matches the deposit against.
    clearing = (f"{platform.value} Clearing Account", Side.DEBIT)
    journal_lines = [
        JournalLine(account=clearing[0], side=clearing[1], amount=round(net, 2))
    ]
    for (account, side), amount in sorted(buckets.items()):
        if (account, side) == clearing:
            # An exception resolved *to* the clearing account (a withheld
            # balance, typically) belongs on the same line as the net, not a
            # second line against the same account on the same side.
            journal_lines[0].amount = round(journal_lines[0].amount + amount, 2)
            continue
        journal_lines.append(
            JournalLine(account=account, side=side, amount=round(amount, 2))
        )

    return JournalEntry(
        platform=platform, cycle=cycle, lines=journal_lines,
        reference=reference or f"JE-{platform.value[:3].upper()}-{cycle}",
    )
