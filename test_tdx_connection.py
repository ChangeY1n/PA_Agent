#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试通达信数据源连接"""
import sys
import io
from pathlib import Path

# 设置标准输出编码为 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from pa_agent.data.tdx_source import TDXSource


def test_tdx_connection():
    """测试通达信 MCP 服务连接和数据获取"""
    print("=" * 60)
    print("测试通达信数据源")
    print("=" * 60)

    # 创建数据源
    tdx = TDXSource()
    print("[OK] 创建 TDXSource 实例")

    # 连接
    try:
        tdx.connect()
        print("[OK] 连接成功")
    except Exception as e:
        print(f"[ERROR] 连接失败: {e}")
        return False

    # 获取支持的时间周期
    timeframes = tdx.supported_timeframes()
    print(f"[OK] 支持的时间周期: {timeframes}")

    # 获取支持的股票列表
    symbols = tdx.list_symbols()
    print(f"[OK] 预设股票列表: {symbols}")

    # 测试订阅和获取数据
    test_symbol = "000001"  # 平安银行
    test_timeframe = "1d"   # 日线

    try:
        tdx.subscribe(test_symbol, test_timeframe)
        print(f"[OK] 订阅成功: {test_symbol} @ {test_timeframe}")
    except Exception as e:
        print(f"[ERROR] 订阅失败: {e}")
        tdx.disconnect()
        return False

    # 获取 K 线数据
    try:
        bars = tdx.latest_snapshot(10)
        print(f"[OK] 获取到 {len(bars)} 根 K 线")

        if bars:
            print("\n最新 3 根 K 线:")
            for i, bar in enumerate(bars[:3]):
                from datetime import datetime
                dt = datetime.fromtimestamp(bar.ts_open / 1000)
                status = "形成中" if not bar.closed else "已完成"
                print(f"  [{i+1}] {dt.strftime('%Y-%m-%d %H:%M')} "
                      f"O:{bar.open:.2f} H:{bar.high:.2f} "
                      f"L:{bar.low:.2f} C:{bar.close:.2f} "
                      f"V:{bar.volume:.0f} ({status})")
    except Exception as e:
        print(f"[ERROR] 获取 K 线失败: {e}")
        import traceback
        traceback.print_exc()
        tdx.disconnect()
        return False

    # 断开连接
    tdx.disconnect()
    print("\n[OK] 测试完成，连接已断开")
    return True


if __name__ == "__main__":
    success = test_tdx_connection()
    sys.exit(0 if success else 1)
