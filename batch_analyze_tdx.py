"""批量分析通达信自选股的命令行工具."""
import asyncio
import io
import logging
import sys
from pathlib import Path

# 设置标准输出编码为 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from pa_agent.analysis.batch_analyzer import (
    load_custom_stock_list,
    analyze_stock_batch,
    create_default_stock_list,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)


def progress_callback(current: int, total: int, symbol: str, success: bool):
    """进度回调函数."""
    status = "[OK]" if success else "[FAIL]"
    print(f"[{current}/{total}] {status} {symbol}")


async def main():
    """主函数."""
    import argparse

    parser = argparse.ArgumentParser(
        description='批量分析通达信自选股',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 分析自选股（使用默认文件 my_stocks.txt）
  python batch_analyze_tdx.py

  # 指定自选股文件
  python batch_analyze_tdx.py --stocks custom_stocks.txt

  # 指定时间周期和输出目录
  python batch_analyze_tdx.py --timeframe 15m --output results/

  # 创建示例自选股文件
  python batch_analyze_tdx.py --create-example my_stocks.txt
        """
    )

    parser.add_argument(
        '--stocks',
        default='my_stocks.txt',
        help='自选股列表文件路径（默认: my_stocks.txt）'
    )
    parser.add_argument(
        '--timeframe',
        default='1h',
        choices=['1m', '5m', '15m', '30m', '1h', '1d'],
        help='时间周期（默认: 1h）'
    )
    parser.add_argument(
        '--output',
        default='analysis_results',
        help='结果输出目录（默认: analysis_results/）'
    )
    parser.add_argument(
        '--create-example',
        metavar='FILE',
        help='创建示例自选股文件并退出'
    )

    args = parser.parse_args()

    # 创建示例文件
    if args.create_example:
        file_path = create_default_stock_list(args.create_example)
        print(f"[OK] 已创建示例自选股文件: {file_path}")
        print(f"     请编辑该文件，添加您的自选股代码")
        return

    # 加载自选股列表
    print(f"正在加载自选股列表: {args.stocks}")
    stock_codes = load_custom_stock_list(args.stocks)

    if not stock_codes:
        print(f"[ERROR] 未找到自选股或文件为空: {args.stocks}")
        print(f"\n提示: 使用 --create-example 创建示例文件:")
        print(f"  python {Path(__file__).name} --create-example my_stocks.txt")
        sys.exit(1)

    print(f"[OK] 加载了 {len(stock_codes)} 只股票")
    print(f"     时间周期: {args.timeframe}")
    print(f"     输出目录: {args.output}")
    print()

    # 显示股票列表
    print("自选股列表:")
    for i, code in enumerate(stock_codes, 1):
        print(f"  {i}. {code}")
    print()

    # 确认开始分析
    try:
        response = input("开始批量分析? [Y/n] ").strip().lower()
        if response and response != 'y':
            print("已取消")
            return
    except (KeyboardInterrupt, EOFError):
        print("\n已取消")
        return

    print("\n" + "=" * 60)
    print("开始批量分析...")
    print("=" * 60)

    # 执行批量分析
    results = await analyze_stock_batch(
        stock_codes=stock_codes,
        timeframe=args.timeframe,
        data_source_kind='tdx',
        output_dir=args.output,
        on_progress=progress_callback
    )

    # 显示统计结果
    print("\n" + "=" * 60)
    print("批量分析完成")
    print("=" * 60)

    success_count = sum(1 for r in results if r['success'])
    failed_count = len(results) - success_count

    print(f"总数: {len(results)}")
    print(f"成功: {success_count}")
    print(f"失败: {failed_count}")

    if failed_count > 0:
        print("\n失败的股票:")
        for r in results:
            if not r['success']:
                print(f"  {r['symbol']}: {r['error']}")

    print(f"\n结果已保存到: {args.output}/")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n程序已中断")
        sys.exit(130)
    except Exception as e:
        logger.error("程序异常: %s", e, exc_info=True)
        sys.exit(1)
