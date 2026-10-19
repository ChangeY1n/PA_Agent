"""批量分析通达信自选股 - 多线程版本."""
import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from queue import Queue

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s',
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
        if line and not line.startswith('#'):
            code = line.split()[0] if ' ' in line else line
            codes.append(code)

    return codes


def analyze_single_stock(
    symbol: str,
    timeframe: str,
    output_dir: str,
    ctx_factory: callable
) -> dict:
    """分析单只股票（线程安全版本）."""
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.data.base import KlineFrame
    from pa_agent.util.threading import CancelToken
    from pa_agent.data.factory import create_data_source

    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "success": False,
        "error": None,
        "file": None,
        "decision": None,
    }

    # 每个线程创建自己的数据源连接
    tdx = None

    try:
        # 创建线程本地的 AppContext
        ctx = ctx_factory()

        # 创建数据源
        tdx = create_data_source('tdx')
        tdx.connect()

        # 订阅股票
        tdx.subscribe(symbol, timeframe)

        # 获取 K 线数据
        bars = tdx.latest_snapshot(100)

        if not bars:
            raise ValueError(f"未获取到数据")

        logger.info("[%s] 获取到 %d 根 K 线", symbol, len(bars))

        # 构建 KlineFrame
        from pa_agent.data.base import IndicatorBundle
        from pa_agent.util.timefmt import now_local_ms

        # 计算指标
        def calculate_indicators(bars):
            """计算 EMA20 和 ATR14 指标."""
            import numpy as np

            bar_count = len(bars)
            closes = [bar.close for bar in bars]
            highs = [bar.high for bar in bars]
            lows = [bar.low for bar in bars]

            # 计算 EMA20（从旧到新）
            ema20_values = []
            ema = None
            alpha = 2 / (20 + 1)

            for close in reversed(closes):  # 从最旧的开始
                if ema is None:
                    ema = close
                else:
                    ema = alpha * close + (1 - alpha) * ema
                ema20_values.append(ema)

            ema20_values.reverse()  # 转回从新到旧

            # 计算 ATR14
            atr14_values = []
            tr_values = []

            # 从旧到新计算
            for i in range(len(bars) - 1, -1, -1):
                if i == len(bars) - 1:
                    # 第一根K线
                    tr = highs[i] - lows[i]
                else:
                    high_low = highs[i] - lows[i]
                    high_close = abs(highs[i] - closes[i + 1])
                    low_close = abs(lows[i] - closes[i + 1])
                    tr = max(high_low, high_close, low_close)
                tr_values.append(tr)

            # 计算 ATR（14周期移动平均）
            atr = None
            alpha_atr = 1 / 14

            for tr in tr_values:
                if atr is None:
                    atr = tr
                else:
                    atr = alpha_atr * tr + (1 - alpha_atr) * atr
                atr14_values.append(atr)

            atr14_values.reverse()  # 转回从新到旧

            return tuple(ema20_values), tuple(atr14_values)

        ema20_vals, atr14_vals = calculate_indicators(bars)

        indicators = IndicatorBundle(
            ema20=ema20_vals,
            atr14=atr14_vals
        )

        kline_frame = KlineFrame(
            symbol=symbol,
            timeframe=timeframe,
            bars=tuple(bars),
            indicators=indicators,
            snapshot_ts_local_ms=now_local_ms()
        )

        # 创建分析器
        orchestrator = TwoStageOrchestrator(
            client=ctx.client,
            assembler=ctx.assembler,
            router=ctx.router,
            validator=ctx.validator,
            pending_writer=ctx.pending_writer,
            exp_reader=ctx.exp_reader,
            csv_logger=ctx.csv_logger,
            excel_logger=ctx.excel_logger,
        )

        # 执行两阶段分析
        cancel_token = CancelToken()

        record = orchestrator.submit(
            frame=kline_frame,
            cancel_token=cancel_token,
            on_event=lambda evt: None  # 多线程模式下不打印事件
        )

        # 检查分析是否成功
        logger.debug("[%s] record: stage1=%s, stage2=%s",
                    symbol,
                    bool(record.stage1_diagnosis),
                    bool(record.stage2_decision))

        if record.stage2_decision:
            result["success"] = True
            result["decision"] = {
                "操作方向": record.stage2_decision.get("操作方向"),
                "入场建议": record.stage2_decision.get("入场建议"),
                "风险水平": record.stage2_decision.get("风险水平"),
                "止损价": record.stage2_decision.get("止损价"),
                "目标价": record.stage2_decision.get("目标价"),
            }

            # 保存完整记录
            record_dict = {
                "meta": {
                    "symbol": record.meta.symbol,
                    "timeframe": record.meta.timeframe,
                    "timestamp": record.meta.timestamp,
                },
                "stage1_diagnosis": record.stage1_diagnosis,
                "stage2_decision": record.stage2_decision,
                "strategy_files_used": record.strategy_files_used,
            }

            # 保存文件
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # 添加线程ID避免文件名冲突
            filename = f"{symbol}_{timeframe}_{timestamp}_{threading.current_thread().ident}.json"
            file_path = output_path / filename

            file_path.write_text(
                json.dumps(record_dict, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )

            result["file"] = str(file_path)

            logger.info("[OK] %s 分析完成: %s",
                       symbol,
                       record.stage2_decision.get("操作方向", "N/A"))
        else:
            # 保存失败的记录以便调试
            error_msg = f"分析未返回决策结果"
            if record.exception:
                error_msg = f"{error_msg}: {record.exception}"

            logger.warning("[%s] %s (stage1=%s, stage2=%s)",
                         symbol, error_msg,
                         "OK" if record.stage1_diagnosis else "FAIL",
                         "OK" if record.stage2_decision else "FAIL")

            raise ValueError(error_msg)

    except Exception as e:
        result["error"] = str(e)
        logger.error("[FAIL] %s 分析失败: %s", symbol, e)

    finally:
        if tdx:
            try:
                tdx.disconnect()
            except Exception:
                pass

    return result


def batch_analyze_with_ai_multithread(
    stock_list_file: str = "my_stocks.txt",
    timeframe: str = "1h",
    output_dir: str = "batch_analysis_results",
    max_workers: int = 10
):
    """使用多线程批量分析自选股."""
    from pa_agent.app_context import AppContext

    # 加载自选股列表
    logger.info("加载自选股列表: %s", stock_list_file)
    stock_codes = load_stock_list(stock_list_file)

    if not stock_codes:
        logger.error("未找到自选股")
        return

    total = len(stock_codes)
    logger.info("共加载 %d 只股票", total)
    logger.info("时间周期: %s", timeframe)
    logger.info("并发线程数: %d", max_workers)
    logger.info("预计耗时: %.1f 分钟", total * 2 / max_workers / 60)  # 假设每只2秒
    print()

    # 创建 AppContext 工厂函数
    def create_context():
        """为每个线程创建独立的 AppContext."""
        return AppContext.bootstrap()

    results = []
    completed = 0
    start_time = datetime.now()

    # 使用线程池
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="Analyzer") as executor:
        # 提交所有任务
        future_to_symbol = {
            executor.submit(analyze_single_stock, symbol, timeframe, output_dir, create_context): symbol
            for symbol in stock_codes
        }

        # 处理完成的任务
        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                result = future.result()
                results.append(result)
                completed += 1

                # 打印进度
                elapsed = (datetime.now() - start_time).total_seconds()
                progress = completed / total * 100
                avg_time = elapsed / completed
                remaining = (total - completed) * avg_time

                logger.info(
                    "进度: %d/%d (%.1f%%) | 已用时: %.1f分钟 | 预计剩余: %.1f分钟",
                    completed, total, progress,
                    elapsed / 60, remaining / 60
                )

            except Exception as e:
                logger.error("处理 %s 时出错: %s", symbol, e)
                results.append({
                    "symbol": symbol,
                    "success": False,
                    "error": str(e)
                })
                completed += 1

    # 统计
    success_count = sum(1 for r in results if r["success"])
    failed_count = total - success_count
    total_time = (datetime.now() - start_time).total_seconds() / 60

    # 保存汇总
    summary_path = Path(output_dir) / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "batch_time": datetime.now().isoformat(),
        "timeframe": timeframe,
        "total": total,
        "success": success_count,
        "failed": failed_count,
        "duration_minutes": round(total_time, 2),
        "max_workers": max_workers,
        "results": results
    }

    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    # 导出值得参与的 setup
    export_worthy_setups(results, output_dir)

    # 打印统计
    print()
    print("=" * 60)
    print("批量分析完成")
    print("=" * 60)
    print(f"总数: {total}")
    print(f"成功: {success_count}")
    print(f"失败: {failed_count}")
    print(f"总耗时: {total_time:.1f} 分钟")
    print(f"平均速度: {total_time * 60 / total:.1f} 秒/股票")
    print(f"\n结果保存在: {output_dir}/")
    print(f"汇总文件: {summary_path}")


