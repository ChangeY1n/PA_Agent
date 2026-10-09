"""Helpers for locating prior analysis records for incremental runs."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pa_agent.config.paths import RECORDS_PENDING_DIR
from pa_agent.data.datetime_ts import format_epoch_for_display, ts_open_to_ms
from pa_agent.data.base import KlineFrame
from pa_agent.records.schema import AnalysisRecord

_TS_EPS_MS = 1.0  # milliseconds tolerance for bar open time matching


@dataclass(frozen=True)
class IncrementalBarDelta:
    """How many closed bars appeared since a previous record."""

    new_count: int
    anchor_ts_open: float
    new_bar_ts_opens: tuple[float, ...]


def format_bar_ts(ts_open: float) -> str:
    """Format bar open time for logs/UI (server-time epoch, no local TZ shift)."""
    return format_epoch_for_display(ts_open, short=False)


def list_record_paths(directory: Path | None = None) -> list[Path]:
    """Return saved analysis record paths, newest modified first."""
    root = directory or RECORDS_PENDING_DIR
    if not root.is_dir():
        return []
    paths = [p for p in root.glob("*.json") if p.is_file()]
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths


def load_record(path: Path) -> AnalysisRecord | None:
    """Load one AnalysisRecord, returning None for unreadable legacy files."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AnalysisRecord.model_validate(raw)
    except Exception:
        return None


def _successful_record_or_none(
    path: Path, symbol: str, timeframe: str
) -> AnalysisRecord | None:
    """Load *path* and return it only if it is a full successful match."""
    record = load_record(path)
    if record is None:
        return None
    if record.meta.symbol != symbol or record.meta.timeframe != timeframe:
        return None
    if record.exception is not None:
        return None
    if not record.stage1_diagnosis or not record.stage2_decision:
        return None
    if not record.kline_data:
        return None
    return record


def find_latest_successful_record_and_path(
    *,
    symbol: str,
    timeframe: str,
    directory: Path | None = None,
) -> tuple[Path, AnalysisRecord] | None:
    """Return (path, record) of the newest full successful record.

    快路径：记录文件名内嵌 ``_{symbol}_{timeframe}.json`` 后缀（见
    PendingWriter 的命名），先用后缀过滤、只解析少量候选；无命中再回退
    全量扫描以兼容手工改名/遗留文件名。短代码后缀可能误报（如 "001"
    vs "6001"），但 meta 校验保证正确性——后缀只是性能提示。
    """
    paths = list_record_paths(directory)
    suffix = f"_{symbol}_{timeframe}.json"
    for path in paths:
        if not path.name.endswith(suffix):
            continue
        record = _successful_record_or_none(path, symbol, timeframe)
        if record is not None:
            return path, record
    # 回退：解析所有不带该后缀的文件（遗留文件名）
    for path in paths:
        if path.name.endswith(suffix):
            continue
        record = _successful_record_or_none(path, symbol, timeframe)
        if record is not None:
            return path, record
    return None


def find_latest_successful_record(
    *,
    symbol: str,
    timeframe: str,
    directory: Path | None = None,
) -> AnalysisRecord | None:
    """Find the newest full successful record for a symbol/timeframe."""
    hit = find_latest_successful_record_and_path(
        symbol=symbol, timeframe=timeframe, directory=directory
    )
    return hit[1] if hit is not None else None


def compute_incremental_bar_delta(
    frame: KlineFrame,
    previous_record: AnalysisRecord,
) -> IncrementalBarDelta | None:
    """Return bars newer than the previous record's latest closed bar.

    ``frame.bars`` and ``previous_record.kline_data`` are newest-first. The anchor
    is ``kline_data[0]`` (K1 at the time of the previous analysis). New bars are
    those with ``ts_open`` strictly greater than the anchor — not merely bars
    appearing before the anchor index in the current window.
    """
    if not previous_record.kline_data:
        return None

    anchor_raw = previous_record.kline_data[0]["ts_open"]
    anchor = ts_open_to_ms(anchor_raw)

    anchor_seen = False
    new_ts: list[float] = []
    for bar in frame.bars:
        ts = ts_open_to_ms(bar.ts_open)
        if abs(ts - anchor) <= _TS_EPS_MS:
            anchor_seen = True
            continue
        if ts > anchor + _TS_EPS_MS:
            new_ts.append(bar.ts_open)

    if not anchor_seen:
        return None

    return IncrementalBarDelta(
        new_count=len(new_ts),
        anchor_ts_open=float(anchor_raw),
        new_bar_ts_opens=tuple(new_ts),
    )


def count_new_bars_since_record(
    frame: KlineFrame,
    previous_record: AnalysisRecord,
) -> int | None:
    """Backward-compatible wrapper returning only the new bar count."""
    delta = compute_incremental_bar_delta(frame, previous_record)
    if delta is None:
        return None
    return delta.new_count
