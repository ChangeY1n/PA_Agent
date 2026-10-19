"""批量分析通达信自选股 — 多线程 + 多 Provider 版.

用法示例:
  # 前 150 只用 Gemini (provider profile "gmini")
  python batch_ai_analyze_tdx.py --profile gmini --limit 150

  # 其余全部用 DeepSeek 官方 (provider profile "ds")
  python batch_ai_analyze_tdx.py --profile ds --skip 150

  # 一条命令按顺序跑完两批
  python batch_ai_analyze_tdx.py --dual gmini:150 ds:rest

分析流程与 GUI 完全一致: TDX K线 → build_analysis_frame → TwoStageOrchestrator
(Stage1 诊断 + Stage2 决策), 并记录 CSV/Excel 决策日志与 pending 记录。
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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_tdx")

PROJECT_ROOT = Path(__file__).parent

# 每只股票送入分析的 K 线根数(与 GUI 默认 analysis_bar_count 一致)
ANALYSIS_BARS = 100
# 额外多取的缓冲根数(含形成中K线 + 指标预热余量由 build_analysis_frame 内部处理)
FETCH_BARS = 170
# 单只股票失败后的重试次数(瞬态网络/限流错误)
RETRIES_PER_STOCK = 1
RETRY_WAIT_S = 3.0


# ── 自选股列表 ────────────────────────────────────────────────────────────────

def load_watchlist(fallback_file: str = "my_stocks.txt") -> tuple[list[str], str]:
    """优先实时读取通达信自选股(zxg.blk), 失败则回退到文本文件."""
    try:
        from pa_agent.config.settings import load_settings
        from pa_agent.config.paths import SETTINGS_JSON_PATH
        from pa_agent.data.tdx_source import TDXSource

        settings = load_settings(SETTINGS_JSON_PATH)
        src = TDXSource()
        src.set_tdx_dir(getattr(settings.general, "tdx_install_dir", "") or None)
        symbols = src.watchlist_symbols()
        if symbols:
            return symbols, "通达信自选股(zxg.blk)"
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取通达信自选股失败(%s), 回退到 %s", exc, fallback_file)

    path = PROJECT_ROOT / fallback_file
    codes = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                codes.append(line.split()[0])
    return codes, f"文本文件 {fallback_file}"


# ── 上下文构建(复刻 AppContext.bootstrap, 但允许替换 provider profile) ───────

def build_shared_context(profile_name: str):
    """按 provider profile 构建一份可跨线程共享的分析组件."""
    from pa_agent.config.paths import (
        SETTINGS_JSON_PATH,
        RECORDS_PENDING_DIR,
        EXPERIENCE_DIR,
        PROMPT_DIR,
    )
    from pa_agent.config.settings import load_settings
    from pa_agent.util.logging import configure_logging
    from pa_agent.util.event_bus import EventBus
    from pa_agent.ai.deepseek_client import DeepSeekClient
    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.ai.router import route_strategy_files
    from pa_agent.ai.json_validator import JsonValidator
    from pa_agent.records.pending_writer import PendingWriter
    from pa_agent.records.experience_reader import ExperienceReader
    from pa_agent.records.decision_csv_logger import DecisionCSVLogger
    from pa_agent.records.decision_excel_logger import DecisionExcelLogger

    settings = load_settings(SETTINGS_JSON_PATH)

    profiles = getattr(settings, "provider_profiles", None) or {}
    if profile_name == "active":
        profile_name = str(getattr(settings, "active_provider_name", "") or "")
    if profile_name and profile_name in profiles:
        settings.provider = profiles[profile_name]
    else:
        # 未指定或名字不存在 → 用当前生效 provider(与 GUI 一致)
        profile_name = f"当前provider({settings.provider.model})"

    configure_logging(api_key=settings.provider.api_key)

    app_logger = logging.getLogger("pa_agent")
    event_bus = EventBus()
    client = DeepSeekClient(settings=settings.provider, logger_=app_logger)
    exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=app_logger)
    assembler = PromptAssembler(
        prompt_dir=PROMPT_DIR,
        experience_reader=exp_reader,
        prompt_settings=settings.prompt,
    )
    validator = JsonValidator(settings)
    pending_writer = PendingWriter(
        pending_dir=RECORDS_PENDING_DIR,
        event_bus=event_bus,
        api_key=settings.provider.api_key,
    )
    csv_logger = DecisionCSVLogger(
        csv_path=getattr(settings.general, "decision_csv_path", "records/trading_decisions.csv"),
        enabled=getattr(settings.general, "enable_decision_csv_logging", True),
    )
    excel_logger = DecisionExcelLogger(
        xlsx_path=getattr(settings.general, "decision_excel_path", "records/trading_decisions.xlsx"),
        enabled=getattr(settings.general, "enable_decision_excel_logging", True),
    )

    return {
        "profile": profile_name,
        "settings": settings,
        "client": client,
        "assembler": assembler,
        "router": route_strategy_files,
        "validator": validator,
        "pending_writer": pending_writer,
        "exp_reader": exp_reader,
        "csv_logger": csv_logger,
        "excel_logger": excel_logger,
    }


# ── 每线程资源(复用 TDX 长连接) ──────────────────────────────────────────────

_tls = threading.local()


def _thread_tdx():
    """返回当前线程复用的 TDXSource(长连接, 避免每只股票重新握手)."""
    src = getattr(_tls, "tdx", None)
    if src is None:
        from pa_agent.data.factory import create_data_source

        src = create_data_source("tdx")
        src.connect()
        _tls.tdx = src
    return src


# ── 单只股票分析 ──────────────────────────────────────────────────────────────

def analyze_one_stock(symbol: str, timeframe: str, ctx: dict, output_dir: Path) -> dict:
    """分析单只股票: 取K线 → 正式分析帧 → 两阶段编排 → 保存结果."""
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.data.snapshot import build_analysis_frame
    from pa_agent.util.threading import CancelToken

    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "success": False,
        "error": None,
        "file": None,
        "decision": None,
    }

    tdx = _thread_tdx()
    bars = None
    last_err: Exception | None = None

    for attempt in range(1 + RETRIES_PER_STOCK):
        try:
            tdx.subscribe(symbol, timeframe)
            bars = tdx.latest_snapshot(FETCH_BARS)
            if bars:
                break
            last_err = ValueError("未返回K线数据")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            tdx.disconnect()          # 丢弃可能坏掉的长连接, 下次重建
            _tls.tdx = None
        if attempt < RETRIES_PER_STOCK:
            time.sleep(RETRY_WAIT_S)

    if not bars:
        result["error"] = f"K线获取失败: {last_err}"
        return result

    # 与 GUI 完全一致的分析帧构建(丢弃形成中K线 + 指标预热)
    frame = build_analysis_frame(list(bars), ANALYSIS_BARS, symbol, timeframe)
    if frame is None:
        result["error"] = f"可用已收盘K线不足 {ANALYSIS_BARS} 根"
        return result

    orchestrator = TwoStageOrchestrator(
        client=ctx["client"],
        assembler=ctx["assembler"],
        router=ctx["router"],
        validator=ctx["validator"],
        pending_writer=ctx["pending_writer"],
        exp_reader=ctx["exp_reader"],
        settings=ctx["settings"],
        csv_logger=ctx["csv_logger"],
        excel_logger=ctx["excel_logger"],
    )
    record = orchestrator.submit(frame=frame, cancel_token=CancelToken(), on_event=lambda evt: None)

    if not record.stage2_decision:
        exc_info = record.exception
        result["error"] = f"未返回决策: {exc_info}" if exc_info else "未返回决策(阶段一/二校验失败)"
        return result

    decision = record.stage2_decision
    terminal = decision.get("terminal") or {}
    order = decision.get("decision") or {}
    result["success"] = True
    result["decision"] = {
        "outcome": terminal.get("outcome"),
        "node_id": terminal.get("node_id"),
        "label": terminal.get("label"),
        "order_direction": order.get("order_direction"),
        "order_type": order.get("order_type"),
        "entry_price": order.get("entry_price"),
        "stop_loss_price": order.get("stop_loss_price"),
        "take_profit_price": order.get("take_profit_price"),
        "trade_confidence": order.get("trade_confidence"),
        "estimated_win_rate": order.get("estimated_win_rate"),
    }

    record_dict = {
        "meta": {
            "symbol": getattr(record.meta, "symbol", symbol),
            "timeframe": getattr(record.meta, "timeframe", timeframe),
            "timestamp": getattr(record.meta, "timestamp", None),
            "provider_profile": ctx["profile"],
            "model": ctx["settings"].provider.model,
        },
        "stage1_diagnosis": record.stage1_diagnosis,
        "stage2_decision": record.stage2_decision,
        "strategy_files_used": record.strategy_files_used,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = output_dir / f"{symbol}_{timeframe}_{ts}.json"
    file_path.write_text(json.dumps(record_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    result["file"] = str(file_path)
    return result


# ── 批量执行 ──────────────────────────────────────────────────────────────────

def run_batch(
    profile_name: str,
    symbols: list[str],
    timeframe: str,
    output_root: str,
    max_workers: int,
) -> list[dict]:
    ctx = build_shared_context(profile_name)
    output_dir = Path(output_root) / str(ctx["profile"]).replace("(", "_").replace(")", "")

    logger.info("=" * 62)
    logger.info("批次: %s | 模型: %s | %s", ctx["profile"], ctx["settings"].provider.model,
                ctx["settings"].provider.base_url)
    logger.info("股票数: %d | 周期: %s | 并发: %d", len(symbols), timeframe, max_workers)
    logger.info("结果目录: %s", output_dir)
    logger.info("=" * 62)

    results: list[dict] = []
    done = 0
    ok = 0
    t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="an") as pool:
        futures = {pool.submit(analyze_one_stock, s, timeframe, ctx, output_dir): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                res = {"symbol": sym, "success": False, "error": f"线程异常: {exc}", "decision": None}
            results.append(res)
            done += 1
            if res["success"]:
                ok += 1
                d = res["decision"] or {}
                op = d.get("outcome") or "?"
                if d.get("order_direction"):
                    op = f"{op}/{d['order_direction']}"
                logger.info("[%d/%d] OK  %s -> %s", done, len(symbols), sym, op)
            else:
                logger.warning("[%d/%d] FAIL %s : %s", done, len(symbols), sym, res["error"])
            if done % 10 == 0 or done == len(symbols):
                elapsed = time.monotonic() - t0
                eta = elapsed / done * (len(symbols) - done)
                logger.info("进度 %d/%d (成功%d) 用时%.1f分 预计剩余%.1f分",
                            done, len(symbols), ok, elapsed / 60, eta / 60)

    minutes = (time.monotonic() - t0) / 60
    summary = {
        "batch_time": datetime.now().isoformat(),
        "provider_profile": ctx["profile"],
        "model": ctx["settings"].provider.model,
        "timeframe": timeframe,
        "total": len(symbols),
        "success": ok,
        "failed": len(symbols) - ok,
        "duration_minutes": round(minutes, 2),
        "max_workers": max_workers,
        "results": results,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    export_worthy_setups(results, output_dir)

    logger.info("批次完成: %s 成功 %d / %d, 耗时 %.1f 分钟, 汇总: %s",
                ctx["profile"], ok, len(symbols), minutes, summary_path)
    return results


# ── 值得参与的 Setup 导出 ─────────────────────────────────────────────────────

def export_worthy_setups(results: list[dict], output_dir: Path) -> None:
    """筛选 terminal.outcome=trade(明确给出下单建议)的股票并导出."""
    worthy = []
    for r in results:
        if not r["success"]:
            continue
        d = r.get("decision") or {}
        if d.get("outcome") != "trade":
            continue
        worthy.append({
            "股票代码": r["symbol"],
            "周期": r.get("timeframe"),
            "方向": d.get("order_direction"),
            "订单类型": d.get("order_type"),
            "入场价": d.get("entry_price"),
            "止损价": d.get("stop_loss_price"),
            "目标价": d.get("take_profit_price"),
            "交易置信度": d.get("trade_confidence"),
            "预估胜率": d.get("estimated_win_rate"),
            "决策节点": d.get("node_id"),
            "决策摘要": d.get("label"),
        })

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (output_dir / f"worthy_setups_{stamp}.json").write_text(
        json.dumps(worthy, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"值得参与的 Setup 列表 (outcome=trade)  共 {len(worthy)} 个", "=" * 70, ""]
    for i, s in enumerate(worthy, 1):
        lines.append(f"{i}. {s['股票代码']} [{s['周期']}] {s['方向']} {s['订单类型']} "
                     f"置信:{s['交易置信度']} 胜率:{s['预估胜率']}")
        lines.append(f"   入场: {s['入场价']}   止损: {s['止损价']}   目标: {s['目标价']}")
        lines.append(f"   节点 {s['决策节点']}: {s['决策摘要']}")
        lines.append("")
    (output_dir / f"worthy_setups_{stamp}.txt").write_text("\n".join(lines), encoding="utf-8")

    logger.info("值得参与的 setup (trade): %d 个 (worthy_setups_%s.json/txt)", len(worthy), stamp)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="批量 AI 分析通达信自选股(多线程/多Provider)")
    parser.add_argument("--profile", default="active",
                        help="settings.json provider_profiles 中的方案名(gmini/ds/莫奈/默认); 默认=当前激活方案")
    parser.add_argument("--timeframe", default="1h", choices=["1m", "5m", "15m", "30m", "1h", "1d"])
    parser.add_argument("--limit", type=int, default=0, help="只分析前 N 只(0=全部)")
    parser.add_argument("--skip", type=int, default=0, help="跳过前 N 只")
    parser.add_argument("--workers", type=int, default=25, help="并发线程数(默认25)")
    parser.add_argument("--output", default="batch_analysis_results", help="结果根目录")
    parser.add_argument("--symbols", default="",
                        help="只分析指定股票: 逗号分隔代码, 或 @文件 (每行一代码); 提供@文件时跳过自选列表读取")
    parser.add_argument("--dual", nargs=2, metavar=("前段", "后段"),
                        help='一条命令跑两批, 例: --dual gmini:150 ds:rest')
    args = parser.parse_args()

    if args.symbols:
        spec = args.symbols
        if spec.startswith("@"):
            all_symbols = [
                ln.strip() for ln in (PROJECT_ROOT / spec[1:]).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
            src = f"指定文件 {spec[1:]}"
        else:
            all_symbols = [s.strip() for s in spec.split(",") if s.strip()]
            src = "命令行指定"
        logger.info("自选股来源: %s, 共 %d 只", src, len(all_symbols))
        if not all_symbols:
            logger.error("指定股票列表为空")
            sys.exit(1)
        # 指定股票时 --limit/--skip 不再适用, 直接单批跑
        run_batch(args.profile, all_symbols, args.timeframe, args.output, args.workers)
        return

    all_symbols, src = load_watchlist()
    if not all_symbols:
        logger.error("自选股列表为空")
        sys.exit(1)
    logger.info("自选股来源: %s, 共 %d 只", src, len(all_symbols))

    if args.dual:
        head_spec, tail_spec = args.dual
        head_profile, _, head_n = head_spec.partition(":")
        tail_profile, _, _ = tail_spec.partition(":")
        n = int(head_n) if head_n else 150
        head, tail = all_symbols[:n], all_symbols[n:]
        logger.info("双批模式: 前 %d 只 -> %s ; 其余 %d 只 -> %s", len(head), head_profile, len(tail), tail_profile)
        if head:
            run_batch(head_profile, head, args.timeframe, args.output, args.workers)
        if tail:
            run_batch(tail_profile, tail, args.timeframe, args.output, args.workers)
        return

    start = args.skip
    end = (start + args.limit) if args.limit > 0 else len(all_symbols)
    symbols = all_symbols[start:end]
    if not symbols:
        logger.error("切片为空 (skip=%d, limit=%d, 总数=%d)", args.skip, args.limit, len(all_symbols))
        sys.exit(1)
    run_batch(args.profile, symbols, args.timeframe, args.output, args.workers)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已中断")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        logger.error("程序异常: %s", exc, exc_info=True)
        sys.exit(1)
