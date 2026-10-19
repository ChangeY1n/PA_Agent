"""Helpers for locating prior analysis records for incremental runs."""
from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from pa_agent.config.paths import RECORDS_PENDING_DIR
from pa_agent.data.datetime_ts import format_epoch_for_display, ts_open_to_ms
from pa_agent.data.base import KlineFrame
from pa_agent.records.schema import AnalysisRecord

_TS_EPS_MS = 1.0  # milliseconds tolerance for bar open time matching

#: PendingWriter 命名：{YYYY-MM-DD_HH-mm-ss}_{symbol}_{timeframe}.json
_RECORD_NAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}"
    r"_(?P<symbol>[A-Za-z0-9]+)"
    r"_(?P<timeframe>[0-9]+[a-z]+)"
    r"\.json$"
)

# ── 查找缓存 ────────────────────────────────────────────────────────────────
# records/pending 可能积累数百个文件（实测 533 个 / 221MB）。全量解析在
# GUI 线程上单次可达 ~1s，是品种切换卡顿的主因之一，因此：
# 1) list_record_paths 结果按目录 mtime 缓存（新建/删除记录会改变目录 mtime；
#    记录文件按 PendingWriter 命名只写一次，无原地重写）。
# 2) 单条记录解析结果按 (path, mtime, size) 做 LRU 缓存。
# 锁只保护缓存结构；解析在锁外进行（幂等，重复解析无害）。
_paths_cache_lock = threading.Lock()
_paths_cache: dict[Path, tuple[float, list[Path]]] = {}

_record_cache_lock = threading.Lock()
_record_cache: OrderedDict[tuple[Path, float, int], AnalysisRecord | None] = OrderedDict()
_RECORD_CACHE_MAX = 32


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
    """Return saved analysis record paths, newest modified first.

    结果按目录 mtime 缓存：目录内容变化（新增/删除记录）自动失效。
    """
    root = directory or RECORDS_PENDING_DIR
    try:
        dir_mtime = root.stat().st_mtime
    except OSError:
        return []
    with _paths_cache_lock:
        cached = _paths_cache.get(root)
        if cached is not None and cached[0] == dir_mtime:
            return cached[1]
    if not root.is_dir():
        return []
    paths = [p for p in root.glob("*.json") if p.is_file()]
    # mtime 相同（同一时间刻度内连写多个文件）时按文件名倒序兜底：
    # PendingWriter 命名以记录时间开头，名字倒序 ≈ 新记录在前，保证
    # 排序确定且与"最新优先"语义一致。
    paths.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    with _paths_cache_lock:
        _paths_cache[root] = (dir_mtime, paths)
    return paths


def load_record(path: Path) -> AnalysisRecord | None:
    """Load one AnalysisRecord, returning None for unreadable legacy files."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AnalysisRecord.model_validate(raw)
    except Exception:
        return None


def _cached_load_record(path: Path) -> AnalysisRecord | None:
    """load_record 的 (mtime, size) LRU 缓存版本."""
    try:
        stat = path.stat()
        key = (path, stat.st_mtime, stat.st_size)
    except OSError:
        return load_record(path)
    with _record_cache_lock:
        if key in _record_cache:
            _record_cache.move_to_end(key)
            return _record_cache[key]
    record = load_record(path)
    with _record_cache_lock:
        _record_cache[key] = record
        while len(_record_cache) > _RECORD_CACHE_MAX:
            _record_cache.popitem(last=False)
    return record


def _successful_record_or_none(
    path: Path, symbol: str, timeframe: str
) -> AnalysisRecord | None:
    """Load *path* and return it only if it is a full successful match."""
    record = _cached_load_record(path)
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

    快路径：记录文件名内嵌 ``_{symbol}_{timeframe}``（见 PendingWriter 的
    命名），只解析名字精确匹配的候选（regex 提取，无 endswith 短代码误报）；
    兜底：仅解析不符合写入命名的遗留/手工改名文件。两条路径的解析结果均
    走 (mtime, size) 缓存，重复查找不再重复读盘。
    """
    paths = list_record_paths(directory)
    # 快路径：文件名编码的 (symbol, timeframe) 与请求完全一致
    for path in paths:
        m = _RECORD_NAME_RE.match(path.name)
        if m is None:
            continue
        if m.group("symbol") != symbol or m.group("timeframe") != timeframe:
            continue
        record = _successful_record_or_none(path, symbol, timeframe)
        if record is not None:
            return path, record
    # 兜底：仅解析不符合写入命名的文件（legacy 名，数量通常为 0）
    for path in paths:
        if _RECORD_NAME_RE.match(path.name) is not None:
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
