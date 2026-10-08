"""Read the TongDaXin (通达信) 自选股 watchlist.

Supports the two storage formats written by TDX clients:
- ``T0002/blocknew/zxg.blk``   — text format: optional ``1999999`` header line,
  then one 7-digit code per line (market digit 0=深圳 / 1=上海 + 6-digit code).
- ``T0002/blocknew/zxg.block`` — binary format: 384-byte header followed by
  314-byte records (6-byte ASCII code + 1-byte market, 0=深圳 / 1=上海).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: 常见通达信安装目录，按顺序探测（可通过 PA_AGENT_TDX_DIR 环境变量覆盖）
_COMMON_TDX_DIRS: tuple[str, ...] = (
    r"D:\app2\tdx",
    r"C:\new_tdx",
    r"D:\new_tdx",
    r"C:\tdx",
    r"D:\tdx",
    r"C:\zdcf",
    r"D:\zdcf",
)

#: .blk 文本文件首行的版本标记
_BLK_HEADER_LINE = "1999999"

#: .block 二进制文件：384 字节文件头，其后每条记录 314 字节
_BLOCK_HEADER_SIZE = 384
_BLOCK_RECORD_SIZE = 314

#: 输出 6 位代码时，可根据前缀唯一推断市场的代码段
#: 上海 56xxxx 为近年启用的新股代码段（如 563080），需与 tdx_source 推断保持一致
_SZ_PREFIXES = ("00", "30", "39")
_SH_PREFIXES = ("60", "56", "51", "50", "68")


def _looks_like_tdx_dir(path: Path) -> bool:
    return (path / "T0002" / "blocknew").is_dir()


def find_tdx_install_dir() -> Path | None:
    """定位包含 T0002/blocknew 的通达信安装目录；找不到返回 None."""
    candidates: list[Path] = []
    env_dir = os.environ.get("PA_AGENT_TDX_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(Path(raw) for raw in _COMMON_TDX_DIRS)
    for cand in candidates:
        try:
            if _looks_like_tdx_dir(cand):
                return cand
        except OSError:
            continue
    return None


def watchlist_file_for(tdx_dir: str | Path | None = None) -> Path | None:
    """返回自选股文件路径（优先文本格式 zxg.blk）。

    显式指定的目录无效（不存在或不含 T0002/blocknew）时回退到自动探测，
    避免配置指向已删除/错误的路径时自选股静默读不到。
    """
    root: Path | None = None
    if tdx_dir:
        candidate = Path(tdx_dir).expanduser()
        if _looks_like_tdx_dir(candidate):
            root = candidate
        else:
            logger.warning(
                "配置的通达信目录无效（未找到 T0002/blocknew）：%s，回退自动探测", candidate
            )
    if root is None:
        root = find_tdx_install_dir()
    if root is None:
        return None
    block_dir = root / "T0002" / "blocknew"
    for name in ("zxg.blk", "zxg.block"):
        path = block_dir / name
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _market_aware_code(market: str, code: str) -> str:
    """输出 6 位裸代码；当前缀推断的市场与文件市场位冲突时带 sh/sz 前缀."""
    if market == "0" and not code.startswith(_SZ_PREFIXES):
        return f"sz{code}"
    if market == "1" and not code.startswith(_SH_PREFIXES):
        return f"sh{code}"
    return code


def _parse_blk_text(path: Path) -> list[tuple[str, str]]:
    """解析文本格式 .blk，返回 (市场位, 6 位代码) 列表."""
    entries: list[tuple[str, str]] = []
    try:
        content = path.read_text(encoding="gbk", errors="ignore")
    except OSError as exc:
        logger.debug("读取自选股文件失败 %s: %s", path, exc)
        return []
    for line in content.splitlines():
        line = line.strip()
        if not line or not line.isdigit() or line == _BLK_HEADER_LINE:
            continue
        if len(line) == 7:
            entries.append((line[0], line[1:]))
        elif len(line) == 6:
            entries.append(("", line))
    return entries


def _parse_block_binary(path: Path) -> list[tuple[str, str]]:
    """解析二进制格式 .block，返回 (市场位, 6 位代码) 列表."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.debug("读取自选股文件失败 %s: %s", path, exc)
        return []
    entries: list[tuple[str, str]] = []
    offset = _BLOCK_HEADER_SIZE
    while offset + 7 <= len(data):
        try:
            code = data[offset:offset + 6].decode("ascii")
        except UnicodeDecodeError:
            code = ""
        market_byte = data[offset + 6]
        if code.isdigit():
            # 市场字节：0=深圳 1=上海；其他值按未知处理
            market = str(market_byte) if market_byte in (0, 1) else ""
            entries.append((market, code))
        offset += _BLOCK_RECORD_SIZE
    return entries


def read_watchlist_file(path: Path) -> list[str]:
    """读取单个自选股文件，返回股票代码列表（保持文件顺序，已去重）."""
    suffix = path.suffix.lower()
    if suffix == ".blk":
        entries = _parse_blk_text(path)
    else:
        entries = _parse_block_binary(path)

    symbols: list[str] = []
    seen: set[str] = set()
    for market, code in entries:
        sym = _market_aware_code(market, code)
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


def read_tdx_watchlist(tdx_dir: str | Path | None = None) -> list[str]:
    """读取通达信自选股代码列表；目录未指定时自动探测。"""
    path = watchlist_file_for(tdx_dir)
    if path is None:
        return []
    symbols = read_watchlist_file(path)
    if symbols:
        logger.info("已读取通达信自选股 %d 只: %s", len(symbols), path)
    else:
        logger.debug("自选股文件为空: %s", path)
    return symbols
