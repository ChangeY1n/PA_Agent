"""批量筛选通达信自选股：跑两阶段 AI 分析并列出有交易机会的决策.

用法:
    python batch_watchlist_screen.py                 # 自选前 10 只试跑（轻量初筛模式）
    python batch_watchlist_screen.py --limit 50      # 跑前 50 只
    python batch_watchlist_screen.py --limit 0       # 全部自选
    python batch_watchlist_screen.py --mode full     # 跳过初筛，全部走完整两阶段
    python batch_watchlist_screen.py --light-only    # 只跑初筛不深析（校准/省钱巡视）
    python batch_watchlist_screen.py --timeframe 1d  # 指定周期（默认沿用设置中的上次周期）
    python batch_watchlist_screen.py --min-conviction 55  # 初筛信心分门槛（0=关）

说明:
- 股票列表直接读取通达信自选股（与 GUI 下拉列表同源，含 ↻ 自选 的重读逻辑）。
- 默认「轻量初筛」：每只先用一次约 0.8 万 token 的紧凑调用粗筛，通过的才跑
  完整两阶段（每只约 16 万 token）；初筛方向为空头的直接淘汰（A股无便利做空，
  历史全部出单均为做多；--allow-short 关闭该过滤）。
- --min-conviction：初筛虽通过但 conviction（0-100 信心分）低于门槛的也不深析
  ——2026-10-19 实测 96 只深析中约 40-50 只 conviction < 55 且几乎全为「不下单」。
- 当前涨停/跌停的股票直接跳过（连初筛也不跑）——停板价无法有效成交，深析
  出的单不可执行；规则按板块涨跌幅（主板10%、创业/科创20%、北交30%）。
- 每只进入完整分析的股票走与 GUI 完全相同的管线（build_display_frame +
  TwoStageOrchestrator），记录照常写入 records/pending，之后在 GUI 中切到
  该股即可增量分析。
- 「有交易机会」= 阶段二决策 order_type 不是「不下单」。
- 核心逻辑在 pa_agent/batch/screen_runner.py（与 GUI 批量窗口共用）。

GUI: 工具菜单 →「批量自选筛选」提供同一功能的窗口版（可编辑初筛提示词、
实时结果表、线程数选择）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pa_agent.batch.screen_runner import (
    STATUS_MARKS,
    analyze_one,
    write_screen_summary,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_screen")


def main() -> int:
    parser = argparse.ArgumentParser(description="批量筛选通达信自选股（两阶段 AI 分析）")
    parser.add_argument("--mode", choices=("light", "full"), default="light",
                        help="light=先轻量初筛再深析（默认，省 60-70%% token）；"
                             "full=全部直接完整两阶段")
    parser.add_argument("--light-only", action="store_true",
                        help="只跑轻量初筛、不跑完整分析（校准/省钱巡视用）")
    parser.add_argument("--light-bars", type=int, default=0,
                        help="初筛用 K 线根数（默认 80：前 60 摘要 + 近 20 明细）")
    parser.add_argument("--single-screen", action="store_true",
                        help="关闭双初筛共识，只跑 Brooks 主筛（默认开双筛，token ×2）")
    parser.add_argument("--allow-short", action="store_true",
                        help="初筛为空头方向的也进入深析（默认只做多：A股无便利做空，"
                             "历史全部出单均为做多）")
    parser.add_argument("--min-conviction", type=int, default=0,
                        help="初筛 conviction 门槛（0-100，0=关闭；低于门槛不深析省 token）")
    parser.add_argument("--limit", type=int, default=10,
                        help="取自选前 N 只（0 = 全部，默认 10）")
    parser.add_argument("--timeframe", default="",
                        help="分析周期（默认沿用 config/settings.json 的上次周期）")
    parser.add_argument("--workers", type=int, default=10,
                        help="并发线程数（默认 10；网关限流出现失败时调低）")
    parser.add_argument("--bars", type=int, default=0,
                        help="K 线根数（默认用设置中的 analysis_bar_count；批量可降到 60 省 token）")
    parser.add_argument("--no-local-gate", action="store_true",
                        help="禁用阶段一本地短路（默认开启：阶段一诊断无可用"
                             "策略文件时直接判观望，跳过阶段二）")
    parser.add_argument("--offset", type=int, default=0,
                        help="跳过自选前 N 只（配合 --limit 分批跑）")
    parser.add_argument("--symbols", default="",
                        help="只分析指定股票（逗号分隔，如 600048,600196；"
                             "优先于 --offset/--limit，用于重跑失败股）")
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
    if args.symbols:
        # 指定代码模式：优先于 --offset/--limit（用于重跑失败股）
        wanted = [s.strip() for s in args.symbols.split(",") if s.strip()]
        known = set(symbols)
        unknown = [s for s in wanted if s not in known]
        if unknown:
            logger.warning("不在自选中的代码已忽略: %s", ",".join(unknown))
        symbols = [s for s in wanted if s in known]
        if not symbols:
            logger.error("--symbols 指定的代码均不在自选中")
            return 1
    else:
        symbols = symbols[args.offset:]
        if args.limit > 0:
            symbols = symbols[: args.limit]
    if not symbols:
        logger.error("offset=%d 超出自选总数 %d", args.offset, total_watchlist)
        return 1

    logger.info(
        "自选共 %d 只，本次分析 %d 只（offset=%d）· 周期 %s · %d 根K线 · %d 并发 · "
        "模式 %s · 只做多 %s · conviction门槛 %d · 本地短路 %s · 双初筛 %s",
        total_watchlist, len(symbols), args.offset, timeframe,
        bar_count, args.workers, args.mode,
        "关" if args.allow_short else "开",
        args.min_conviction,
        "关" if args.no_local_gate else "开",
        "关" if args.single_screen else "开",
    )

    light_bars = args.light_bars if args.light_bars > 0 else 80

    results: list[dict] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="screen") as ex:
        futures = {
            ex.submit(analyze_one, s, timeframe, bar_count,
                      not args.no_local_gate,
                      mode=args.mode, light_only=args.light_only,
                      light_bars=light_bars,
                      long_only=not args.allow_short,
                      min_conviction=args.min_conviction,
                      dual_screen=not args.single_screen): s
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
            mark = STATUS_MARKS[r["status"]]
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
    limit_skips = [r for r in results if r["status"] == "limit_skip"]
    failed = [r for r in results if r["status"] == "failed"]

    tokens_light = sum(r.get("tokens_light") or 0 for r in results)
    tokens_full = sum(r.get("tokens_full") or 0 for r in results)
    n_full_ran = [r for r in results if (r.get("tokens_full") or 0) > 0]

    # ── 保存结果 ────────────────────────────────────────────────────────────
    out_dir = write_screen_summary(
        args.output,
        results,
        {
            "timeframe": timeframe,
            "bar_count": bar_count,
            "mode": args.mode,
            "light_only": args.light_only,
            "dual_screen": not args.single_screen,
            "min_conviction": args.min_conviction,
            "duration_seconds": round(elapsed, 1),
        },
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
    if limit_skips:
        line2 += f" · 停板跳过 {len(limit_skips)}"
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

    if limit_skips:
        print("\n停板跳过明细:")
        for r in limit_skips:
            print(f"  {r['symbol']}: {(r.get('reasoning') or '')}")

    print(f"\n完整结果: {out_dir / 'summary.json'}")
    print("每只股票的完整分析已存入 records/pending，GUI 切到该股可查看/增量分析")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
