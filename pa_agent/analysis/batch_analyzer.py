"""批量分析通达信自选股的功能模块."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_custom_stock_list(file_path: str | Path) -> list[str]:
    """从文件加载自选股列表.

    支持的格式:
    1. 纯文本文件，每行一个股票代码
    2. JSON 文件，包含股票代码列表

    Args:
        file_path: 自选股列表文件路径

    Returns:
        股票代码列表
    """
    path = Path(file_path)

    if not path.exists():
        logger.error("自选股文件不存在: %s", path)
        return []

    try:
        content = path.read_text(encoding='utf-8')

        # 尝试解析为 JSON
        if path.suffix.lower() == '.json':
            data = json.loads(content)
            if isinstance(data, list):
                return [str(code).strip() for code in data if code]
            elif isinstance(data, dict) and 'codes' in data:
                return [str(code).strip() for code in data['codes'] if code]

        # 否则按文本格式处理，每行一个代码
        codes = []
        for line in content.splitlines():
            line = line.strip()
            # 跳过空行和注释
            if line and not line.startswith('#'):
                # 支持 "代码 名称" 格式，只取代码部分
                code = line.split()[0] if ' ' in line else line
                codes.append(code)

        return codes

    except Exception as exc:
        logger.error("读取自选股文件失败: %s", exc)
        return []


def save_analysis_result(
    symbol: str,
    timeframe: str,
    result: dict[str, Any],
    output_dir: str | Path = "analysis_results"
) -> Path | None:
    """保存单个股票的分析结果.

    Args:
        symbol: 股票代码
        timeframe: 时间周期
        result: 分析结果字典
        output_dir: 输出目录

    Returns:
        保存的文件路径，失败返回 None
    """
    try:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # 生成文件名: {代码}_{周期}_{时间戳}.json
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{symbol}_{timeframe}_{timestamp}.json"
        file_path = output_path / filename

        # 添加元数据
        output_data = {
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": timestamp,
            "analysis_time": datetime.now().isoformat(),
            "result": result
        }

        file_path.write_text(
            json.dumps(output_data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )

        logger.info("分析结果已保存: %s", file_path)
        return file_path

    except Exception as exc:
        logger.error("保存分析结果失败 %s: %s", symbol, exc)
        return None


def save_batch_summary(
    results: list[dict[str, Any]],
    output_dir: str | Path = "analysis_results"
) -> Path | None:
    """保存批量分析的汇总结果.

    Args:
        results: 所有股票的分析结果列表
        output_dir: 输出目录

    Returns:
        保存的文件路径，失败返回 None
    """
    try:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"batch_summary_{timestamp}.json"
        file_path = output_path / filename

        summary = {
            "batch_time": datetime.now().isoformat(),
            "total_count": len(results),
            "success_count": sum(1 for r in results if r.get('success')),
            "failed_count": sum(1 for r in results if not r.get('success')),
            "results": results
        }

        file_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )

        logger.info("批量分析汇总已保存: %s", file_path)
        return file_path

    except Exception as exc:
        logger.error("保存批量汇总失败: %s", exc)
        return None


async def analyze_stock_batch(
    stock_codes: list[str],
    timeframe: str = "1h",
    data_source_kind: str = "tdx",
    output_dir: str | Path = "analysis_results",
    on_progress: callable | None = None
) -> list[dict[str, Any]]:
    """批量分析多只股票.

    Args:
        stock_codes: 股票代码列表
        timeframe: 时间周期（默认 1h）
        data_source_kind: 数据源类型
        output_dir: 结果保存目录
        on_progress: 进度回调函数 (current, total, symbol, success)

    Returns:
        分析结果列表
    """
    from pa_agent.data.factory import create_data_source
    from pa_agent.orchestrator.two_stage import run_two_stage_analysis
    from pa_agent.app_context import AppContext

    results = []
    total = len(stock_codes)

    logger.info("开始批量分析 %d 只股票", total)

    # 创建数据源
    data_source = create_data_source(data_source_kind)

    try:
        data_source.connect()

        for idx, symbol in enumerate(stock_codes, 1):
            logger.info("正在分析 [%d/%d]: %s", idx, total, symbol)

            result = {
                "symbol": symbol,
                "timeframe": timeframe,
                "success": False,
                "error": None,
                "analysis": None,
                "file_path": None
            }

            try:
                # 订阅股票
                data_source.subscribe(symbol, timeframe)

                # 获取 K 线数据
                bars = data_source.latest_snapshot(100)  # 获取 100 根 K 线

                if not bars:
                    raise ValueError(f"未获取到 {symbol} 的数据")

                # 执行两阶段分析
                # 注意：这里需要根据实际的 run_two_stage_analysis 接口调整
                analysis_result = await run_two_stage_analysis(
                    symbol=symbol,
                    timeframe=timeframe,
                    bars=bars,
                    data_source=data_source
                )

                result["success"] = True
                result["analysis"] = analysis_result

                # 保存单个结果
                file_path = save_analysis_result(
                    symbol, timeframe, analysis_result, output_dir
                )
                result["file_path"] = str(file_path) if file_path else None

                logger.info("✓ %s 分析完成", symbol)

            except Exception as exc:
                result["error"] = str(exc)
                logger.error("✗ %s 分析失败: %s", symbol, exc)

            results.append(result)

            # 调用进度回调
            if on_progress:
                on_progress(idx, total, symbol, result["success"])

        # 保存批量汇总
        save_batch_summary(results, output_dir)

    finally:
        data_source.disconnect()

    success_count = sum(1 for r in results if r["success"])
    logger.info("批量分析完成: 成功 %d/%d", success_count, total)

    return results


def create_default_stock_list(file_path: str | Path = "my_stocks.txt") -> Path:
    """创建默认的自选股列表文件（示例）.

    Args:
        file_path: 要创建的文件路径

    Returns:
        创建的文件路径
    """
    path = Path(file_path)

    # 默认自选股示例
    default_stocks = [
        "# 自选股列表",
        "# 每行一个股票代码，支持格式: 000001 或 sz000001 或 000001.SZ",
        "# 以 # 开头的行为注释",
        "",
        "000001  # 平安银行",
        "600519  # 贵州茅台",
        "000858  # 五粮液",
        "600036  # 招商银行",
        "000333  # 美的集团",
    ]

    path.write_text('\n'.join(default_stocks), encoding='utf-8')
    logger.info("已创建示例自选股文件: %s", path)

    return path
