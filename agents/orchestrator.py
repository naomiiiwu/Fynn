"""
Fynn Orchestrator Agent.

Coordinates all sub-agents to run the full bookkeeping pipeline.
Replaces the monolithic BookkeeperAgent.

Pipeline:
  1. Parallel ingestion (one IngestionAgent per platform)
  2. Per-platform: Reconciliation → Anomaly → P&L
  3. SheetsAgent (all platforms in one call)
  4. WhatsAppAgent (combined + per-platform message)
"""

import asyncio
from dataclasses import dataclass, field

from models.user_profile import UserProfile

from .anomaly_agent import AnomalyAgent
from .ingestion_agent import IngestionAgent, IngestionResult
from .pnl_agent import PnLAgent
from .reconciliation_agent import ReconciliationAgent
from .sheets_agent import SheetsAgent
from .whatsapp_agent import WhatsAppAgent


@dataclass
class OrchestratorResult:
    pnl_reports:     list[dict]       = field(default_factory=list)
    pipeline_status: dict[str, str]   = field(default_factory=dict)
    combined_pnl:    dict             = field(default_factory=dict)


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
        period: str,
        platform_list: list[str],
        profile: UserProfile,
    ) -> OrchestratorResult:
        print(f"\n[Orchestrator] Starting pipeline for {profile.name} — {platform_list} — {period}")
        status: dict[str, str] = {}

        # ── Step 1: Parallel ingestion ─────────────────────────────────────────
        ingestion_tasks = [
            IngestionAgent().run(platform, period)
            for platform in platform_list
        ]
        raw_results = await asyncio.gather(*ingestion_tasks, return_exceptions=True)

        ingestion_results: list[IngestionResult] = []
        for platform, result in zip(platform_list, raw_results):
            if isinstance(result, Exception):
                print(f"  [Orchestrator] Ingestion failed for {platform}: {result}")
                status[f"ingestion_{platform}"] = f"error: {result}"
            else:
                ingestion_results.append(result)
                status[f"ingestion_{platform}"] = f"ok ({result.row_count} rows, {result.source})"

        if not ingestion_results:
            print("  [Orchestrator] All ingestion failed — aborting.")
            return OrchestratorResult(pipeline_status=status)

        # ── Step 2: Per-platform processing ───────────────────────────────────
        pnl_reports: list[dict] = []
        sensitivity = getattr(profile, "anomaly_sensitivity", "normal")
        currency    = getattr(profile, "currency", "SGD")

        # Load cost totals for this specific period from in-memory store
        import main as _main
        all_cost_totals: dict[str, dict[str, float]] = getattr(_main, "_cost_totals", {})
        cost_totals_myr: dict[str, float] = all_cost_totals.get(period, {})
        if cost_totals_myr:
            print(f"  [Orchestrator] Cost totals for {period} (MYR): { {k: f'{v:,.2f}' for k, v in cost_totals_myr.items()} }")
        else:
            print(f"  [Orchestrator] No cost files uploaded for {period} — P&L will show platform costs only.")

        for ingest in ingestion_results:
            platform = ingest.platform
            print(f"\n  [Orchestrator] Processing {platform}...")

            try:
                recon = ReconciliationAgent().run(ingest.transactions, platform, period)
                status[f"reconciliation_{platform}"] = "ok"
            except Exception as exc:
                print(f"  [Orchestrator] Reconciliation failed for {platform}: {exc}")
                status[f"reconciliation_{platform}"] = f"error: {exc}"
                continue

            try:
                anomalies = AnomalyAgent().run(ingest.transactions, recon, sensitivity)
                status[f"anomalies_{platform}"] = f"ok ({len(anomalies)} found)"
            except Exception as exc:
                print(f"  [Orchestrator] Anomaly detection failed for {platform}: {exc}")
                anomalies = []
                status[f"anomalies_{platform}"] = f"error: {exc}"

            try:
                pnl = PnLAgent().run(
                    platform=platform,
                    period=period,
                    reconciliation=recon,
                    anomalies=anomalies,
                    transactions=ingest.transactions,
                    currency=currency,
                    cost_totals_myr=cost_totals_myr,
                )
                pnl_reports.append(pnl)
                status[f"pnl_{platform}"] = "ok"
            except Exception as exc:
                print(f"  [Orchestrator] P&L generation failed for {platform}: {exc}")
                status[f"pnl_{platform}"] = f"error: {exc}"

        if not pnl_reports:
            print("  [Orchestrator] No P&L reports generated — aborting.")
            return OrchestratorResult(pipeline_status=status)

        # ── Step 3: Google Sheets ──────────────────────────────────────────────
        try:
            sheets_result = SheetsAgent().run(pnl_reports)
            status["sheets"] = "ok" if sheets_result["success"] else "fallback_json"
        except Exception as exc:
            print(f"  [Orchestrator] Sheets failed: {exc}")
            status["sheets"] = f"error: {exc}"

        # ── Step 4: WhatsApp ───────────────────────────────────────────────────
        try:
            ok = WhatsAppAgent().run(pnl_reports, profile, to=sender)
            status["whatsapp"] = "ok" if ok else "failed"
        except Exception as exc:
            print(f"  [Orchestrator] WhatsApp failed: {exc}")
            status["whatsapp"] = f"error: {exc}"

        print(f"\n[Orchestrator] Pipeline complete. Status: {status}")

        # Build combined P&L for storage
        from agents.sheets_agent import _build_combined_pnl
        combined = _build_combined_pnl(pnl_reports) if len(pnl_reports) > 1 else pnl_reports[0]

        return OrchestratorResult(
            pnl_reports=pnl_reports,
            pipeline_status=status,
            combined_pnl=combined,
        )

    def run_sync(
        self,
        sender: str,
        period: str,
        platform_list: list[str],
        profile: UserProfile,
    ) -> OrchestratorResult:
        """Synchronous wrapper for use in non-async contexts (background threads)."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're already in an async context — run in a new thread's event loop
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        asyncio.run,
                        self.run(sender, period, platform_list, profile),
                    )
                    return future.result()
            return loop.run_until_complete(
                self.run(sender, period, platform_list, profile)
            )
        except RuntimeError:
            return asyncio.run(self.run(sender, period, platform_list, profile))
