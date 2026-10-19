#!/usr/bin/env python3
"""生成小程序**分享卡片图**（5:4，纯标准库渲染，超采样抗锯齿）。

用途：`onShareAppMessage({ imageUrl })` —— 不指定时微信会把"当前页截图"当卡片图，
从邀请页分享会露出邀请码，观感也差。这里生成一张统一的品牌图。

设计（与 icon.png / app.wxss 同一视觉身份）：
- 底：靛蓝→紫罗兰对角渐变（#6366f1 → #8b5cf6）+ 柔光光斑
- 主体：白色宝箱（万宝）+ 四色彩点 + 金色 AI 星芒（按 144 设计稿等比放大居中）
- 无文字（纯标准库无法排版中文），靠构图与配色表达品牌

输出：<MP_ASSETS>/invite-share.png，默认 ../miniprogram/assets（800x640 = 5:4）
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

W, H = 800, 640          # 5:4
SS = 2                   # 超采样倍数

BOX = 144                # 复用 icon 的 144x144 设计稿坐标系
SCALE = 3.9              # 设计稿放大倍数
OX = (W - BOX * SCALE) / 2
OY = (H - BOX * SCALE) / 2 - 6

# ===== 配色（与 app.wxss / gen_icon.py 保持一致） =====
C_TOP = (99, 102, 241)      # #6366f1 靛蓝
C_BOT = (139, 92, 246)      # #8b5cf6 紫罗兰
WHITE = (255, 255, 255)
GOLD = (251, 191, 36)       # #fbbf24
GOLD_DEEP = (245, 158, 11)  # #f59e0b
CYAN = (6, 182, 212)
ORANGE = (249, 115, 22)
PINK = (236, 72, 153)
GREEN = (16, 185, 129)


# ---------- 基础几何 ----------
def in_rounded_rect(x, y, x0, y0, x1, y1, r):
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
    """n 角星芒（超椭圆），用于 AI 点缀。"""
    dx, dy = x - cx, y - cy
    d = math.hypot(dx, dy)
    if d > R:
        return False
    if d == 0:
        return True
    a = math.atan2(dy, dx)
    edge = R * (1 - s * abs(math.cos(n * a / 2)))
    return d <= edge


# ---------- 背景 ----------
def background(x, y):
    t = (x / W + y / H) / 2
    col = tuple(C_TOP[i] + (C_BOT[i] - C_TOP[i]) * t for i in range(3))
    # 柔光光斑
    for (bx, by, br, a) in ((W * 0.82, H * 0.14, H * 0.52, 0.16),
                            (W * 0.16, H * 0.88, H * 0.46, 0.12),
                            (W * 0.5, H * 0.42, H * 0.62, 0.07)):
        d = math.hypot(x - bx, y - by)
        if d < br:
            k = (1 - d / br) * a
            col = tuple(col[i] + (255 - col[i]) * k for i in range(3))
    return col


# ---------- 主体（复用 icon 的 144 设计稿） ----------
def artwork(dx, dy):
    """返回设计稿坐标 (dx,dy) 处的主体颜色；None = 透明（露出背景）。"""
    # 提手
    if in_ring(dx, dy, 72, 44, 16, 9) and dy <= 48:
        return WHITE
    # 箱体
    if in_rounded_rect(dx, dy, 32, 66, 112, 116, 12):
        return WHITE
    # 箱盖
    if in_rounded_rect(dx, dy, 26, 48, 118, 72, 10):
        return WHITE
    # 盖体分隔阴影线
    if in_rounded_rect(dx, dy, 32, 70, 112, 74, 2):
        return (219, 213, 250)
    # 箱盖四色彩点
    for (cx, color) in ((44, CYAN), (62, ORANGE), (80, PINK), (98, GREEN)):
        if in_circle(dx, dy, cx, 60, 5.4):
            return color
    # 金色锁扣
    if in_rounded_rect(dx, dy, 66, 66, 78, 82, 4):
        return GOLD
    if in_circle(dx, dy, 72, 74, 2.6):
        return GOLD_DEEP
    # 主星芒（右上）
    if in_spark(dx, dy, 110, 30, 21):
        return GOLD
    if in_spark(dx, dy, 110, 30, 8):
        return WHITE
    # 副星芒（左上）
    if in_spark(dx, dy, 26, 34, 12, s=0.3):
        return WHITE
    return None


def sample(x, y):
    """超采样坐标系中某点的 RGBA。"""
    col = background(x, y)

    # 主体投影：主体下方一层柔和暗影，制造悬浮感
    dx = (x - OX) / SCALE
    dy = (y - OY) / SCALE
    if 20 <= dx <= 124 and 40 <= dy <= 124:
        sd = math.hypot((dx - 72) / 52, (dy - 118) / 14)
        if sd < 1:
            k = (1 - sd) * 0.18
            col = tuple(col[i] * (1 - k) for i in range(3))

    if 0 <= dx < BOX and 0 <= dy < BOX:
        art = artwork(dx, dy)
        if art is not None:
            col = art

    # 画面四角各加一枚小星芒，填补留白
    for (sx, sy, sr) in ((W - 92, H - 84, 17), (96, 92, 13), (W - 150, H - 190, 9), (140, H - 120, 9)):
        if in_spark(x, y, sx, sy, sr, s=0.34):
            col = WHITE if sr < 12 else GOLD
    return (*col, 255)


def render():
    rows = []
    n = SS * SS
    for oy in range(H):
        row = bytearray()
        for ox in range(W):
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
        (b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)),
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


def preview(rows, cols=64, rows_out=24):
    """终端 ASCII 预览，便于无图像环境下核对构图。"""
    legend = {WHITE: "#", GOLD: "*", CYAN: "c", ORANGE: "o", PINK: "p", GREEN: "g"}
    step_x = W / cols
    step_y = H / rows_out
    out = []
    for ry in range(rows_out):
        line = ""
        for rx in range(cols):
            x = int(rx * step_x + step_x / 2)
            y = int(ry * step_y + step_y / 2)
            px = rows[y][x * 4: x * 4 + 4]
            key = min(legend.keys(), key=lambda k: sum((k[i] - px[i]) ** 2 for i in range(3)))
            dist = sum((key[i] - px[i]) ** 2 for i in range(3)) ** 0.5
            line += legend[key] if dist < 60 else ("." if px[0] > 200 else ":" if px[0] > 120 else " ")
        out.append(line)
    return "\n".join(out)


def main():
    rows = render()
    png = to_png(rows)
    out = os.path.join(ASSETS_DIR, "invite-share.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(png)
    print(preview(rows))
    print(f"written {out} ({W}x{H}, {len(png)} bytes)")


if __name__ == "__main__":
    main()
