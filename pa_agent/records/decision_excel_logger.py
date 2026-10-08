"""Excel logger for trading decisions.

Appends trading decisions to a single-sheet .xlsx workbook when analysis
produces an opening position decision (order_type != "不下单").

Each row is appended under a fixed header. The workbook is opened
read-append-write on every call, so concurrent processes racing on the
same file are tolerated via a short retry loop. Within a single process
a threading.Lock serialises writes.

Sheet name: ``decisions`` (always the first sheet).
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openpyxl import Workbook, load_workbook

if TYPE_CHECKING:
    from pa_agent.records.schema import AnalysisRecord

logger = logging.getLogger(__name__)

# Sheet name used inside the workbook
SHEET_NAME = "decisions"

# Excel column headers (Chinese + English labels in the header text)
EXCEL_HEADERS = [
    "时间戳 (timestamp)",
    "品种 (symbol)",
    "周期 (timeframe)",
    "订单类型 (order_type)",
    "方向 (direction)",
    "开仓价格 (entry_price)",
    "止损价 (stop_loss)",
    "止盈价 (take_profit)",
    "交易倾向 (decision_stance)",
    "决策理由 (reasoning)",
]

# Column widths in Excel characters (visually readable in Excel)
COLUMN_WIDTHS = {
    "时间戳 (timestamp)": 24,
    "品种 (symbol)": 14,
    "周期 (timeframe)": 12,
    "订单类型 (order_type)": 14,
    "方向 (direction)": 10,
    "开仓价格 (entry_price)": 16,
    "止损价 (stop_loss)": 16,
    "止盈价 (take_profit)": 16,
    "交易倾向 (decision_stance)": 18,
    "决策理由 (reasoning)": 80,
}

# Number of retries when the file is locked by Excel/another process
_OPEN_RETRIES = 5
_OPEN_RETRY_DELAY_S = 0.2


class DecisionExcelLogger:
    """Thread-safe Excel logger for trading decisions.

    Writes one row per ``log_decision`` call to a single-sheet .xlsx
    workbook. If the file is opened in Excel by the user, writes are
    retried a few times before giving up.

    Parameters
    ----------
    xlsx_path:
        Path to the .xlsx file. Created (with header) if it does not exist.
    enabled:
        If False, ``log_decision`` is a no-op.
    """

    def __init__(self, xlsx_path: str | Path, enabled: bool = True) -> None:
        self._xlsx_path = Path(xlsx_path)
        self._enabled = enabled
        self._lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────

    def log_decision(self, record: "AnalysisRecord") -> None:
        """Log a trading decision to the Excel workbook if it's not "不下单".

        Parameters
        ----------
        record:
            The full analysis record containing ``stage2_decision``.
        """
        if not self._enabled:
            return

        stage2_decision = record.stage2_decision
        if stage2_decision is None:
            return

        # Accept either a top-level dict or one wrapped under "decision"
        decision = stage2_decision.get("decision", stage2_decision) or {}
        order_type = decision.get("order_type", "")

        if order_type == "不下单" or not order_type:
            return

        try:
            self._write_decision_row(record, decision)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write decision to Excel: %s", exc, exc_info=True)

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable Excel logging."""
        self._enabled = enabled

    def set_xlsx_path(self, xlsx_path: str | Path) -> None:
        """Update the Excel file path (takes effect on the next write)."""
        self._xlsx_path = Path(xlsx_path)

    # ── Internals ──────────────────────────────────────────────────────────

    def _write_decision_row(
        self, record: "AnalysisRecord", decision: dict[str, Any]
    ) -> None:
        """Append a single decision row to the workbook under a process lock."""
        row = self._prepare_row(record, decision)

        with self._lock:
            self._xlsx_path.parent.mkdir(parents=True, exist_ok=True)
            wb = self._open_or_create_workbook()
            try:
                ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active
                # Defensive: if the active sheet is unnamed, ensure the named
                # sheet exists; this can happen if a user manually removed it.
                if ws.title != SHEET_NAME:
                    if SHEET_NAME in wb.sheetnames:
                        ws = wb[SHEET_NAME]
                    else:
                        ws = wb.create_sheet(SHEET_NAME, 0)
                        ws.append(EXCEL_HEADERS)
                        self._apply_column_widths(ws)

                # First-run header (file existed but was empty)
                if ws.max_row == 0:
                    ws.append(EXCEL_HEADERS)
                    self._apply_column_widths(ws)

                ws.append([row[h] for h in EXCEL_HEADERS])

                # Save with retry — Excel on Windows holds an exclusive lock
                self._save_with_retry(wb)
            finally:
                # openpyxl keeps file handles internally only while saving;
                # explicit close() releases resources on all platforms.
                try:
                    wb.close()
                except Exception:  # noqa: BLE001
                    pass

        logger.info(
            "Logged decision to Excel: %s %s %s @ %s",
            record.meta.symbol,
            decision.get("order_type"),
            decision.get("order_direction"),
            decision.get("entry_price"),
        )

    def _open_or_create_workbook(self) -> Workbook:
        """Open the existing workbook (with retry) or create a new one."""
        if not self._xlsx_path.exists() or self._xlsx_path.stat().st_size == 0:
            wb = Workbook()
            ws = wb.active
            ws.title = SHEET_NAME
            ws.append(EXCEL_HEADERS)
            self._apply_column_widths(ws)
            return wb

        last_exc: Exception | None = None
        for attempt in range(_OPEN_RETRIES):
            try:
                return load_workbook(self._xlsx_path)
            except (PermissionError, OSError) as exc:
                last_exc = exc
                logger.debug(
                    "Excel file busy (attempt %d/%d): %s",
                    attempt + 1,
                    _OPEN_RETRIES,
                    exc,
                )
                time.sleep(_OPEN_RETRY_DELAY_S * (attempt + 1))
        # Fall through — propagate the last error to caller
        raise PermissionError(
            f"无法打开 Excel 文件 {self._xlsx_path}（可能正被 Excel 占用）: {last_exc}"
        )

    def _save_with_retry(self, wb: Workbook) -> None:
        """Save the workbook, retrying on PermissionError (file in use)."""
        last_exc: Exception | None = None
        for attempt in range(_OPEN_RETRIES):
            try:
                wb.save(self._xlsx_path)
                return
            except (PermissionError, OSError) as exc:
                last_exc = exc
                logger.debug(
                    "Excel save busy (attempt %d/%d): %s",
                    attempt + 1,
                    _OPEN_RETRIES,
                    exc,
                )
                time.sleep(_OPEN_RETRY_DELAY_S * (attempt + 1))
        raise PermissionError(
            f"无法保存 Excel 文件 {self._xlsx_path}（可能正被 Excel 占用）: {last_exc}"
        )

    @staticmethod
    def _apply_column_widths(ws: Any) -> None:
        """Apply sensible column widths (best-effort, ignore errors)."""
        try:
            for col_idx, header in enumerate(EXCEL_HEADERS, start=1):
                ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = (
                    COLUMN_WIDTHS.get(header, 18)
                )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _prepare_row(
        record: "AnalysisRecord", decision: dict[str, Any]
    ) -> dict[str, Any]:
        """Build a row dict keyed by Excel header."""
        meta = record.meta

        def _fmt_price(value: Any) -> Any:
            if value is None or value == "":
                return ""
            try:
                return float(value)
            except (TypeError, ValueError):
                return str(value)

        reasoning = str(decision.get("reasoning", decision.get("brief_reasoning", "")) or "")
        reasoning = " ".join(reasoning.split())

        return {
            "时间戳 (timestamp)": meta.timestamp_local_iso,
            "品种 (symbol)": meta.symbol,
            "周期 (timeframe)": meta.timeframe,
            "订单类型 (order_type)": str(decision.get("order_type", "")),
            "方向 (direction)": str(decision.get("order_direction", "")),
            "开仓价格 (entry_price)": _fmt_price(decision.get("entry_price")),
            "止损价 (stop_loss)": _fmt_price(decision.get("stop_loss_price")),
            "止盈价 (take_profit)": _fmt_price(decision.get("take_profit_price")),
            "交易倾向 (decision_stance)": meta.decision_stance,
            "决策理由 (reasoning)": reasoning,
        }
