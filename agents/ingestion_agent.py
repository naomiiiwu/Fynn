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
from services.csv_parser import parse_shopee_csv

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

        # Try exact match first, then any available platform
        txns = _main._platform_transactions.get(platform)
        if txns is None:
            txns = next(iter(_main._platform_transactions.values()), None)

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
