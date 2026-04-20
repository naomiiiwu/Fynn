"""
Fynn Orchestrator Agent.

Coordinates all sub-agents to run the full bookkeeping pipeline.
Replaces the monolithic BookkeeperAgent.

Pipeline:
  1. Parallel ingestion (one IngestionAgent per platform)
  2. Per-platform: Reconciliation → Anomaly → P&L
  3. ExcelAgent — generate .xlsx, upload to Supabase Storage
  4. WhatsAppAgent — send summary + Excel attachment
"""

import asyncio
from dataclasses import dataclass, field

from models.user_profile import UserProfile

from .anomaly_agent import AnomalyAgent
from .excel_agent import ExcelAgent
from .ingestion_agent import IngestionAgent, IngestionResult
from .pnl_agent import PnLAgent
from .reconciliation_agent import ReconciliationAgent
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

        # Load business cost totals — merge 'unknown' + period-specific
        # (unknown = uploaded before period detection; period-specific takes precedence for same key)
        import main as _main
        all_cost_totals: dict[str, dict[str, float]] = getattr(_main, "_cost_totals", {})
        cost_totals_myr: dict[str, float] = {
            **all_cost_totals.get("unknown", {}),
            **all_cost_totals.get(period, {}),
        }
        if cost_totals_myr:
            print(f"  [Orchestrator] Business costs for {period} (MYR): { {k: f'MYR {v:,.2f}' for k, v in cost_totals_myr.items()} }")
        else:
            print(f"  [Orchestrator] No cost files for {period} — P&L will show platform costs only.")

        # Business costs (COGS, payroll, ads, etc.) are company-level, not per-platform.
        # Individual platform P&Ls show only their own platform fees/shipping/vouchers.
        # Business costs are applied once to the combined P&L at the end.
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
                # No business costs here — applied to combined P&L only
                pnl = PnLAgent().run(
                    platform=platform,
                    period=period,
                    reconciliation=recon,
                    anomalies=anomalies,
                    transactions=ingest.transactions,
                    currency=currency,
                    cost_totals_myr={},
                )
                pnl_reports.append(pnl)
                status[f"pnl_{platform}"] = "ok"
            except Exception as exc:
                print(f"  [Orchestrator] P&L generation failed for {platform}: {exc}")
                status[f"pnl_{platform}"] = f"error: {exc}"

        if not pnl_reports:
            print("  [Orchestrator] No P&L reports generated — aborting.")
            return OrchestratorResult(pipeline_status=status)

        # ── Build combined P&L (with business costs applied once) ─────────────
        from agents.sheets_agent import _build_combined_pnl
        from services.currency import CurrencyConverter

        if len(pnl_reports) > 1:
            combined = _build_combined_pnl(pnl_reports, cost_totals_myr, currency)
        else:
            # Single platform — re-run PnL with business costs included
            combined = PnLAgent().run(
                platform=pnl_reports[0]["platform"],
                period=period,
                reconciliation=ReconciliationAgent().run(
                    ingestion_results[0].transactions, pnl_reports[0]["platform"], period
                ),
                anomalies=[a for p in pnl_reports for a in p.get("anomalies", [])],
                transactions=ingestion_results[0].transactions,
                currency=currency,
                cost_totals_myr=cost_totals_myr,
            )

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
        )


    def run_sync(
        self,
        sender: str,
        period: str,
        platform_list: list[str],
        profile: UserProfile,
    ) -> OrchestratorResult:
        """Synchronous wrapper — always runs the coroutine in a fresh event loop on a new thread."""
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                self.run(sender, period, platform_list, profile),
            )
            return future.result(timeout=300)  # 5-minute hard cap
