"""简化版批量分析通达信自选股工具.

这个脚本读取自选股列表，使用通达信数据源获取1小时K线，
然后对每只股票执行分析并保存结果。
"""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)

logger = logging.getLogger(__name__)


def load_stock_list(file_path: str = "my_stocks.txt") -> list[str]:
    """加载自选股列表."""
    path = Path(file_path)

    if not path.exists():
        logger.error("自选股文件不存在: %s", path)
        return []

    codes = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        # 跳过空行和注释
        if line and not line.startswith('#'):
            # 支持 "代码 名称" 格式
            code = line.split()[0] if ' ' in line else line
            codes.append(code)

    return codes


def save_result(symbol: str, timeframe: str, bars_data: list, output_dir: str = "batch_results"):
    """保存分析结果."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{symbol}_{timeframe}_{timestamp}.json"
    file_path = output_path / filename

    # 转换 KlineBar 对象为字典
    bars_list = []
    for bar in bars_data:
        bars_list.append({
            "seq": bar.seq,
            "ts_open": bar.ts_open,
            "datetime": datetime.fromtimestamp(bar.ts_open / 1000).strftime("%Y-%m-%d %H:%M:%S"),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "closed": bar.closed,
        })

    data = {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": timestamp,
        "analysis_time": datetime.now().isoformat(),
        "bars_count": len(bars_data),
        "bars": bars_list,
    }

    file_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    logger.info("已保存: %s", file_path)
    return file_path


def batch_analyze(stock_list_file: str = "my_stocks.txt", timeframe: str = "1h"):
    """批量分析自选股."""
    from pa_agent.data.factory import create_data_source

    # 加载自选股列表
    logger.info("加载自选股列表: %s", stock_list_file)
    stock_codes = load_stock_list(stock_list_file)

    if not stock_codes:
        logger.error("未找到自选股")
        return

    logger.info("共加载 %d 只股票", len(stock_codes))
    logger.info("时间周期: %s", timeframe)
    print()

    # 创建通达信数据源
    logger.info("创建通达信数据源...")
    tdx = create_data_source('tdx')
    tdx.connect()

    results = []
    success_count = 0
    failed_count = 0

    try:
        for idx, symbol in enumerate(stock_codes, 1):
            logger.info("[%d/%d] 分析 %s...", idx, len(stock_codes), symbol)

            try:
                # 订阅
                tdx.subscribe(symbol, timeframe)

                # 获取K线数据
                bars = tdx.latest_snapshot(100)

                if not bars:
                    raise ValueError(f"未获取到数据")

                logger.info("  获取到 %d 根K线", len(bars))

                # 保存结果
                file_path = save_result(symbol, timeframe, bars)

                results.append({
                    "symbol": symbol,
                    "success": True,
                    "bars_count": len(bars),
                    "file": str(file_path)
                })

                success_count += 1
                logger.info("  [OK] %s 完成", symbol)

            except Exception as e:
                logger.error("  [FAIL] %s 失败: %s", symbol, e)
                results.append({
                    "symbol": symbol,
                    "success": False,
                    "error": str(e)
                })
                failed_count += 1

            print()

    finally:
        tdx.disconnect()

    # 保存汇总
    summary_path = Path("batch_results") / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "batch_time": datetime.now().isoformat(),
        "timeframe": timeframe,
        "total": len(stock_codes),
        "success": success_count,
        "failed": failed_count,
        "results": results
    }

    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    # 打印统计
    print("=" * 60)
    print("批量分析完成")
    print("=" * 60)
    print(f"总数: {len(stock_codes)}")
    print(f"成功: {success_count}")
    print(f"失败: {failed_count}")
    print(f"\n结果保存在: batch_results/")
    print(f"汇总文件: {summary_path}")


def main():
    """主函数."""
    import argparse

    parser = argparse.ArgumentParser(description='批量获取通达信自选股行情')
    parser.add_argument('--stocks', default='my_stocks.txt', help='自选股列表文件')
    parser.add_argument('--timeframe', default='1h', choices=['1m', '5m', '15m', '30m', '1h', '1d'], help='时间周期')
    parser.add_argument('--create-example', action='store_true', help='创建示例自选股文件')

    args = parser.parse_args()

    if args.create_example:
        example_stocks = [
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
        Path('my_stocks.txt').write_text('\n'.join(example_stocks), encoding='utf-8')
        print("[OK] 已创建示例文件: my_stocks.txt")
        print("     请编辑该文件添加您的自选股")
        return

    batch_analyze(args.stocks, args.timeframe)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已中断")
        sys.exit(130)
    except Exception as e:
        logger.error("程序异常: %s", e, exc_info=True)
        sys.exit(1)