def export_worthy_setups(results: list[dict], output_dir: str):
    """导出值得参与的 setup."""
    worthy = []

    for r in results:
        if not r["success"]:
            continue

        decision = r.get("decision", {})
        operation = decision.get("操作方向", "")

        # 筛选条件：非观望状态
        if operation and operation != "观望":
            worthy.append({
                "股票代码": r["symbol"],
                "操作方向": operation,
                "入场建议": decision.get("入场建议", ""),
                "风险水平": decision.get("风险水平", ""),
                "止损价": decision.get("止损价", ""),
                "目标价": decision.get("目标价", ""),
            })

    if not worthy:
        logger.info("未找到值得参与的 setup")
        return

    # 保存为 JSON
    output_path = Path(output_dir) / f"worthy_setups_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_path.write_text(
        json.dumps(worthy, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    # 保存为文本表格
    txt_path = Path(output_dir) / f"worthy_setups_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    lines = [
        "值得参与的 Setup 列表",
        "=" * 80,
        f"总计: {len(worthy)} 个",
        ""
    ]

    for i, setup in enumerate(worthy, 1):
        lines.append(f"{i}. {setup['股票代码']} - {setup['操作方向']}")
        lines.append(f"   入场: {setup['入场建议']}")
        lines.append(f"   风险: {setup['风险水平']}")
        lines.append(f"   止损: {setup['止损价']}")
        lines.append(f"   目标: {setup['目标价']}")
        lines.append("")

    txt_path.write_text('\n'.join(lines), encoding='utf-8')

    logger.info(f"发现 {len(worthy)} 个值得参与的 setup")
    logger.info(f"已导出到: {output_path}")
    logger.info(f"文本版本: {txt_path}")

    # 打印前10个
    print(f"\n值得参与的 Setup 总数: {len(worthy)}")
    print("\n前10个:")
    for i, setup in enumerate(worthy[:10], 1):
        print(f"  {i}. {setup['股票代码']} - {setup['操作方向']} ({setup['风险水平']})")


