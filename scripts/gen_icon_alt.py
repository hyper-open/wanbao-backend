#!/usr/bin/env python3
"""万宝工具箱 icon 备选方案 B：齿轮 + AI 星芒（144x144 PNG，纯标准库）。
设计：白色齿轮（工具）中心镂空，镂空处透出金色 AI 星芒——单个一体的徽章式图形。
"""
import math
import os
import struct
import zlib

# 小程序已拆分到独立仓库（与本仓同级：../miniprogram）。
# 本脚本原样保留在此，仅用于生成品牌资源；输出目录默认指向小程序仓的 assets，
# 可用环境变量 MP_ASSETS 覆盖，例如：
#   MP_ASSETS=/path/to/miniprogram/assets python3 scripts/gen_icon.py
ASSETS_DIR = os.environ.get("MP_ASSETS") or os.path.join("..", "miniprogram", "assets")

SIZE = 144
SS = 4  # 超采样倍数
W = SIZE * SS

C_TOP = (79, 70, 229)    # indigo-600
C_BOT = (124, 58, 237)   # violet-600
WHITE = (255, 255, 255)
GOLD = (251, 191, 36)    # amber-400

CX = CY = 72.0
TEETH = 8        # 齿数
R_BODY = 46      # 齿轮本体半径
R_TOOTH = 54     # 齿尖半径
R_HOLE = 28      # 中心镂空半径


def gear_r(theta):
    """齿轮极坐标半径：方齿轮廓。"""
    k = (theta * TEETH / (2 * math.pi)) % 2
    return R_TOOTH if k < 1 else R_BODY


def in_spark(x, y, R, s=0.32, n=5):
    """四角 AI 星芒。"""
    dx, dy = x - CX, y - CY
    if dx * dx + dy * dy > R * R:
        return False
    th = math.atan2(dy, dx)
    rr = R * (s + (1 - s) * abs(math.cos(2 * th)) ** n)
    return math.hypot(dx, dy) <= rr


def sample(px, py):
    x, y = px / SS, py / SS

    # 背景：对角渐变
    t = (x + y) / (2 * SIZE)
    col = (
        C_TOP[0] + (C_BOT[0] - C_TOP[0]) * t,
        C_TOP[1] + (C_BOT[1] - C_TOP[1]) * t,
        C_TOP[2] + (C_BOT[2] - C_TOP[2]) * t,
        255,
    )

    dx, dy = x - CX, y - CY
    d = math.hypot(dx, dy)
    # 白色齿轮（中心镂空）
    if d <= gear_r(math.atan2(dy, dx)):
        col = (*WHITE, 255)
    # 镂空中心：金色星芒 + 白色小核
    if d <= R_HOLE:
        if in_spark(x, y, 22):
            col = (*GOLD, 255)
        if (x - CX) ** 2 + (y - CY) ** 2 <= 5 ** 2:
            col = (*WHITE, 255)
    return col


def main():
    rows = []
    for oy in range(SIZE):
        row = bytearray()
        for ox in range(SIZE):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = sample(ox * SS + sx, oy * SS + sy)
                    for i in range(4):
                        acc[i] += c[i]
            n = SS * SS
            row += bytes(round(v / n) for v in acc)
        rows.append(bytes(row))

    raw = b"".join(b"\x00" + r for r in rows)
    png = b"\x89PNG\r\n\x1a\n"
    for chunk_type, data in [
        (b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)),
        (b"IDAT", zlib.compress(raw, 9)),
        (b"IEND", b""),
    ]:
        png += (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    out = os.path.join(ASSETS_DIR, "icon_alt.png")
    with open(out, "wb") as f:
        f.write(png)
    print(f"written {out} ({len(png)} bytes)")


if __name__ == "__main__":
    main()
