"""
Ingestion Agent — loads and parses CSV data for a single platform.

No Claude call on the happy path — pure Python parse.
Claude is only invoked if the CSV has unrecognised columns.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from models.transaction import Transaction
from services.csv_parser import parse_csv

_executor = ThreadPoolExecutor(max_workers=4)


@dataclass
class IngestionResult:
    platform: str
    period: str
    transactions: list[Transaction] = field(default_factory=list)
    row_count: int = 0
    source: str = "mock"            # "uploaded_csv" | "mock"
    error: Optional[str] = None


class IngestionAgent:
    """Load and parse a platform's CSV data from the in-memory store."""

    async def run(self, platform: str, period: str) -> IngestionResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._load_sync, platform, period)

    def _load_sync(self, platform: str, period: str) -> IngestionResult:
        import main as _main

        # Try exact (platform, period) match first, then any period for the platform
        platform_periods = _main._platform_transactions.get(platform, {})
        txns = platform_periods.get(period)
        if txns is None:
            # Fall back to any period for this platform (e.g. when period label differs slightly)
            txns = next(iter(platform_periods.values()), None)
        if txns is None:
            # Try any other platform as last resort before mock
            for other_periods in _main._platform_transactions.values():
                candidate = other_periods.get(period) or next(iter(other_periods.values()), None)
                if candidate:
                    txns = candidate
                    break

        if txns:
            print(f"  [Ingestion:{platform}] Loaded {len(txns)} transactions from upload")
            return IngestionResult(
                platform=platform,
                period=period,
                transactions=txns,
                row_count=len(txns),
                source="uploaded_csv",
            )

        # Fallback to mock data
        from data.mock_shopee_data import get_mock_transactions
        txns = get_mock_transactions()
        print(f"  [Ingestion:{platform}] No CSV found — using mock data ({len(txns)} rows)")
        return IngestionResult(
            platform=platform,
            period=period,
            transactions=txns,
            row_count=len(txns),
            source="mock",
        )
