#!/usr/bin/env python3
"""生成万宝工具箱小程序 icon（144x144 PNG，纯标准库渲染，超采样抗锯齿）。

设计（v2 · 对齐小程序视觉身份）：
- 底：靛蓝→紫罗兰对角渐变（#6366f1 → #8b5cf6，与 app.wxss --grad 一致）+ 两枚柔光光斑
- 主体：白色宝箱（万宝），带提手与金色锁扣
- 点缀：箱盖四色彩点（青/橙/玫红/绿，取自类目撞色）+ 金色 AI 四角星芒

输出：<MP_ASSETS>/icon.png，默认 ../miniprogram/assets（微信小程序头像要求 ≥144x144）
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

# ===== 配色（与 app.wxss 保持一致） =====
C_TOP = (99, 102, 241)      # #6366f1 靛蓝
C_BOT = (139, 92, 246)      # #8b5cf6 紫罗兰
WHITE = (255, 255, 255)
GOLD = (251, 191, 36)       # #fbbf24
GOLD_DEEP = (245, 158, 11)  # #f59e0b
CYAN = (6, 182, 212)        # #06b6d4
ORANGE = (249, 115, 22)     # #f97316
PINK = (236, 72, 153)       # #ec4899
GREEN = (16, 185, 129)      # #10b981

R_CANVAS = 34               # 圆角方形底


def in_rounded_rect(x, y, x0, y0, x1, y1, r):
    """点 (x,y) 是否落在圆角矩形内。"""
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    dx = max(x0 + r - x, x - (x1 - r), 0)
    dy = max(y0 + r - y, y - (y1 - r), 0)
    return dx * dx + dy * dy <= r * r


def in_circle(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_ring(x, y, cx, cy, r_out, r_in):
    d2 = (x - cx) ** 2 + (y - cy) ** 2
    return r_in * r_in <= d2 <= r_out * r_out


def in_spark(x, y, cx, cy, R, s=0.28, n=5):
    """AI 四角星芒：r(θ) = R * (s + (1-s)*|cos 2θ|^n)。"""
    dx, dy = x - cx, y - cy
    if dx * dx + dy * dy > R * R:
        return False
    th = math.atan2(dy, dx)
    rr = R * (s + (1 - s) * abs(math.cos(2 * th)) ** n)
    return math.hypot(dx, dy) <= rr


def sample(x, y):
    """返回超采样坐标系中该点的 RGBA。"""
    # ===== 1. 画布外：透明 =====
    if not in_rounded_rect(x, y, 0, 0, SIZE, SIZE, R_CANVAS):
        return (0, 0, 0, 0)

    # ===== 2. 背景：对角渐变 =====
    t = (x + y) / (2 * SIZE)
    col = tuple(C_TOP[i] + (C_BOT[i] - C_TOP[i]) * t for i in range(3)) + (255,)

    # ===== 3. 柔光光斑（与小程序 Hero 的光斑呼应） =====
    for (bx, by, br, a) in ((108, 22, 46, 0.20), (26, 122, 40, 0.13)):
        d = math.hypot(x - bx, y - by)
        if d < br:
            k = (1 - d / br) * a
            col = tuple(
                col[i] + (255 - col[i]) * k for i in range(3)
            ) + (255,)

    # ===== 4. 宝箱提手（先画，被箱盖压住下沿） =====
    if in_ring(x, y, 72, 44, 16, 9) and y <= 48:
        col = (*WHITE, 255)

    # ===== 5. 箱体 =====
    if in_rounded_rect(x, y, 32, 66, 112, 116, 12):
        col = (*WHITE, 255)

    # ===== 6. 箱盖（略宽于箱体，形成层次） =====
    if in_rounded_rect(x, y, 26, 48, 118, 72, 10):
        col = (*WHITE, 255)
    # 盖体分隔阴影线，增加立体感
    if in_rounded_rect(x, y, 32, 70, 112, 74, 2):
        col = (219, 213, 250, 255)

    # ===== 7. 箱盖上的四色彩点（类目撞色） =====
    for (cx, color) in ((44, CYAN), (62, ORANGE), (80, PINK), (98, GREEN)):
        if in_circle(x, y, cx, 60, 5.4):
            col = (*color, 255)

    # ===== 8. 金色锁扣 =====
    if in_rounded_rect(x, y, 66, 66, 78, 82, 4):
        col = (*GOLD, 255)
    if in_circle(x, y, 72, 74, 2.6):
        col = (*GOLD_DEEP, 255)

    # ===== 9. 主星芒（右上，跨越箱体） =====
    if in_spark(x, y, 110, 30, 21):
        col = (*GOLD, 255)
    if in_spark(x, y, 110, 30, 8):
        col = (*WHITE, 255)
    # 副星芒（左上，小）
    if in_spark(x, y, 26, 34, 12, s=0.3):
        col = (*WHITE, 255)

    return col


def render():
    rows = []
    n = SS * SS
    for oy in range(SIZE):
        row = bytearray()
        for ox in range(SIZE):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = sample(ox + sx / SS, oy + sy / SS)
                    for i in range(4):
                        acc[i] += c[i]
            row += bytes(round(v / n) for v in acc)
        rows.append(bytes(row))
    return rows


def to_png(rows):
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
    return png


def preview(rows, cols=48, rows_out=24):
    """终端 ASCII 预览，便于无图像环境下核对构图。"""
    legend = {
        (255, 255, 255): "#",
        (251, 191, 36): "*",
        (6, 182, 212): "c",
        (249, 115, 22): "o",
        (236, 72, 153): "p",
        (16, 185, 129): "g",
    }
    step_x = SIZE / cols
    step_y = SIZE / rows_out
    out = []
    for ry in range(rows_out):
        line = ""
        for rx in range(cols):
            x = int(rx * step_x + step_x / 2)
            y = int(ry * step_y + step_y / 2)
            px = rows[y][x * 4 : x * 4 + 4]
            if px[3] < 128:
                line += " "
                continue
            key = min(legend.keys(), key=lambda k: sum((k[i] - px[i]) ** 2 for i in range(3)))
            dist = sum((key[i] - px[i]) ** 2 for i in range(3)) ** 0.5
            line += legend[key] if dist < 60 else ("#" if px[0] > 200 else "+")
        out.append(line)
    return "\n".join(out)


def main():
    rows = render()
    png = to_png(rows)
    out = os.path.join(ASSETS_DIR, "icon.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(png)
    print(preview(rows))
    print(f"written {out} ({SIZE}x{SIZE}, {len(png)} bytes)")


if __name__ == "__main__":
    main()
