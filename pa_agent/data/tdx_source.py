"""TongDaXin (通达信) data source using eltdx library.

Uses eltdx to connect to TongDaXin servers for A-share market data.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar, normalize_kline_bar

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")

# 通达信周期映射到 eltdx KlinePeriod
_TDX_PERIOD_MAP = {
    "1m": "MINUTE_1",
    "5m": "MINUTE_5",
    "15m": "MINUTE_15",
    "30m": "MINUTE_30",
    "1h": "MINUTE_60",
    "1d": "DAY",
}

_SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "1d")

# 预设股票代码
_PRESET_SYMBOLS: tuple[str, ...] = (
    "000001",  # 平安银行
    "600519",  # 贵州茅台
    "000300",  # 沪深300
    "399006",  # 创业板指
    "600036",  # 招商银行
)


def _normalize_code_to_eltdx(symbol: str) -> str:
    """将股票代码转换为 eltdx 格式 (sz000001 或 sh600519)"""
    sym = symbol.strip().lower()

    # 已经是 sz/sh 格式
    if sym.startswith(("sz", "sh")):
        return sym

    # 去除 .SZ / .SH 后缀
    if "." in sym:
        code, market = sym.split(".")
        market = market.lower()
        if market == "sz":
            return f"sz{code}"
        elif market == "sh":
            return f"sh{code}"
        code = sym.split(".")[0]
    else:
        code = sym

    # 裸代码，根据首位判断市场
    # 深圳: 00xxxx (主板), 30xxxx (创业板), 39xxxx (指数)
    # 上海: 60xxxx (主板), 56xxxx (主板新股段，如 563080), 51xxxx, 50xxxx, 68xxxx (科创板)
    if code.startswith(("00", "30", "39")):
        return f"sz{code}"
    elif code.startswith(("60", "56", "51", "50", "68")):
        return f"sh{code}"

    # 默认深圳
    return f"sz{code}"


def _cn_now() -> datetime:
    return datetime.now(tz=_CN_TZ)


def _is_trading_time() -> bool:
    """判断是否在交易时间"""
    now = _cn_now()
    if now.weekday() >= 5:  # 周末
        return False
    t = now.hour * 60 + now.minute
    morning = 9 * 60 + 30 <= t < 11 * 60 + 30
    afternoon = 13 * 60 <= t < 15 * 60
    return morning or afternoon


class TDXSource(DataSource):
    """通达信数据源，使用 eltdx 库获取 A 股行情数据."""

    def __init__(self) -> None:
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._tdx_dir: str | None = None
        # 自选股缓存：(文件路径, mtime, size) → 代码列表，避免每次下拉重复读文件
        self._watchlist_cache_key: tuple[str, float, int] | None = None
        self._watchlist_cache_symbols: list[str] = []

    def connect(self) -> None:
        """连接到通达信服务"""
        try:
            # eltdx 使用上下文管理器，验证库是否可用
            from eltdx import TdxClient
            logger.info("TDX (eltdx) 已准备就绪")
            self._connected = True
        except ImportError as exc:
            logger.error("eltdx 未安装")
            raise DataSourceTransientError(
                "eltdx 未安装，请执行: pip install git+https://github.com/Neoooo0909/tdx-mcp.git"
            ) from exc

    def disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        logger.info("TDX 数据源已断开")

    def set_tdx_dir(self, tdx_dir: str | None) -> None:
        """设置通达信安装目录（读取自选股用）；None 时自动探测."""
        self._tdx_dir = (tdx_dir or "").strip() or None
        self._watchlist_cache_key = None
        self._watchlist_cache_symbols = []

    def watchlist_symbols(self) -> list[str]:
        """读取自选股代码（带 mtime 缓存，文件未变化时直接用缓存）."""
        from pa_agent.data.tdx_watchlist import read_watchlist_file, watchlist_file_for

        path = watchlist_file_for(self._tdx_dir)
        if path is None:
            return []
        try:
            stat = path.stat()
            key = (str(path), stat.st_mtime, stat.st_size)
        except OSError:
            key = None
        if key is not None and key == self._watchlist_cache_key:
            return self._watchlist_cache_symbols

        symbols = read_watchlist_file(path)
        self._watchlist_cache_key = key
        self._watchlist_cache_symbols = symbols
        return symbols

    def list_symbols(self) -> list[str]:
        """返回预设股票列表 + 通达信自选股."""
        symbols = list(_PRESET_SYMBOLS)
        seen = set(symbols)
        for sym in self.watchlist_symbols():
            if sym not in seen:
                seen.add(sym)
                symbols.append(sym)
        return symbols

    def supported_timeframes(self) -> list[str]:
        """返回支持的时间周期"""
        return list(_SUPPORTED_TIMEFRAMES)

    def subscribe(self, symbol: str, timeframe: str) -> None:
        """订阅指定品种和周期"""
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"不支持的周期: {timeframe!r}. "
                f"请使用: {list(_SUPPORTED_TIMEFRAMES)}"
            )
        self._symbol = symbol
        self._timeframe = timeframe
        logger.info("TDX 已订阅: %s %s", symbol, timeframe)

    def unsubscribe(self) -> None:
        """取消订阅"""
        self._symbol = ""
        self._timeframe = ""
        logger.info("TDX 已取消订阅")

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """获取最新的 n 根 K 线"""
        if not self._connected:
            raise DataSourceTransientError("TDX 未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("TDX 未订阅品种/周期")

        try:
            bars = self._fetch_bars(self._symbol, self._timeframe, n + 10)
            if not bars:
                raise DataSourceTransientError(
                    f"TDX 未返回数据: {self._symbol} {self._timeframe}"
                )

            # 转换为 KlineBar 格式
            result = self._convert_to_kline_bars(bars, n)
            return result

        except DataSourceTransientError:
            raise
        except Exception as exc:
            logger.warning("TDX 数据获取失败: %s", exc, exc_info=True)
            raise DataSourceTransientError(f"TDX 获取失败: {exc}") from exc

    def _fetch_bars(self, symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
        """从通达信获取 K 线数据"""
        try:
            from eltdx import TdxClient, KlinePeriod

            # 转换代码格式
            code = _normalize_code_to_eltdx(symbol)

            # 转换周期
            period_str = _TDX_PERIOD_MAP.get(timeframe, "DAY")
            period = getattr(KlinePeriod, period_str)

            # 使用上下文管理器创建客户端
            with TdxClient() as client:
                # 获取 K 线数据
                result = client.get_kline(code, period, count=count)

                if not result or not result.items:
                    raise DataSourceTransientError(f"未获取到数据: {symbol}")

                return self._parse_eltdx_bars(result.items)

        except ImportError as exc:
            raise DataSourceTransientError("eltdx 未安装") from exc
        except DataSourceTransientError:
            raise
        except Exception as exc:
            logger.warning("TDX 获取 K 线失败: %s", exc)
            raise DataSourceTransientError(f"TDX 获取 K 线失败: {exc}") from exc

    def _parse_eltdx_bars(self, items: list[Any]) -> list[dict[str, Any]]:
        """解析 eltdx K 线响应数据"""
        bars = []

        if not items:
            return []

        # eltdx 返回的是 KlineItem 对象列表
        for item in items:
            try:
                # KlineItem 属性: time, open_price, high_price, low_price, close_price, volume
                dt_value = item.time

                # 确保时区
                if isinstance(dt_value, datetime):
                    if dt_value.tzinfo is None:
                        dt = dt_value.replace(tzinfo=_CN_TZ)
                    else:
                        dt = dt_value.astimezone(_CN_TZ)
                else:
                    logger.debug("无法解析时间: %s", dt_value)
                    continue

                ts_ms = int(dt.timestamp() * 1000)

                bars.append({
                    "ts_open": ts_ms,
                    "open": float(item.open_price),
                    "high": float(item.high_price),
                    "low": float(item.low_price),
                    "close": float(item.close_price),
                    "volume": float(item.volume),
                })
            except (ValueError, AttributeError, TypeError) as exc:
                logger.debug("解析 K 线数据失败: %s, item=%s", exc, item)
                continue

        return bars

    def _convert_to_kline_bars(self, bars: list[dict[str, Any]], n: int) -> list[KlineBar]:
        """将原始数据转换为 KlineBar 对象"""
        # eltdx 返回的数据是从旧到新排列
        # 需要反转为从新到旧
        bars_desc = list(reversed(bars[-n:]))

        result: list[KlineBar] = []
        is_trading = _is_trading_time()

        for i, bar in enumerate(bars_desc):
            # 第一根 K 线如果在交易时间内，标记为未完成
            closed = True
            if i == 0 and is_trading:
                closed = False

            kbar = KlineBar(
                seq=i + 1,
                ts_open=float(bar["ts_open"]),
                open=bar["open"],
                high=bar["high"],
                low=bar["low"],
                close=bar["close"],
                volume=bar["volume"],
                closed=closed,
            )
            result.append(normalize_kline_bar(kbar))

        return result
