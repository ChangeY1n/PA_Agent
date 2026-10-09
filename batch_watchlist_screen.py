"""批量筛选通达信自选股：跑两阶段 AI 分析并列出有交易机会的决策.

用法:
    python batch_watchlist_screen.py                 # 自选前 10 只试跑
    python batch_watchlist_screen.py --limit 50      # 跑前 50 只
    python batch_watchlist_screen.py --limit 0       # 全部自选
    python batch_watchlist_screen.py --timeframe 1d  # 指定周期（默认沿用设置中的上次周期）

说明:
- 股票列表直接读取通达信自选股（与 GUI 下拉列表同源，含 ↻ 自选 的重读逻辑）。
- 每只股票走与 GUI 完全相同的分析管线（build_display_frame + TwoStageOrchestrator），
  记录照常写入 records/pending，之后在 GUI 中切到该股即可增量分析。
- 「有交易机会」= 阶段二决策 order_type 不是「不下单」。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_screen")

# 进程级锁：csv_logger 无内建锁，多线程写同一 CSV 需串行化
_CSV_LOCK = threading.Lock()
_CTX_LOCAL = threading.local()

NO_ORDER = "不下单"


def thread_ctx():
    """每个工作线程一个独立 AppContext（客户端/编排器不跨线程共享）。"""
    ctx = getattr(_CTX_LOCAL, "ctx", None)
    if ctx is not None:
        return ctx

    from pa_agent.app_context import AppContext

    ctx = AppContext.bootstrap()

    # csv_logger 并发写保护（excel_logger 内部已有锁）
    csv_logger = getattr(ctx, "csv_logger", None)
    if csv_logger is not None:
        orig = csv_logger.log_decision

        def _locked(rec, _orig=orig):
            with _CSV_LOCK:
                _orig(rec)

        csv_logger.log_decision = _locked

    _CTX_LOCAL.ctx = ctx
    return ctx


def analyze_one(symbol: str, timeframe: str, bar_count: int) -> dict:
    """分析单只股票：取 K 线 → 建帧 → 两阶段分析 → 分类决策。"""
    from pa_agent.data.factory import create_data_source
    from pa_agent.data.snapshot import (
        INDICATOR_WARMUP_BARS,
        build_display_frame,
    )
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.util.threading import CancelToken

    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "failed",  # opportunity | no_order | failed
        "order_type": "",
        "order_direction": "",
        "entry_price": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "trade_confidence": None,
        "reasoning": "",
        "error": None,
    }

    tdx = None
    t0 = time.monotonic()
    try:
        ctx = thread_ctx()

        tdx = create_data_source("tdx")
        tdx.connect()
        tdx.subscribe(symbol, timeframe)
        bars = tdx.latest_snapshot(bar_count + INDICATOR_WARMUP_BARS + 5)
        if not bars:
            raise ValueError("未获取到 K 线数据")

        frame = build_display_frame(bars, bar_count, symbol, timeframe)
        if frame is None:
            raise ValueError("K 线不足以构建分析帧")

        orchestrator = TwoStageOrchestrator(
            client=ctx.client,
            assembler=ctx.assembler,
            router=ctx.router,
            validator=ctx.validator,
            pending_writer=ctx.pending_writer,
            exp_reader=ctx.exp_reader,
            settings=ctx.settings,
            csv_logger=ctx.csv_logger,
            excel_logger=ctx.excel_logger,
        )
        record = orchestrator.submit(frame, CancelToken(), on_event=lambda evt: None)

        if record.exception is not None:
            exc = record.exception
            raise ValueError(
                f"{exc.get('type', 'error')}: {exc.get('message', '')}"
                if isinstance(exc, dict)
                else str(exc)
            )

        decision = record.stage2_decision or {}
        inner = decision.get("decision", {}) if isinstance(decision, dict) else {}
        order = str(inner.get("order_type") or "")
        result["order_type"] = order
        result["order_direction"] = str(inner.get("order_direction") or "")
        result["entry_price"] = inner.get("entry_price")
        result["stop_loss_price"] = inner.get("stop_loss_price")
        result["take_profit_price"] = inner.get("take_profit_price")
        result["trade_confidence"] = inner.get("trade_confidence")
        result["reasoning"] = str(inner.get("reasoning") or "")
        result["status"] = "opportunity" if order and order != NO_ORDER else "no_order"

    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    finally:
        if tdx is not None:
            try:
                tdx.disconnect()
            except Exception:  # noqa: BLE001
                pass

    result["seconds"] = round(time.monotonic() - t0, 1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="批量筛选通达信自选股（两阶段 AI 分析）")
    parser.add_argument("--limit", type=int, default=10,
                        help="取自选前 N 只（0 = 全部，默认 10）")
    parser.add_argument("--timeframe", default="",
                        help="分析周期（默认沿用 config/settings.json 的上次周期）")
    parser.add_argument("--workers", type=int, default=5,
                        help="并发线程数（默认 5）")
    parser.add_argument("--offset", type=int, default=0,
                        help="跳过自选前 N 只（配合 --limit 分批跑）")
    parser.add_argument("--output", default="batch_analysis_results",
                        help="结果输出目录")
    args = parser.parse_args()

    from pa_agent.config.settings import load_settings
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.data.factory import create_data_source

    settings = load_settings(SETTINGS_JSON_PATH)
    timeframe = args.timeframe or getattr(
        settings.general, "last_timeframe", "1h"
    ) or "1h"
    bar_count = int(getattr(settings.general, "analysis_bar_count", 100) or 100)

    # 读取自选股（与 GUI 同源：设置中的通达信目录 + 自动探测兜底）
    probe = create_data_source("tdx")
    try:
        symbols = list(probe.watchlist_symbols())
    finally:
        try:
            probe.disconnect()
        except Exception:  # noqa: BLE001
            pass
    if not symbols:
        logger.error("未读取到通达信自选股（检查设置中的通达信安装目录）")
        return 1

    total_watchlist = len(symbols)
    symbols = symbols[args.offset:]
    if args.limit > 0:
        symbols = symbols[: args.limit]
    if not symbols:
        logger.error("offset=%d 超出自选总数 %d", args.offset, total_watchlist)
        return 1

    logger.info(
        "自选共 %d 只，本次分析 %d 只（offset=%d）· 周期 %s · %d 根K线 · %d 并发",
        total_watchlist, len(symbols), args.offset, timeframe,
        bar_count, args.workers,
    )

    results: list[dict] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="screen") as ex:
        futures = {ex.submit(analyze_one, s, timeframe, bar_count): s for s in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            sym = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001
                r = {"symbol": sym, "status": "failed", "error": str(exc),
                     "timeframe": timeframe, "order_type": "", "reasoning": ""}
            results.append(r)
            mark = {"opportunity": "★机会", "no_order": "—观望",
                    "failed": "✗失败"}[r["status"]]
            logger.info(
                "[%d/%d] %s %s %s%s",
                i, len(symbols), sym, mark, r.get("order_type") or "",
                f" ({r['error'][:80]})" if r["status"] == "failed" else "",
            )

    elapsed = time.monotonic() - t0
    opportunities = sorted(
        (r for r in results if r["status"] == "opportunity"),
        key=lambda r: -(r.get("trade_confidence") or 0),
    )
    no_order = [r for r in results if r["status"] == "no_order"]
    failed = [r for r in results if r["status"] == "failed"]

    # ── 保存结果 ────────────────────────────────────────────────────────────
    out_dir = Path(args.output) / datetime.now().strftime("screen_%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "batch_time": datetime.now().isoformat(),
                "timeframe": timeframe,
                "bar_count": bar_count,
                "total": len(results),
                "opportunities": len(opportunities),
                "no_order": len(no_order),
                "failed": len(failed),
                "duration_seconds": round(elapsed, 1),
                "results": results,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    # ── 汇总输出 ────────────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print(f"批量筛选完成：{len(results)} 只 · 周期 {timeframe} · 耗时 {elapsed/60:.1f} 分钟")
    print(f"有交易机会 {len(opportunities)} · 观望 {len(no_order)} · 失败 {len(failed)}")
    print("=" * 78)

    if opportunities:
        print(f"\n{'代码':<8}{'方向':<6}{'单型':<10}{'入场':>9}{'止损':>9}{'目标':>9}{'信心':>6}  理由")
        print("-" * 78)
        for r in opportunities:
            def _fmt(v, fmt="{:.2f}"):
                return fmt.format(v) if isinstance(v, (int, float)) else str(v or "—")
            conf = r.get("trade_confidence")
            reason = (r.get("reasoning") or "").replace("\n", " ")[:36]
            print(
                f"{r['symbol']:<8}{r.get('order_direction', '') or '—':<6}"
                f"{r.get('order_type', ''):<10}"
                f"{_fmt(r.get('entry_price')):>9}{_fmt(r.get('stop_loss_price')):>9}"
                f"{_fmt(r.get('take_profit_price')):>9}"
                f"{(str(conf) if conf is not None else '—'):>6}  {reason}"
            )
    else:
        print("\n本次没有筛选出交易机会（全部观望/不下单）")

    if failed:
        print("\n失败列表:")
        for r in failed:
            print(f"  {r['symbol']}: {r['error']}")

    print(f"\n完整结果: {out_dir / 'summary.json'}")
    print("每只股票的完整分析已存入 records/pending，GUI 切到该股可查看/增量分析")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
