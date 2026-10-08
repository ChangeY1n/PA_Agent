"""Smoke test for DecisionExcelLogger.

Run as a script to verify end-to-end behaviour: writes a real .xlsx with
two decisions, then re-opens and asserts the row count and field values.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make `pa_agent` importable when running from the repo root
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from openpyxl import load_workbook  # noqa: E402

from pa_agent.records.decision_excel_logger import (  # noqa: E402
    EXCEL_HEADERS,
    SHEET_NAME,
    DecisionExcelLogger,
)
from pa_agent.records.schema import AnalysisRecord, RecordMeta  # noqa: E402


def _make_record(*, order_type: str, direction: str, entry: float) -> AnalysisRecord:
    meta = RecordMeta(
        timestamp_local_iso="2026-06-23T10:00:00.000",
        timestamp_local_ms=1750670400000,
        symbol="XAUUSDm",
        timeframe="15m",
        bar_count=100,
        ai_provider={"model": "claude-sonnet-4-6"},
        decision_stance="balanced",
    )
    return AnalysisRecord(
        meta=meta,
        kline_data=[],
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis=None,
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={
            "decision": {
                "order_type": order_type,
                "order_direction": direction,
                "entry_price": entry,
                "stop_loss_price": entry - 5.0,
                "take_profit_price": entry + 10.0,
                "reasoning": "测试决策 — 突破关键阻力位, EMA 支撑强劲, 风险/收益比 1:2",
            }
        },
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        xlsx = Path(tmp) / "trading_decisions.xlsx"
        logger = DecisionExcelLogger(xlsx_path=xlsx, enabled=True)

        # 1) First call creates the file with header
        logger.log_decision(_make_record(order_type="限价单", direction="做多", entry=5234.5))
        assert xlsx.exists(), "xlsx file was not created"
        wb = load_workbook(xlsx)
        assert SHEET_NAME in wb.sheetnames, f"missing sheet {SHEET_NAME}"
        ws = wb[SHEET_NAME]
        assert ws.max_row == 2, f"expected 1 header + 1 row, got {ws.max_row}"
        header_row = [c.value for c in ws[1]]
        assert header_row == EXCEL_HEADERS, f"unexpected header: {header_row}"
        data_row = [c.value for c in ws[2]]
        assert data_row[1] == "XAUUSDm"
        assert data_row[3] == "限价单"
        assert data_row[4] == "做多"
        assert abs(float(data_row[5]) - 5234.5) < 1e-6
        assert "突破关键阻力位" in str(data_row[9])
        wb.close()
        print("[ok] first decision written with header")

        # 2) Second call appends, no duplicate header
        logger.log_decision(_make_record(order_type="市价单", direction="做空", entry=1900.25))
        wb = load_workbook(xlsx)
        ws = wb[SHEET_NAME]
        assert ws.max_row == 3, f"expected 1 header + 2 rows, got {ws.max_row}"
        second = [c.value for c in ws[3]]
        assert second[3] == "市价单"
        assert second[4] == "做空"
        assert abs(float(second[5]) - 1900.25) < 1e-6
        wb.close()
        print("[ok] second decision appended")

        # 3) "不下单" decisions must NOT be written
        before_max_row = load_workbook(xlsx)[SHEET_NAME].max_row
        logger.log_decision(_make_record(order_type="不下单", direction="观望", entry=0.0))
        after_max_row = load_workbook(xlsx)[SHEET_NAME].max_row
        assert after_max_row == before_max_row, (
            f"row count changed after 不下单: {before_max_row} -> {after_max_row}"
        )
        print("[ok] '不下单' decisions are skipped")

        # 4) Disabled logger is a no-op
        logger.set_enabled(False)
        logger.log_decision(_make_record(order_type="限价单", direction="做多", entry=1000.0))
        still = load_workbook(xlsx)[SHEET_NAME].max_row
        assert still == after_max_row, "disabled logger wrote a row"
        logger.set_enabled(True)
        print("[ok] set_enabled(False) suppresses writes")

        # 5) Concurrent writes do not corrupt the workbook
        from concurrent.futures import ThreadPoolExecutor
        logger.set_xlsx_path(xlsx)
        def _worker(i: int) -> None:
            logger.log_decision(
                _make_record(
                    order_type="限价单",
                    direction="做多" if i % 2 == 0 else "做空",
                    entry=1000.0 + i,
                )
            )
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i in range(20):
                pool.submit(_worker, i)
        final_max = load_workbook(xlsx)[SHEET_NAME].max_row
        # header (1) + initial 2 + 20 concurrent = 23
        assert final_max == 23, f"expected 23 rows after concurrency, got {final_max}"
        print(f"[ok] 20 concurrent writes appended (max_row={final_max})")

    print("\nAll DecisionExcelLogger smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
