"""
Fynn Orchestrator Agent.

Coordinates all sub-agents to run the full bookkeeping pipeline.
Replaces the monolithic BookkeeperAgent.

Pipeline:
  1. Parallel ingestion (one IngestionAgent per platform)
  2. Per-platform: Reconciliation → Anomaly → P&L → Ledger (journal entries)
  3. ExcelAgent — generate .xlsx, upload to Supabase Storage
  4. WhatsAppAgent — send summary + Excel attachment
"""

import asyncio
from dataclasses import dataclass, field

from models.ledger import JournalEntry
from models.user_profile import UserProfile

from .anomaly_agent import AnomalyAgent
from .excel_agent import ExcelAgent
from .ingestion_agent import IngestionAgent, IngestionResult
from .ledger_agent import LedgerAgent
from .pnl_agent import PnLAgent
from .reconciliation_agent import ReconciliationAgent
from .whatsapp_agent import WhatsAppAgent


@dataclass
class OrchestratorResult:
    pnl_reports:     list[dict]           = field(default_factory=list)
    pipeline_status: dict[str, str]       = field(default_factory=dict)
    combined_pnl:    dict                 = field(default_factory=dict)
    journal_entries: list[JournalEntry]   = field(default_factory=list)


class OrchestratorAgent:
    """
    Coordinates all Fynn sub-agents.

    Usage:
        agent = OrchestratorAgent()
        result = await agent.run(
            sender="whatsapp:+6591234567",
            period="March 2026",
            platform_list=["shopee"],
            profile=user_profile,
        )
    """

    async def run(
        self,
        sender: str,
        periods: list[str],
        platform_list: list[str],
        profile: UserProfile,
    ) -> OrchestratorResult:
        period_label = periods[0] if len(periods) == 1 else f"{periods[0]} – {periods[-1]}"
        print(f"\n[Orchestrator] Starting pipeline for {profile.name} — {platform_list} — {period_label}")
        status: dict[str, str] = {}

        import main as _main
        from agents.excel_agent import _build_combined_pnl
        from services.database import load_pnl_by_period, save_pnl

        sensitivity = getattr(profile, "anomaly_sensitivity", "normal")
        currency    = getattr(profile, "currency", "SGD")
        dirty_periods: set[str] = getattr(_main, "_dirty_periods", set())
        all_cost_totals: dict[str, dict[str, float]] = getattr(_main, "_cost_totals", {})
        unknown_costs = all_cost_totals.get("unknown", {})

        def _cost_for_period(p: str) -> dict[str, float]:
            return {**unknown_costs, **all_cost_totals.get(p, {})}

        # ── Steps 1+2: Per-period — cache check then pipeline if needed ────────
        # Each period is processed independently so clean periods can be served
        # from the Supabase cache without re-running any agents.
        period_pnls: list[dict] = []   # one combined P&L per period
        all_platform_pnls: list[dict] = []  # full P&L dicts for Excel detail sheets
        all_journal_entries: list[JournalEntry] = []  # ledger entries across all periods/platforms

        for period in periods:
            is_dirty = period in dirty_periods

            if not is_dirty:
                cached = load_pnl_by_period(sender, period)
                if cached:
                    print(f"  [Orchestrator] Cache hit for {period} — skipping pipeline.")
                    status[f"period_{period}"] = "cached"
                    period_pnls.append(cached)
                    continue

            print(f"\n  [Orchestrator] Processing {period} (dirty={is_dirty})...")

            # Ingest all platforms for this period in parallel
            ingestion_tasks = [IngestionAgent().run(platform, period) for platform in platform_list]
            raw_results = await asyncio.gather(*ingestion_tasks, return_exceptions=True)

            platform_pnls: list[dict] = []
            for platform, result in zip(platform_list, raw_results):
                key = f"{platform}_{period}"
                if isinstance(result, Exception):
                    print(f"    Ingestion failed for {key}: {result}")
                    status[f"ingestion_{key}"] = f"error: {result}"
                    continue
                status[f"ingestion_{key}"] = f"ok ({result.row_count} rows, {result.source})"

                cost_totals_myr = _cost_for_period(period)

                try:
                    recon = ReconciliationAgent().run(result.transactions, platform, period)
                    status[f"reconciliation_{key}"] = "ok"
                except Exception as exc:
                    print(f"    Reconciliation failed for {key}: {exc}")
                    status[f"reconciliation_{key}"] = f"error: {exc}"
                    continue

                try:
                    anomalies = AnomalyAgent().run(result.transactions, recon, sensitivity)
                    status[f"anomalies_{key}"] = f"ok ({len(anomalies)} found)"
                except Exception as exc:
                    anomalies = []
                    status[f"anomalies_{key}"] = f"error: {exc}"

                try:
                    pnl = PnLAgent().run(
                        platform=platform, period=period,
                        reconciliation=recon, anomalies=anomalies,
                        transactions=result.transactions,
                        currency=currency, cost_totals_myr=cost_totals_myr,
                    )
                    platform_pnls.append(pnl)
                    all_platform_pnls.append(pnl)
                    status[f"pnl_{key}"] = "ok"
                except Exception as exc:
                    print(f"    P&L failed for {key}: {exc}")
                    status[f"pnl_{key}"] = f"error: {exc}"
                    continue

                try:
                    entries = LedgerAgent().run(
                        reconciliation=recon, cost_totals_myr=cost_totals_myr,
                        platform=platform, period=period,
                    )
                    all_journal_entries.extend(entries)
                    status[f"ledger_{key}"] = f"ok ({len(entries)} entries)"
                except Exception as exc:
                    print(f"    Ledger failed for {key}: {exc}")
                    status[f"ledger_{key}"] = f"error: {exc}"

            if not platform_pnls:
                print(f"  [Orchestrator] No P&L generated for {period} — skipping.")
                continue

            # Combine platforms within this period and cache the result
            period_combined = _build_combined_pnl(platform_pnls, period_label=period, currency=currency)
            save_pnl(sender, period_combined)
            dirty_periods.discard(period)
            period_pnls.append(period_combined)
            status[f"period_{period}"] = f"ok ({len(platform_pnls)} platform(s))"

        if not period_pnls:
            print("  [Orchestrator] No P&L reports generated — aborting.")
            return OrchestratorResult(pipeline_status=status)

        # ── Build final combined P&L across all periods ───────────────────────
        combined = _build_combined_pnl(period_pnls, period_label=period_label, currency=currency)
        # all_platform_pnls are the full P&L dicts (with revenue/costs/profit) for Excel detail sheets
        pnl_reports = all_platform_pnls

        if all_journal_entries:
            from services.database import save_journal_entries
            save_journal_entries(sender, all_journal_entries)

        # ── Step 3: Excel workbook ─────────────────────────────────────────────
        excel_url = None
        try:
            excel_result = ExcelAgent().run(pnl_reports, combined)
            excel_url    = excel_result.get("url")
            status["excel"] = "ok" if excel_url else "saved_locally"
        except Exception as exc:
            print(f"  [Orchestrator] Excel generation failed: {exc}")
            status["excel"] = f"error: {exc}"

        # ── Step 4: WhatsApp ───────────────────────────────────────────────────
        try:
            ok = WhatsAppAgent().run(
                pnl_reports, profile, to=sender, combined=combined, excel_url=excel_url
            )
            status["whatsapp"] = "ok" if ok else "failed"
        except Exception as exc:
            print(f"  [Orchestrator] WhatsApp failed: {exc}")
            status["whatsapp"] = f"error: {exc}"

        print(f"\n[Orchestrator] Pipeline complete. Status: {status}")

        return OrchestratorResult(
            pnl_reports=pnl_reports,
            pipeline_status=status,
            combined_pnl=combined,
            journal_entries=all_journal_entries,
        )


    def run_sync(
        self,
        sender: str,
        periods: list[str],
        platform_list: list[str],
        profile: UserProfile,
    ) -> OrchestratorResult:
        """Synchronous wrapper — always runs the coroutine in a fresh event loop on a new thread."""
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                self.run(sender, periods, platform_list, profile),
            )
            return future.result(timeout=300)  # 5-minute hard cap
