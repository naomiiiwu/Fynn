"""
Multi-currency conversion service.

Uses Open Exchange Rates API when available, falls back to
hardcoded rates if the API key is missing or the request fails.
"""

import os
from typing import Optional

import httpx

# Fallback exchange rates (relative to USD as base)
# Source: approximate rates as of early 2026
FALLBACK_RATES_USD_BASE = {
    "USD": 1.0,
    "MYR": 4.45,
    "SGD": 1.35,
    "THB": 34.80,
    "CNY": 7.28,
}

# Pre-computed direct cross rates for convenience (updated periodically)
FALLBACK_CROSS_RATES = {
    ("MYR", "SGD"): 0.304,   # 1 MYR = 0.304 SGD
    ("SGD", "MYR"): 3.289,
    ("MYR", "USD"): 0.225,
    ("USD", "MYR"): 4.45,
    ("MYR", "THB"): 7.82,
    ("THB", "MYR"): 0.128,
    ("MYR", "CNY"): 1.636,
    ("CNY", "MYR"): 0.611,
    ("SGD", "USD"): 0.741,
    ("USD", "SGD"): 1.35,
}


class CurrencyConverter:
    """Handles multi-currency conversion using Open Exchange Rates or fallback rates."""

    def __init__(self) -> None:
        """Initialise the converter; load API key from environment if available."""
        self.app_id = os.getenv("OPENEXCHANGERATES_APP_ID", "")
        self.api_base = "https://openexchangerates.org/api"
        self._live_rates: Optional[dict] = None

    def _fetch_live_rates(self) -> Optional[dict]:
        """
        Fetch latest exchange rates from Open Exchange Rates API.

        Returns:
            dict of currency -> rate (USD base) or None on failure.
        """
        if not self.app_id:
            print("  [Currency] No OPENEXCHANGERATES_APP_ID found — using fallback rates.")
            return None

        try:
            url = f"{self.api_base}/latest.json?app_id={self.app_id}"
            response = httpx.get(url, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            rates = data.get("rates", {})
            print(f"  [Currency] Live rates fetched successfully (base: USD, {len(rates)} currencies).")
            return rates
        except Exception as exc:
            print(f"  [Currency] API request failed ({exc}) — using fallback rates.")
            return None

    def _get_rates(self) -> dict:
        """Return live rates if available, otherwise fallback rates."""
        if self._live_rates is None:
            self._live_rates = self._fetch_live_rates() or FALLBACK_RATES_USD_BASE
        return self._live_rates

    def convert(self, amount: float, from_currency: str, to_currency: str) -> dict:
        """
        Convert an amount from one currency to another.

        Args:
            amount:        The amount to convert.
            from_currency: ISO 4217 source currency code (e.g. "MYR").
            to_currency:   ISO 4217 target currency code (e.g. "SGD").

        Returns:
            dict with keys:
              - converted_amount (float)
              - exchange_rate    (float)
              - from_currency    (str)
              - to_currency      (str)
              - source           ("live" | "fallback")
        """
        if from_currency == to_currency:
            return {
                "converted_amount": round(amount, 2),
                "exchange_rate": 1.0,
                "from_currency": from_currency,
                "to_currency": to_currency,
                "source": "same_currency",
            }

        # Try direct cross-rate lookup first (accurate, no rounding)
        cross_key = (from_currency, to_currency)
        if cross_key in FALLBACK_CROSS_RATES and not self.app_id:
            rate = FALLBACK_CROSS_RATES[cross_key]
            source = "fallback_direct"
        else:
            rates = self._get_rates()
            from_rate = rates.get(from_currency)
            to_rate = rates.get(to_currency)

            if from_rate is None or to_rate is None:
                raise ValueError(f"Unsupported currency pair: {from_currency} -> {to_currency}")

            # Convert via USD as intermediate base
            rate = to_rate / from_rate
            source = "live" if self._live_rates and self._live_rates != FALLBACK_RATES_USD_BASE else "fallback"

        converted = round(amount * rate, 2)
        print(f"  [Currency] {amount:.2f} {from_currency} → {converted:.2f} {to_currency} (rate: {rate:.4f}, source: {source})")

        return {
            "converted_amount": converted,
            "exchange_rate": round(rate, 6),
            "from_currency": from_currency,
            "to_currency": to_currency,
            "source": source,
        }

    def myr_to_sgd(self, amount_myr: float) -> dict:
        """
        Convenience method: convert MYR to SGD.

        Args:
            amount_myr: Amount in Malaysian Ringgit.

        Returns:
            Conversion result dict (see convert()).
        """
        return self.convert(amount_myr, "MYR", "SGD")
