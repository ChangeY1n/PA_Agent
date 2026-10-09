"""批量筛选通达信自选股：跑两阶段 AI 分析并列出有交易机会的决策.

用法:
    python batch_watchlist_screen.py                 # 自选前 10 只试跑（轻量初筛模式）
    python batch_watchlist_screen.py --limit 50      # 跑前 50 只
    python batch_watchlist_screen.py --limit 0       # 全部自选
    python batch_watchlist_screen.py --mode full     # 跳过初筛，全部走完整两阶段
    python batch_watchlist_screen.py --light-only    # 只跑初筛不深析（校准/省钱巡视）
    python batch_watchlist_screen.py --timeframe 1d  # 指定周期（默认沿用设置中的上次周期）

说明:
- 股票列表直接读取通达信自选股（与 GUI 下拉列表同源，含 ↻ 自选 的重读逻辑）。
- 默认「轻量初筛」：每只先用一次约 0.8 万 token 的紧凑调用粗筛，通过的才跑
  完整两阶段（每只约 16 万 token）；初筛方向为空头的直接淘汰（A股无便利做空，
  历史全部出单均为做多；--allow-short 关闭该过滤）。
- 每只进入完整分析的股票走与 GUI 完全相同的管线（build_display_frame +
  TwoStageOrchestrator），记录照常写入 records/pending，之后在 GUI 中切到
  该股即可增量分析。
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

def local_no_trade_gate(stage1_json: dict) -> bool:
    """阶段一本地短路：True = 跳过阶段二（省 ~60% token）。

    仅当 router 一个策略文件都不会加载时触发（direction=neutral 且
    cycle_position ∈ {spike, extreme_tr}）：阶段二无任何策略可依，
    历史记录 9/9 全部判「不下单」，跳过等价于省掉走过场。

    刻意不扩大到 neutral+区间/宽通道：600333（neutral+broad_channel，
    2026-10-08）检测到 breakout_test 后阶段二给出过限价单，扩大会误杀
    真实机会；也不看 detected_patterns —— 形态集合在出单/不出单两组
    几乎相同，无法区分。
    """
    from pa_agent.ai.router import route_strategy_files

    try:
        return not route_strategy_files(stage1_json)
    except Exception:  # noqa: BLE001
        return False


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


def analyze_one(
    symbol: str,
    timeframe: str,
    bar_count: int,
    use_local_gate: bool = True,
    *,
    mode: str = "light",
    light_only: bool = False,
    light_bars: int = 60,
    long_only: bool = True,
) -> dict:
    """分析单只股票：取 K 线 →（可选）轻量初筛 → 两阶段分析 → 分类决策。"""
    from pa_agent.ai.light_screen import LIGHT_SCREEN_DEFAULT_BARS, run_light_screen
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
        "status": "failed",  # opportunity | no_order | screened_out | light_pass | failed
        "order_type": "",
        "order_direction": "",
        "entry_price": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "trade_confidence": None,
        "reasoning": "",
        "error": None,
        "light": None,  # 初筛判定 {pass, direction, setup, conviction, reason}
        "tokens_light": 0,
        "tokens_full": 0,
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

        if mode == "light":
            n_light = max(20, min(light_bars or LIGHT_SCREEN_DEFAULT_BARS, bar_count))
            light_frame = build_display_frame(bars, n_light, symbol, timeframe)
            if light_frame is None:
                raise ValueError("K 线不足以构建初筛帧")
            light = run_light_screen(ctx.client, light_frame)
            result["light"] = {
                k: light.get(k) for k in ("pass", "direction", "setup", "conviction", "reason")
            }
            result["tokens_light"] = (light.get("usage_total") or {}).get("total_tokens") or 0
            if not light.get("pass"):
                result["status"] = "screened_out"
                result["reasoning"] = str(light.get("reason") or "")
                return _finish(result, t0)
            if long_only and str(light.get("direction") or "") == "bearish":
                # A股无便利做空；历史全部出单（6/6）均为做多，空头方向不深析。
                result["status"] = "screened_out"
                result["reasoning"] = "（只做多模式：初筛方向为空头）"
                return _finish(result, t0)
            if light_only:
                result["status"] = "light_pass"
                return _finish(result, t0)

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
        record = orchestrator.submit(
            frame,
            CancelToken(),
            on_event=lambda evt: None,
            local_gate=local_no_trade_gate if use_local_gate else None,
        )

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
        result["tokens_full"] = (record.usage_total or {}).get("total_tokens") or 0

    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    finally:
        if tdx is not None:
            try:
                tdx.disconnect()
            except Exception:  # noqa: BLE001
                pass

    return _finish(result, t0)


def _finish(result: dict, t0: float) -> dict:
    result["seconds"] = round(time.monotonic() - t0, 1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="批量筛选通达信自选股（两阶段 AI 分析）")
    parser.add_argument("--mode", choices=("light", "full"), default="light",
                        help="light=先轻量初筛再深析（默认，省 60-70%% token）；"
                             "full=全部直接完整两阶段")
    parser.add_argument("--light-only", action="store_true",
                        help="只跑轻量初筛、不跑完整分析（校准/省钱巡视用）")
    parser.add_argument("--light-bars", type=int, default=0,
                        help="初筛用 K 线根数（默认 60）")
    parser.add_argument("--allow-short", action="store_true",
                        help="初筛为空头方向的也进入深析（默认只做多：A股无便利做空，"
                             "历史全部出单均为做多）")
    parser.add_argument("--limit", type=int, default=10,
                        help="取自选前 N 只（0 = 全部，默认 10）")
    parser.add_argument("--timeframe", default="",
                        help="分析周期（默认沿用 config/settings.json 的上次周期）")
    parser.add_argument("--workers", type=int, default=5,
                        help="并发线程数（默认 5）")
    parser.add_argument("--bars", type=int, default=0,
                        help="K 线根数（默认用设置中的 analysis_bar_count；批量可降到 60 省 token）")
    parser.add_argument("--no-local-gate", action="store_true",
                        help="禁用阶段一本地短路（默认开启：阶段一诊断无可用"
                             "策略文件时直接判观望，跳过阶段二）")
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
    if args.bars > 0:
        bar_count = args.bars

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
        "自选共 %d 只，本次分析 %d 只（offset=%d）· 周期 %s · %d 根K线 · %d 并发 · "
        "模式 %s · 只做多 %s · 本地短路 %s",
        total_watchlist, len(symbols), args.offset, timeframe,
        bar_count, args.workers, args.mode,
        "关" if args.allow_short else "开",
        "关" if args.no_local_gate else "开",
    )

    light_bars = args.light_bars if args.light_bars > 0 else 60

    results: list[dict] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="screen") as ex:
        futures = {
            ex.submit(analyze_one, s, timeframe, bar_count,
                      not args.no_local_gate,
                      mode=args.mode, light_only=args.light_only,
                      light_bars=light_bars,
                      long_only=not args.allow_short): s
            for s in symbols
        }
        for i, fut in enumerate(as_completed(futures), 1):
            sym = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001
                r = {"symbol": sym, "status": "failed", "error": str(exc),
                     "timeframe": timeframe, "order_type": "", "reasoning": "",
                     "light": None, "tokens_light": 0, "tokens_full": 0}
            results.append(r)
            mark = {"opportunity": "★机会", "no_order": "—观望",
                    "screened_out": "·淘汰", "light_pass": "+入围",
                    "failed": "✗失败"}[r["status"]]
            light_note = ""
            if r.get("light") and r.get("tokens_light"):
                light_note = " [初筛%s %s]" % (
                    "过" if r["light"].get("pass") else "淘汰",
                    r["light"].get("direction") or "?",
                )
            logger.info(
                "[%d/%d] %s %s %s%s%s",
                i, len(symbols), sym, mark, r.get("order_type") or "",
                light_note,
                f" ({r['error'][:80]})" if r["status"] == "failed" else "",
            )

    elapsed = time.monotonic() - t0
    opportunities = sorted(
        (r for r in results if r["status"] == "opportunity"),
        key=lambda r: -(r.get("trade_confidence") or 0),
    )
    no_order = [r for r in results if r["status"] == "no_order"]
    screened_out = [r for r in results if r["status"] == "screened_out"]
    light_pass = [r for r in results if r["status"] == "light_pass"]
    failed = [r for r in results if r["status"] == "failed"]

    tokens_light = sum(r.get("tokens_light") or 0 for r in results)
    tokens_full = sum(r.get("tokens_full") or 0 for r in results)
    n_full_ran = [r for r in results if (r.get("tokens_full") or 0) > 0]

    # ── 保存结果 ────────────────────────────────────────────────────────────
    out_dir = Path(args.output) / datetime.now().strftime("screen_%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "batch_time": datetime.now().isoformat(),
                "timeframe": timeframe,
                "bar_count": bar_count,
                "mode": args.mode,
                "light_only": args.light_only,
                "total": len(results),
                "opportunities": len(opportunities),
                "no_order": len(no_order),
                "screened_out": len(screened_out),
                "light_pass": len(light_pass),
                "failed": len(failed),
                "tokens_light": tokens_light,
                "tokens_full": tokens_full,
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
    print(f"批量筛选完成：{len(results)} 只 · 周期 {timeframe} · 模式 {args.mode}"
          f" · 耗时 {elapsed/60:.1f} 分钟")
    line2 = (f"有交易机会 {len(opportunities)} · 观望 {len(no_order)} · 失败 {len(failed)}")
    if args.mode == "light":
        line2 += f" · 初筛淘汰 {len(screened_out)}"
        if args.light_only:
            line2 += f" · 入围未深析 {len(light_pass)}"
    print(line2)
    print(f"token 用量：初筛 {tokens_light:,} + 完整分析 {tokens_full:,}"
          f" = {tokens_light + tokens_full:,}")
    if len(n_full_ran) >= 3:
        baseline = (tokens_full / len(n_full_ran)) * len(results)
        saved_pct = 100 * (baseline - tokens_light - tokens_full) / baseline
        print(f"估算节省：{saved_pct:.0f}%（全部直接完整分析约需 {baseline / 10000:.0f} 万 token）")
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

    if screened_out and args.mode == "light":
        print("\n初筛淘汰明细:")
        for r in screened_out:
            reason = (r.get("reasoning") or "")[:50]
            print(f"  {r['symbol']}: {reason}")

    print(f"\n完整结果: {out_dir / 'summary.json'}")
    print("每只股票的完整分析已存入 records/pending，GUI 切到该股可查看/增量分析")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
