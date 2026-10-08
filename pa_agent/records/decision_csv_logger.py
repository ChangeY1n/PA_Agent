"""CSV logger for trading decisions.

Appends trading decisions to a CSV file when analysis produces an opening
position decision (order_type != "不下单").
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pa_agent.records.schema import AnalysisRecord

logger = logging.getLogger(__name__)

# CSV column headers
CSV_HEADERS = [
    "timestamp",
    "symbol",
    "timeframe",
    "order_type",
    "order_direction",
    "entry_price",
    "stop_loss_price",
    "take_profit_price",
    "reasoning",
    "decision_stance",
]


class DecisionCSVLogger:
    """Thread-safe CSV logger for trading decisions."""

    def __init__(self, csv_path: str | Path, enabled: bool = True) -> None:
        """Initialize the CSV logger.

        Parameters
        ----------
        csv_path:
            Path to the CSV file (will be created if it doesn't exist).
        enabled:
            Whether logging is enabled.
        """
        self._csv_path = Path(csv_path)
        self._enabled = enabled

    def log_decision(self, record: AnalysisRecord) -> None:
        """Log a trading decision to CSV if it's not "不下单".

        Parameters
        ----------
        record:
            The full analysis record containing stage2_decision.
        """
        if not self._enabled:
            return

        stage2_decision = record.stage2_decision
        if stage2_decision is None:
            return

        # Extract the inner "decision" dict if present
        decision = stage2_decision.get("decision", stage2_decision)
        order_type = decision.get("order_type", "")

        # Only log if there's an actual order (not "不下单")
        if order_type == "不下单" or not order_type:
            return

        try:
            self._write_decision_row(record, decision)
        except Exception as exc:
            logger.warning("Failed to write decision to CSV: %s", exc, exc_info=True)

    def _write_decision_row(self, record: AnalysisRecord, decision: dict[str, Any]) -> None:
        """Write a single decision row to the CSV file."""
        # Ensure parent directory exists
        self._csv_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if file exists to determine if we need to write headers
        file_exists = self._csv_path.exists()

        # Prepare row data
        row = self._prepare_row(record, decision)

        # Write to CSV with file locking (Windows-compatible approach)
        # Use 'a' mode to append, and handle headers if file is new
        try:
            # For Windows compatibility, we use a simple approach:
            # Open in append mode and write headers only if file is new
            with open(self._csv_path, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)

                # Write headers if file is new or empty
                if not file_exists or os.path.getsize(self._csv_path) == 0:
                    writer.writeheader()

                writer.writerow(row)

            logger.info(
                "Logged decision to CSV: %s %s %s @ %s",
                record.meta.symbol,
                decision.get("order_type"),
                decision.get("order_direction"),
                decision.get("entry_price"),
            )
        except Exception as exc:
            logger.error("Error writing to CSV file %s: %s", self._csv_path, exc)
            raise

    def _prepare_row(self, record: AnalysisRecord, decision: dict[str, Any]) -> dict[str, str]:
        """Prepare a CSV row dict from the record and decision."""
        meta = record.meta

        # Format timestamp
        timestamp = meta.timestamp_local_iso

        # Extract decision fields
        order_type = str(decision.get("order_type", ""))
        order_direction = str(decision.get("order_direction", ""))
        entry_price = decision.get("entry_price")
        stop_loss_price = decision.get("stop_loss_price")
        take_profit_price = decision.get("take_profit_price")
        reasoning = str(decision.get("reasoning", decision.get("brief_reasoning", "")))
        decision_stance = meta.decision_stance

        # Format prices (handle None values)
        entry_str = f"{entry_price:.5g}" if entry_price is not None else ""
        sl_str = f"{stop_loss_price:.5g}" if stop_loss_price is not None else ""
        tp_str = f"{take_profit_price:.5g}" if take_profit_price is not None else ""

        # Clean reasoning text (remove excessive whitespace)
        reasoning = " ".join(reasoning.split())

        return {
            "timestamp": timestamp,
            "symbol": meta.symbol,
            "timeframe": meta.timeframe,
            "order_type": order_type,
            "order_direction": order_direction,
            "entry_price": entry_str,
            "stop_loss_price": sl_str,
            "take_profit_price": tp_str,
            "reasoning": reasoning,
            "decision_stance": decision_stance,
        }

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable CSV logging."""
        self._enabled = enabled

    def set_csv_path(self, csv_path: str | Path) -> None:
        """Update the CSV file path."""
        self._csv_path = Path(csv_path)
