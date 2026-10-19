#!/usr/bin/env python3
"""万宝工具箱 icon 多方案生成（144x144 PNG，纯标准库）。

复用 gen_icon.py 的基元与渲染/编码函数，输出 5 款备选到 docs/icon-preview/，
每款打印 ASCII 预览便于快速比对。（小程序已拆到独立仓库：../miniprogram）

方案：
  A 宝箱 + 四色撞点      gen_icon.py（当前正式款，不在本脚本内）
  B 齿轮 + 星芒          工具感最强
  C 四宫格 + 中心星芒     「一个入口 N 个工具」的集合隐喻，最撞色
  D 灯泡 + 漂浮彩点       AI 灵感/创意感
  E 等距 3D 宝箱          立体感最强
  F 圆环徽章 + 星芒       最简洁，缩到小尺寸也清晰
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_icon as base  # noqa: E402

SIZE = base.SIZE
SS = base.SS
WHITE = base.WHITE
GOLD = base.GOLD
CYAN, ORANGE, PINK, GREEN = base.CYAN, base.ORANGE, base.PINK, base.GREEN
OUT_DIR = "docs/icon-preview"
# 当前正式款 icon 所在的小程序资源目录（小程序已拆到独立仓库，可用 MP_ASSETS 覆盖）
ASSETS_DIR = os.environ.get("MP_ASSETS") or os.path.join("..", "miniprogram", "assets")


# ===== 公共基元 =====
def bg(x, y):
    """统一背景：圆角方形 + 靛紫对角渐变 + 两枚柔光光斑。"""
    if not base.in_rounded_rect(x, y, 0, 0, SIZE, SIZE, base.R_CANVAS):
        return (0, 0, 0, 0)
    t = (x + y) / (2 * SIZE)
    col = tuple(base.C_TOP[i] + (base.C_BOT[i] - base.C_TOP[i]) * t for i in range(3)) + (255,)
    for (bx, by, br, a) in ((108, 22, 46, 0.20), (26, 122, 40, 0.13)):
        d = math.hypot(x - bx, y - by)
        if d < br:
            k = (1 - d / br) * a
            col = tuple(col[i] + (255 - col[i]) * k for i in range(3)) + (255,)
    return col


def in_poly(x, y, pts):
    """射线法判定点是否在多边形内。"""
    inside = False
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xi = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < xi:
                inside = not inside
    return inside


def render(fn):
    rows = []
    n = SS * SS
    for oy in range(SIZE):
        row = bytearray()
        for ox in range(SIZE):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = fn(ox + sx / SS, oy + sy / SS)
                    for i in range(4):
                        acc[i] += c[i]
            row += bytes(round(v / n) for v in acc)
        rows.append(bytes(row))
    return rows


# ===== B 齿轮 + 星芒 =====
GEAR_TEETH, R_BODY, R_TOOTH, R_HOLE = 8, 44, 53, 26


def _gear_r(theta):
    k = (theta * GEAR_TEETH / (2 * math.pi)) % 2
    return R_TOOTH if k < 1 else R_BODY


def v_gear(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    dx, dy = x - 72, y - 72
    d = math.hypot(dx, dy)
    if d <= R_HOLE:
        col = bg(x, y)                      # 中心镂空透出渐变
    elif d <= _gear_r(math.atan2(dy, dx)):
        col = (*WHITE, 255)
    if base.in_spark(x, y, 72, 72, 23):
        col = (*GOLD, 255)
    if base.in_spark(x, y, 72, 72, 10):
        col = (*WHITE, 255)
    return col


# ===== C 四宫格 + 中心星芒 =====
GRID = [((20, 20), CYAN), ((78, 20), ORANGE), ((20, 78), PINK), ((78, 78), GREEN)]


def v_grid(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    for (sx, sy), c in GRID:
        if base.in_rounded_rect(x, y, sx, sy, sx + 46, sy + 46, 14):
            col = (*c, 255)
    if base.in_spark(x, y, 72, 72, 27):
        col = (*GOLD, 255)
    if base.in_spark(x, y, 72, 72, 12):
        col = (*WHITE, 255)
    return col


# ===== D 灯泡 + 漂浮彩点 =====
def v_bulb(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    if base.in_circle(x, y, 70, 58, 32):
        col = (*WHITE, 255)
    if base.in_rounded_rect(x, y, 56, 86, 84, 104, 6):
        col = (*WHITE, 255)
    if base.in_rounded_rect(x, y, 61, 104, 79, 110, 4):
        col = (*WHITE, 255)
    if base.in_spark(x, y, 70, 58, 20):
        col = (*GOLD, 255)
    if base.in_spark(x, y, 70, 58, 9):
        col = (*WHITE, 255)
    for (cx, cy, c) in ((108, 30, CYAN), (122, 50, ORANGE), (110, 68, PINK)):
        if base.in_circle(x, y, cx, cy, 5):
            col = (*c, 255)
    return col


# ===== E 等距 3D 宝箱 =====
TOP_FACE = [(72, 26), (118, 52), (72, 78), (26, 52)]
LEFT_FACE = [(26, 52), (72, 78), (72, 126), (26, 100)]
RIGHT_FACE = [(118, 52), (72, 78), (72, 126), (118, 100)]
# 盖体接缝（横跨两个正面）
SEAM_L = [(26, 72), (72, 98), (72, 105), (26, 79)]
SEAM_R = [(118, 72), (72, 98), (72, 105), (118, 79)]


def v_cube(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    if in_poly(x, y, LEFT_FACE):
        col = (246, 244, 255, 255)
    if in_poly(x, y, RIGHT_FACE):
        col = (206, 197, 248, 255)
    if in_poly(x, y, TOP_FACE):
        col = (*WHITE, 255)
    # 盖体接缝
    if in_poly(x, y, SEAM_L) or in_poly(x, y, SEAM_R):
        col = (186, 176, 240, 255)
    # 金色锁扣（正面中央）
    latch = [(46, 96), (58, 103), (46, 110), (34, 103)]
    if in_poly(x, y, latch):
        col = (*GOLD, 255)
    if in_poly(x, y, [(46, 100), (52, 103), (46, 106), (40, 103)]):
        col = (base.GOLD_DEEP[0], base.GOLD_DEEP[1], base.GOLD_DEEP[2], 255)
    # 顶面彩点
    for (cx, cy, c) in ((58, 50, CYAN), (72, 58, ORANGE), (86, 50, PINK)):
        if base.in_circle(x, y, cx, cy, 4.4):
            col = (*c, 255)
    if base.in_spark(x, y, 114, 22, 16):
        col = (*GOLD, 255)
    if base.in_spark(x, y, 114, 22, 7):
        col = (*WHITE, 255)
    return col


# ===== F 圆环徽章 + 星芒 =====
def v_ring(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    dx, dy = x - 72, y - 72
    d = math.hypot(dx, dy)
    if 37 <= d <= 53:
        col = (*WHITE, 255)
    for ang, c in ((45, CYAN), (135, GREEN), (225, ORANGE), (315, PINK)):
        cx = 72 + 45 * math.cos(math.radians(ang))
        cy = 72 + 45 * math.sin(math.radians(ang))
        if base.in_circle(x, y, cx, cy, 6):
            col = (*c, 255)
    if base.in_spark(x, y, 72, 72, 24):
        col = (*GOLD, 255)
    if base.in_spark(x, y, 72, 72, 10):
        col = (*WHITE, 255)
    return col


# ===== G 「W」字母徽章（万宝首字母）+ 星芒 =====
def stroke_poly(x0, y0, x1, y1, w):
    """把线段扩成带厚度的四边形点集。"""
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy) or 1
    nx, ny = -dy / L * w / 2, dx / L * w / 2
    return [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]


W_STROKES = [
    stroke_poly(28, 42, 50, 104, 15),
    stroke_poly(50, 104, 72, 42, 15),
    stroke_poly(72, 42, 94, 104, 15),
    stroke_poly(94, 104, 116, 42, 15),
]


def v_mono(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    for p in W_STROKES:
        if in_poly(x, y, p):
            col = (*WHITE, 255)
    if base.in_spark(x, y, 116, 28, 14):
        col = (*GOLD, 255)
    if base.in_spark(x, y, 116, 28, 6):
        col = (*WHITE, 255)
    return col


# ===== H 对话气泡 + 星芒（对话式入口） =====
def v_bubble(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    if base.in_rounded_rect(x, y, 22, 30, 122, 96, 24):
        col = (*WHITE, 255)
    if in_poly(x, y, [(48, 92), (48, 120), (74, 92)]):
        col = (*WHITE, 255)
    if base.in_spark(x, y, 72, 56, 20):
        col = (*GOLD, 255)
    if base.in_spark(x, y, 72, 56, 9):
        col = (*WHITE, 255)
    for (cx, c) in ((52, CYAN), (72, ORANGE), (92, PINK)):
        if base.in_circle(x, y, cx, 82, 4.6):
            col = (*c, 255)
    return col


# ===== I 模块叠加（加功能 = 加模块） =====
def v_layers(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    if base.in_rounded_rect(x, y, 24, 24, 92, 92, 20):
        col = (*WHITE, 255)
    if base.in_rounded_rect(x, y, 52, 52, 120, 120, 20):
        col = (167, 139, 250, 255)     # violet-400
    if base.in_spark(x, y, 104, 34, 15):
        col = (*GOLD, 255)
    if base.in_spark(x, y, 104, 34, 6):
        col = (*WHITE, 255)
    if base.in_circle(x, y, 34, 102, 5):
        col = (*CYAN, 255)
    return col


# ===== J 闪电（速度/高效） =====
BOLT = [(80, 22), (44, 78), (68, 78), (58, 122), (100, 62), (74, 62)]


def v_bolt(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    if in_poly(x, y, BOLT):
        col = (*WHITE, 255)
    if base.in_spark(x, y, 108, 34, 14):
        col = (*GOLD, 255)
    if base.in_spark(x, y, 108, 34, 6):
        col = (*WHITE, 255)
    return col


# ===== K 开盖宝箱 + 散出光点 =====
def v_open_chest(x, y):
    col = bg(x, y)
    if col[3] == 0:
        return col
    # 掀起的盖子（透视梯形）
    if in_poly(x, y, [(26, 74), (118, 74), (104, 44), (40, 44)]):
        col = (*WHITE, 255)
    # 箱体
    if base.in_rounded_rect(x, y, 32, 74, 112, 122, 12):
        col = (*WHITE, 255)
    # 箱内金光（窄束，两侧留出盖板白边）
    if in_poly(x, y, [(52, 74), (92, 74), (84, 46), (60, 46)]):
        col = (253, 224, 71, 255)
    if in_poly(x, y, [(60, 74), (84, 74), (79, 50), (65, 50)]):
        col = (*GOLD, 255)
    # 金色锁扣
    if base.in_rounded_rect(x, y, 66, 86, 78, 100, 4):
        col = (*GOLD, 255)
    # 飞出的彩点
    for (cx, cy, c) in ((48, 28, CYAN), (72, 18, ORANGE), (96, 28, PINK), (116, 46, GREEN)):
        if base.in_circle(x, y, cx, cy, 5):
            col = (*c, 255)
    if base.in_spark(x, y, 30, 40, 13):
        col = (*WHITE, 255)
    return col


VARIANTS = [
    ("b_gear", v_gear, "齿轮 + 星芒：工具感最强"),
    ("c_grid", v_grid, "四宫格 + 中心星芒：一个入口 N 个工具"),
    ("d_bulb", v_bulb, "灯泡 + 漂浮彩点：AI 灵感"),
    ("e_cube", v_cube, "等距 3D 宝箱：立体感最强"),
    ("f_ring", v_ring, "圆环徽章 + 星芒：最简洁，小尺寸清晰"),
    ("g_mono", v_mono, "「W」字母徽章 + 星芒：品牌感最强"),
    ("h_bubble", v_bubble, "对话气泡 + 星芒：对话式入口"),
    ("i_layers", v_layers, "模块叠加：加功能 = 加模块"),
    ("j_bolt", v_bolt, "闪电 + 星芒：高效/提速"),
    ("k_open_chest", v_open_chest, "开盖宝箱 + 散出彩点：最有惊喜感"),
]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # 先把当前正式款（A 宝箱）拷进预览目录，便于 6 款横向比对
    src = os.path.join(ASSETS_DIR, "icon.png")
    if os.path.exists(src):
        with open(src, "rb") as f:
            data = f.read()
        with open(os.path.join(OUT_DIR, "icon_a_chest.png"), "wb") as f:
            f.write(data)
        print(f"copied A -> {os.path.join(OUT_DIR, 'icon_a_chest.png')}")

    for name, fn, desc in VARIANTS:
        rows = render(fn)
        path = os.path.join(OUT_DIR, f"icon_{name}.png")
        with open(path, "wb") as f:
            f.write(base.to_png(rows))
        print(f"\n===== {name} · {desc} =====")
        print(base.preview(rows, cols=48, rows_out=24))
        print(f"-> {path}")


if __name__ == "__main__":
    main()
