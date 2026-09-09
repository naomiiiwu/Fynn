"""Settlement reconciliation.

Scope boundary, deliberately: Fynn checks its classified total against the
payout the platform itself reported in the settlement file. It does not touch
bank data. Matching the actual deposit is done by Xero/QuickBooks' own bank
feed once the entry is posted — the same division A2X uses.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from models.transaction import (
    CycleResult,
    Evidence,
    Platform,
    ReconException,
    SettlementLine,
    Side,
)
from services.classification import RuleStore, classify

WITHHELD_LABELS = {"withheld balance", "held balance", "on hold", "seller balance"}


def _orphan_refund_evidence(
    line: SettlementLine, prior_cycles: list[SettlementLine]
) -> Optional[Evidence]:
    """Look for the originating order in earlier cycles.

    This is one of the five exception strategies. Each kind needs its own
    investigation logic — a generic 'something is wrong' flag is not useful to
    an accountant who has to explain the difference.
    """
    if not line.order_id:
        return None
    candidates = [
        p for p in prior_cycles
        if p.platform == line.platform and p.order_id == line.order_id
    ]
    if not candidates:
        return None
    origin = candidates[0]
    exact = any(abs(abs(p.amount) - abs(line.amount)) < 0.005 for p in candidates)
    confidence = 92 if exact and len(candidates) == 1 else 70
    return Evidence(
        summary=(
            f"Order {line.order_id} settled in cycle {origin.cycle}. "
            f"{'Exact amount, no other candidate in the window.' if exact else 'Amount differs — likely a partial refund.'}"
        ),
        confidence=confidence,
        suggested_account="Sales Returns & Allowances",
        suggested_side=Side.DEBIT,
        source="prior_cycle",
    )


def detect_exceptions(
    platform: Platform,
    cycle: str,
    lines: list[SettlementLine],
    unclassified: list[SettlementLine],
    prior_cycles: list[SettlementLine],
) -> list[ReconException]:
    exceptions: list[ReconException] = []
    sales_orders = {l.order_id for l in lines if l.label.lower() == "sale" and l.order_id}

    for line in unclassified:
        kind = "withheld_balance" if line.label.lower() in WITHHELD_LABELS else "unknown_label"
        if kind == "withheld_balance":
            why = (
                f"{abs(line.amount):.2f} is settled but not released — the platform is "
                "holding it in the seller balance rather than paying it out."
            )
            evidence = Evidence(
                summary=(
                    "Funds appear in the platform's withdrawal record as held, not as a "
                    "shortfall. Holding this in the clearing account keeps the cycle tied out "
                    "until the balance is released."
                ),
                confidence=88,
                suggested_account=f"{platform.value} Clearing Account",
                suggested_side=Side.DEBIT,
                source="rules",
            )
        else:
            why = (
                f'Fee label "{line.label}" has no rule for this firm. It affects the payout '
                "but cannot be posted to an account without a decision."
            )
            evidence = None
        exceptions.append(
            ReconException(
                key=line.key, platform=platform, kind=kind, line=line,
                amount=line.amount, why=why, evidence=evidence,
            )
        )

    # Refunds pointing at orders that are not in this cycle.
    for line in lines:
        if line.label.lower() != "refund" or not line.order_id:
            continue
        if line.order_id in sales_orders:
            continue
        ev = _orphan_refund_evidence(line, prior_cycles)
        kind = "partial_refund" if ev and ev.confidence < 90 else "orphan_refund"
        exceptions.append(
            ReconException(
                key=line.key, platform=platform, kind=kind, line=line, amount=line.amount,
                why=(
                    f"Refund references {line.order_id}, which has no matching sale in "
                    f"cycle {cycle}."
                ),
                evidence=ev,
            )
        )
    return exceptions


def reconcile(
    platform: Platform,
    cycle: str,
    lines: list[SettlementLine],
    reported_payout: float,
    store: RuleStore,
    prior_cycles: Optional[list[SettlementLine]] = None,
    resolutions: Optional[dict[str, tuple[str, Side]]] = None,
) -> CycleResult:
    """Reconcile one platform-cycle.

    resolutions maps an exception key to the (account, side) an accountant
    approved. Nothing is posted for an unresolved exception.
    """
    prior_cycles = prior_cycles or []
    resolutions = resolutions or {}

    classified, unclassified = classify(lines, store)
    exceptions = detect_exceptions(platform, cycle, lines, unclassified, prior_cycles)

    resolved_keys = set(resolutions)
    for exc in exceptions:
        if exc.key in resolved_keys:
            exc.resolved = True
            exc.resolved_account = resolutions[exc.key][0]

    # A line counts toward the classified total if it has a rule and is not the
    # subject of an unresolved exception.
    blocked = {e.key for e in exceptions if not e.resolved}
    total = 0.0
    for line, _rule in classified:
        if line.key in blocked:
            continue
        total += line.amount
    for line in unclassified:
        if line.key in resolutions:
            total += line.amount

    total = round(total, 2)
    residual = round(total - reported_payout, 2)

    if abs(residual) >= 0.005 and not any(not e.resolved for e in exceptions):
        exceptions.append(
            ReconException(
                key=f"{platform.value}|{cycle}|residual",
                platform=platform, kind="residual", amount=residual,
                why=(
                    f"Classified total {total:.2f} does not match the reported payout "
                    f"{reported_payout:.2f}. Difference of {residual:.2f} is unexplained."
                ),
            )
        )

    return CycleResult(
        platform=platform, cycle=cycle, classified_total=total,
        reported_payout=reported_payout, residual=residual, exceptions=exceptions,
    )


def group_by_platform(lines: list[SettlementLine]) -> dict[Platform, list[SettlementLine]]:
    grouped: dict[Platform, list[SettlementLine]] = defaultdict(list)
    for line in lines:
        grouped[line.platform].append(line)
    return dict(grouped)
