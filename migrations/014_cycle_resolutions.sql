-- Fynn — Migration 014: decisions that belong to one cycle rather than to a rule.
--
-- Most approvals become a rule: a fee label, decided once, treated the same way
-- for ever. Some cannot. A refund with no matching sale, a balance the platform
-- withheld, an unexplained residual — each is about *this* settlement, not
-- about a label that will recur, so no rule is written and the decision lived
-- only in the open cycle.
--
-- Which meant a restart re-opened them: the rules re-applied and these did not,
-- so a close that had been finished came back partly undone.

create table if not exists cycle_resolutions (
    firm_id    text not null references firm_profiles(workspace_id) on delete cascade,
    cycle      text not null,
    -- The exception key: either one settlement line, or every line sharing a
    -- label. Both are stable across a rebuild because both derive from the
    -- file's own contents.
    key        text not null,
    account    text not null,
    side       text not null check (side in ('debit', 'credit')),
    actor      text not null default '',
    decided_at timestamptz not null default now(),
    primary key (firm_id, cycle, key)
);
