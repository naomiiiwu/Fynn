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
        periods: list[str],
        platform_list: list[str],
        profile: UserProfile,
    ) -> OrchestratorResult:
        period_label = periods[0] if len(periods) == 1 else f"{periods[0]} – {periods[-1]}"
        print(f"\n[Orchestrator] Starting pipeline for {profile.name} — {platform_list} — {period_label}")
        status: dict[str, str] = {}

        # ── Step 1: Parallel ingestion — one task per (platform × period) ─────
        pairs = [(platform, period) for platform in platform_list for period in periods]
        ingestion_tasks = [IngestionAgent().run(platform, period) for platform, period in pairs]
        raw_results = await asyncio.gather(*ingestion_tasks, return_exceptions=True)

        ingestion_results: list[IngestionResult] = []
        for (platform, period), result in zip(pairs, raw_results):
            key = f"{platform}_{period}"
            if isinstance(result, Exception):
                print(f"  [Orchestrator] Ingestion failed for {key}: {result}")
                status[f"ingestion_{key}"] = f"error: {result}"
            else:
                ingestion_results.append(result)
                status[f"ingestion_{key}"] = f"ok ({result.row_count} rows, {result.source})"

        if not ingestion_results:
            print("  [Orchestrator] All ingestion failed — aborting.")
            return OrchestratorResult(pipeline_status=status)

        # ── Step 2: Per-(platform × period) processing ────────────────────────
        pnl_reports: list[dict] = []
        sensitivity = getattr(profile, "anomaly_sensitivity", "normal")
        currency    = getattr(profile, "currency", "SGD")

        import main as _main
        all_cost_totals: dict[str, dict[str, float]] = getattr(_main, "_cost_totals", {})
        unknown_costs = all_cost_totals.get("unknown", {})

        def _cost_for_period(period: str) -> dict[str, float]:
            """Merge 'unknown' costs with period-specific costs (period takes precedence)."""
            return {**unknown_costs, **all_cost_totals.get(period, {})}

        # Business costs are company-level: applied per-period to each period's P&L
        # so the combined total correctly reflects per-period expenses.
        for ingest in ingestion_results:
            platform = ingest.platform
            period   = ingest.period
            key      = f"{platform}_{period}"
            cost_totals_myr = _cost_for_period(period)
            print(f"\n  [Orchestrator] Processing {key}...")
            if cost_totals_myr:
                print(f"    Business costs: { {k: f'MYR {v:,.2f}' for k, v in cost_totals_myr.items()} }")
            else:
                print(f"    No cost files for {period} — platform costs only.")

            try:
                recon = ReconciliationAgent().run(ingest.transactions, platform, period)
                status[f"reconciliation_{key}"] = "ok"
            except Exception as exc:
                print(f"  [Orchestrator] Reconciliation failed for {key}: {exc}")
                status[f"reconciliation_{key}"] = f"error: {exc}"
                continue

            try:
                anomalies = AnomalyAgent().run(ingest.transactions, recon, sensitivity)
                status[f"anomalies_{key}"] = f"ok ({len(anomalies)} found)"
            except Exception as exc:
                print(f"  [Orchestrator] Anomaly detection failed for {key}: {exc}")
                anomalies = []
                status[f"anomalies_{key}"] = f"error: {exc}"

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
                status[f"pnl_{key}"] = "ok"
            except Exception as exc:
                print(f"  [Orchestrator] P&L generation failed for {key}: {exc}")
                status[f"pnl_{key}"] = f"error: {exc}"

        if not pnl_reports:
            print("  [Orchestrator] No P&L reports generated — aborting.")
            return OrchestratorResult(pipeline_status=status)

        # ── Build combined P&L across all periods and platforms ───────────────
        from agents.excel_agent import _build_combined_pnl

        combined = _build_combined_pnl(pnl_reports, period_label=period_label, currency=currency)

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
