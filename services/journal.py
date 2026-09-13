"""Journal entry construction.

One summarised entry per platform per cycle, not one per transaction — posting
45 separate entries would clutter the ledger and defeat the purpose of a
clearing account. The clearing account carries the net, so that when the real
deposit arrives Xero/QBO's own bank reconciliation can match against it.
"""
from __future__ import annotations

import re
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
from services.reconciliation import is_blocked, resolution_for


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
        if is_blocked(line, blocked):
            continue
        decided = resolution_for(line, resolutions)
        if decided is not None:
            account, _declared = decided
        else:
            rule = store.find(line)
            if rule is None:
                continue
            account = rule.account

        # The side is the line's own sign, always — money in is a credit to its
        # account, money out a debit. A decision names the *account*; the
        # direction is a fact about the amount, not a choice. Honouring a
        # contradicting side would unbalance the entry, because the clearing
        # line carries the net and the rest must mirror it. A reversal landing
        # on the opposite side of its original charge is a contra entry, which
        # is what it should be.
        side = Side.CREDIT if line.amount > 0 else Side.DEBIT
        buckets[(account, side)] += abs(line.amount)
        net += line.amount

    # The clearing account leads the entry: it carries the net — what the
    # platform actually deposits — and every other line explains how the gross
    # sales became that figure. Adapters read it as the total of the document
    # they post, which is why it is first and why it is alone on its side.
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


def _payout_slug(payout: str) -> str:
    """A short, stable tag for a settlement period, safe in a reference.

    Stable matters more than pretty: the idempotency key is derived from the
    reference, so a slug that changed between runs would let a retry create a
    second invoice in a client's books.
    """
    text = re.sub(r"[^A-Za-z0-9]+", "", payout or "").upper()
    return text[:16] or "ALL"


def build_payout_journals(
    platform: Platform,
    cycle: str,
    lines: list[SettlementLine],
    store: RuleStore,
    result: CycleResult,
    resolutions: dict[str, tuple[str, Side]] | None = None,
) -> list[JournalEntry]:
    """The same reconciliation, split one entry per payout.

    A cycle is a month because that is how a close is named. A payout is a bank
    deposit, and Lazada makes four of them in a January. One document covering
    all four reconciles against none of them, so the ledger's bank feed has
    nothing to offer the accountant and every deposit is coded by hand.

    Grouping is on the platform's own stated period, never on a date Fynn
    inferred: the platform decides what it paid against, and a line carried into
    the next statement belongs to the statement that paid it.

    Returns a single cycle-wide entry when the platform settles once — Shopee's
    monthly income statement — so nothing is split that was never apart.
    """
    groups: dict[str, list[SettlementLine]] = defaultdict(list)
    for line in lines:
        groups[line.payout or cycle].append(line)

    if len(groups) <= 1:
        return []

    entries = []
    base = f"JE-{platform.value[:3].upper()}-{cycle}"
    for payout, payout_lines in sorted(groups.items()):
        entry = build_journal(
            platform, cycle, payout_lines, store, result, resolutions,
            reference=f"{base}-{_payout_slug(payout)}",
        )
        entry.payout = payout
        entries.append(entry)
    return entries