def main():
    """主函数."""
    import argparse

    parser = argparse.ArgumentParser(description='批量 AI 分析通达信自选股（多线程版）')
    parser.add_argument('--stocks', default='my_stocks.txt', help='自选股列表文件')
    parser.add_argument('--timeframe', default='1h',
                       choices=['1m', '5m', '15m', '30m', '1h', '1d'],
                       help='时间周期')
    parser.add_argument('--output', default='batch_analysis_results',
                       help='结果输出目录')
    parser.add_argument('--workers', type=int, default=10,
                       help='并发线程数（默认10，建议5-20之间）')

    args = parser.parse_args()

    # 计算预计时间
    stock_count = len(load_stock_list(args.stocks))
    estimated_minutes = stock_count * 2 / args.workers / 60

    print(f"准备分析 {stock_count} 只股票")
    print(f"并发数: {args.workers} 线程")
    print(f"预计耗时: {estimated_minutes:.1f} 分钟")
    print()

    try:
        response = input("开始批量分析? [Y/n] ").strip().lower()
        if response and response != 'y':
            print("已取消")
            return
    except (KeyboardInterrupt, EOFError):
        print("\n已取消")
        return

    batch_analyze_with_ai_multithread(args.stocks, args.timeframe, args.output, args.workers)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已中断")
        sys.exit(130)
    except Exception as e:
        logger.error("程序异常: %s", e, exc_info=True)
        sys.exit(1)
