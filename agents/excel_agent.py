"""
Excel Agent — generates the monthly P&L workbook and uploads it to
Supabase Storage so Twilio can send it as a WhatsApp attachment.

Upload path:  reports/{period}/{filename}
Public URL is returned to the caller (WhatsAppAgent).

Fallback: if Supabase Storage is not configured the bytes are saved
locally to /tmp and the local path is returned instead of a URL.
"""

import os
import re
import tempfile
from datetime import datetime

from services.excel import generate


class ExcelAgent:
    """Generates .xlsx report and uploads to Supabase Storage."""

    def run(self, pnl_reports: list[dict], combined: dict) -> dict:
        """
        Args:
            pnl_reports: Per-platform P&L dicts.
            combined:    Company-level P&L dict.

        Returns:
            {
                "url":      str | None,   # public download URL (None if Supabase unavailable)
                "filename": str,
                "bytes":    bytes,        # always present — caller can use directly if needed
            }
        """
        period   = combined.get("period", "report")
        filename = _safe_filename(f"Fynn_{period}.xlsx")

        print(f"  [Excel] Generating workbook: {filename}")
        xlsx_bytes = generate(pnl_reports, combined)
        print(f"  [Excel] Workbook generated ({len(xlsx_bytes):,} bytes)")

        url = _upload_to_supabase(xlsx_bytes, period, filename)

        if url:
            print(f"  [Excel] Uploaded → {url}")
        else:
            # Save locally as fallback
            path = os.path.join(tempfile.gettempdir(), filename)
            with open(path, "wb") as f:
                f.write(xlsx_bytes)
            print(f"  [Excel] Supabase unavailable — saved locally: {path}")

        return {"url": url, "filename": filename, "bytes": xlsx_bytes}


# ── Supabase Storage helpers ───────────────────────────────────────────────────

_BUCKET = "reports"


def _upload_to_supabase(data: bytes, period: str, filename: str) -> str | None:
    """Upload bytes to Supabase Storage and return the public URL, or None on failure."""
    try:
        from services.database import _get_client
        client = _get_client()
        if client is None:
            return None

        # Sanitise period for use as a folder name
        folder   = re.sub(r"[^\w\-]", "_", period)
        path     = f"{folder}/{filename}"
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

        # Upsert — overwrite if the same month's file already exists
        client.storage.from_(_BUCKET).upload(
            path=path,
            file=data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        url = client.storage.from_(_BUCKET).get_public_url(path)
        return url
    except Exception as exc:
        print(f"  [Excel] Supabase upload failed: {exc}")
        return None


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", name)
