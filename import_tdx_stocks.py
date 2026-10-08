"""从通达信读取自选股列表（改进版）."""
from pathlib import Path


def read_tdx_block_file(file_path: str) -> list[str]:
    """读取通达信 .blk 文件中的股票代码.

    通达信的 .blk 文件是文本格式，每行一个股票代码（7位数字）。

    Args:
        file_path: .blk 文件路径

    Returns:
        股票代码列表
    """
    path = Path(file_path)

    if not path.exists():
        print(f"文件不存在: {path}")
        return []

    codes = []

    try:
        # 以 GBK 编码读取
        content = path.read_text(encoding='gbk', errors='ignore')

        for line in content.splitlines():
            line = line.strip()

            # 跳过空行
            if not line:
                continue

            # 跳过第一行的版本号（如 1999999）
            if line == '1999999':
                continue

            # 检查是否是7位数字（市场代码1位 + 股票代码6位）
            if line.isdigit() and len(line) == 7:
                market_code = line[0]
                stock_code = line[1:]

                # 根据市场代码添加前缀
                if market_code == '0':  # 深圳
                    codes.append(f"sz{stock_code}")
                elif market_code == '1':  # 上海
                    codes.append(f"sh{stock_code}")
                else:
                    codes.append(stock_code)
            elif line.isdigit() and len(line) == 6:
                # 纯6位代码
                codes.append(line)

    except Exception as e:
        print(f"读取文件失败: {e}")
        return []

    return codes


def export_tdx_stocks_to_file(
    tdx_path: str = "D:/app2/tdx",
    output_file: str = "my_stocks.txt"
):
    """从通达信目录导出自选股到文本文件.

    Args:
        tdx_path: 通达信安装目录
        output_file: 输出文件路径
    """
    tdx_dir = Path(tdx_path)

    # 查找自选股文件
    zxg_file = tdx_dir / "T0002" / "blocknew" / "zxg.blk"

    if not zxg_file.exists():
        print(f"未找到自选股文件: {zxg_file}")
        print("请检查通达信安装路径是否正确")
        return

    print(f"正在读取: {zxg_file}")
    codes = read_tdx_block_file(str(zxg_file))

    if not codes:
        print("未读取到任何股票代码")
        return

    print(f"读取到 {len(codes)} 只股票")

    # 写入文件
    output_path = Path(output_file)

    lines = [
        "# 从通达信导入的自选股列表",
        f"# 共 {len(codes)} 只",
        "",
    ]

    lines.extend(codes)

    output_path.write_text('\n'.join(lines), encoding='utf-8')

    print(f"\n已导出到: {output_path}")
    print("\n股票列表预览:")
    for i, code in enumerate(codes[:20], 1):
        print(f"  {i}. {code}")

    if len(codes) > 20:
        print(f"  ... 还有 {len(codes) - 20} 只")


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1:
        tdx_path = sys.argv[1]
    else:
        tdx_path = "D:/app2/tdx"

    export_tdx_stocks_to_file(tdx_path, "my_stocks.txt")
