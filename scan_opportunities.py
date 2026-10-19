"""扫描批量分析结果: 统计 outcome 分布, 列出 outcome=trade 的机会票."""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
CUTOFF = "20261014_1226"          # 本次全量跑批开始时间(跳过之前的小样本测试)


def main() -> None:
    out = []

    # 进度: 日志里 WARNING 级别的 OK/FAIL 行(configure_logging 把 INFO 压掉了)
    log = ROOT / "dual_full.log"
    done = ok = 0
    cur_profile = ""
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "批次:" in line:
                cur_profile = line.split("批次:")[1].split("|")[0].strip()
            m = re.search(r"\[(\d+)/(\d+)\] (OK|FAIL)", line)
            if m:
                done = int(m.group(1))
                ok += 1 if m.group(3) == "OK" else 0
    out.append(f"日志进度: 最新 [{done}] (其中OK {ok}) | 当前批次: {cur_profile}")
    out.append("=" * 70)

    stats: dict[tuple[str, str], int] = {}
    trades = []
    n_files = 0
    for d in ("batch_analysis_results/gmini", "batch_analysis_results/ds"):
        p = ROOT / d
        if not p.exists():
            continue
        for f in sorted(p.glob("*_1h_*.json")):
            if "summary" in f.name or "worthy" in f.name:
                continue
            ts = "_".join(f.stem.split("_")[-2:])   # 20261014_122707
            if ts < CUTOFF:
                continue
            try:
                rec = json_loads(f)
            except Exception:
                continue
            n_files += 1
            d2 = rec.get("stage2_decision") or {}
            t = d2.get("terminal") or {}
            o = t.get("outcome", "(no-stage2)")
            prof = (rec.get("meta") or {}).get("provider_profile", "?")
            stats[(prof, o)] = stats.get((prof, o), 0) + 1
            if o == "trade":
                od = d2.get("decision") or {}
                trades.append((
                    f.stem.split("_")[0], prof,
                    od.get("order_direction"), od.get("order_type"),
                    od.get("entry_price"), od.get("stop_loss_price"),
                    od.get("take_profit_price"), od.get("trade_confidence"),
                    (t.get("label") or "")[:40],
                ))

    out.append(f"本次已产出结果文件: {n_files} 个")
    out.append("profile x outcome 统计:")
    for k in sorted(stats):
        out.append(f"  {k[0]:8s} {k[1]:12s} {stats[k]}")
    out.append("=" * 70)
    out.append(f"outcome=trade 共 {len(trades)} 只:")
    for t in sorted(trades, key=lambda x: -(x[7] or 0)):
        out.append(
            f"  {t[0]:8s} [{t[1]}] {t[2]} {t[3]} "
            f"入场:{t[4]} 止损:{t[5]} 目标:{t[6]} 置信:{t[7]} | {t[8]}"
        )

    text = "\n".join(out)
    (ROOT / "opportunities_check.txt").write_text(text, encoding="utf-8")
    print(text)


def json_loads(f: Path):
    import json
    return json.loads(f.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
