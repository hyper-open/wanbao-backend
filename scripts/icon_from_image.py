#!/usr/bin/env python3
"""把生图模型产出的方图处理成小程序图标（纯标准库，无 PIL 依赖）。

能力：
  1. 解码 PNG（8bit、非隔行、RGB/RGBA）
  2. 面积平均缩放（1024 -> 144 等非整数倍也平滑）
  3. 生成圆角版本（带 alpha）
  4. 打印「色相 ASCII 图」+ 分区取色，便于在无图像环境下核对构图

用法：
  python3 scripts/icon_from_image.py <input.png> [output_dir]
"""
import math
import os
import struct
import sys
import zlib

SIZE = 144          # 小程序图标目标尺寸
RADIUS_RATIO = 0.22  # 圆角比例


# ---------- PNG 解码 ----------
def decode_png(path):
    d = open(path, "rb").read()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos, idat = 8, b""
    w = h = bd = ct = inter = None
    while pos < len(d):
        ln = struct.unpack(">I", d[pos : pos + 4])[0]
        typ = d[pos + 4 : pos + 8]
        data = d[pos + 8 : pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bd, ct, _comp, _filt, inter = struct.unpack(">IIBBBBB", data)
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
    if bd != 8 or inter != 0:
        raise ValueError(f"unsupported png: bitdepth={bd} interlace={inter}")
    nch = {0: 1, 2: 3, 4: 2, 6: 4}[ct]
    raw = zlib.decompress(idat)
    stride = w * nch
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p : p + stride])
        p += stride
        if f == 1:
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return w, h, nch, out


# ---------- 缩放 ----------
def resize_area(buf, w, h, nch, nw, nh):
    out = bytearray(nw * nh * nch)
    for oy in range(nh):
        y0, y1 = oy * h / nh, (oy + 1) * h / nh
        iy0, iy1 = int(y0), min(int(math.ceil(y1)), h)
        for ox in range(nw):
            x0, x1 = ox * w / nw, (ox + 1) * w / nw
            ix0, ix1 = int(x0), min(int(math.ceil(x1)), w)
            acc = [0] * nch
            cnt = 0
            for y in range(iy0, iy1):
                base = y * w * nch
                for x in range(ix0, ix1):
                    o = base + x * nch
                    for c in range(nch):
                        acc[c] += buf[o + c]
                    cnt += 1
            o = (oy * nw + ox) * nch
            for c in range(nch):
                out[o + c] = acc[c] // max(cnt, 1)
    return out


# ---------- PNG 编码 ----------
def encode_png(w, h, nch, buf):
    ct = {1: 0, 3: 2, 4: 6}[nch]
    raw = b"".join(b"\x00" + bytes(buf[y * w * nch : (y + 1) * w * nch]) for y in range(h))
    png = b"\x89PNG\r\n\x1a\n"
    for typ, data in [
        (b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ct, 0, 0, 0)),
        (b"IDAT", zlib.compress(raw, 9)),
        (b"IEND", b""),
    ]:
        png += (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )
    return png


def remove_watermark(buf, w, h, nch, box=(0.865, 0.925, 1.0, 1.0), pad=8, feather=12):
    """去除右下角生图水印（「AI 生成」字样）。

    原理：水印落在平滑渐变背景上，用区域外侧 8px 的参考行/列做可加性外推
    v(x,y) = T(x) + L(y) - C，再对区域边界做羽化过渡，避免硬接缝。
    box 为相对坐标 (x0, y0, x1, y1)。
    """
    x0, y0 = int(w * box[0]), int(h * box[1])
    x1, y1 = min(int(w * box[2]), w - 1), min(int(h * box[3]), h - 1)
    sx, sy = max(x0 - pad, 0), max(y0 - pad, 0)
    out = bytearray(buf)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            o = (y * w + x) * nch
            oT, oL, oC = (sy * w + x) * nch, (y * w + sx) * nch, (sy * w + sx) * nch
            d = min(x - x0, y - y0, x1 - x, y1 - y)
            k = 1.0 if d >= feather else (d + 1) / (feather + 1)
            for c in range(3):
                v = buf[oT + c] + buf[oL + c] - buf[oC + c]
                v = 0 if v < 0 else (255 if v > 255 else v)
                out[o + c] = int(buf[o + c] * (1 - k) + v * k)
    return out


BRAND_TOP = (99, 102, 241)    # #6366f1
BRAND_BOT = (139, 92, 246)    # #8b5cf6


def brand_blend(buf, w, h, nch, strength=0.72, dark_thresh=115, gamma=0.88):
    """把偏暗的背景拉向品牌渐变：按亮度做加权混合（越暗混得越多），主体高光保留。

    strength    : 暗部最多混合比例
    dark_thresh : 低于该亮度算「背景」
    gamma       : 整体提亮曲线（<1 提亮）
    """
    lut = [min(255, int(255 * (v / 255.0) ** gamma)) for v in range(256)]
    out = bytearray(buf)
    denom = max(2 * (w - 1), 1)
    for y in range(h):
        for x in range(w):
            o = (y * w + x) * nch
            r, g, b = lut[buf[o]], lut[buf[o + 1]], lut[buf[o + 2]]
            luma = 0.299 * r + 0.587 * g + 0.114 * b
            k = 0.0
            if luma < dark_thresh:
                k = min(1.0, (dark_thresh - luma) / dark_thresh) * strength
            if k <= 0.001:
                out[o], out[o + 1], out[o + 2] = r, g, b
                continue
            t = (x + y) / denom
            for i, c in enumerate((r, g, b)):
                bgc = BRAND_TOP[i] + (BRAND_BOT[i] - BRAND_TOP[i]) * t
                out[o + i] = int(c * (1 - k) + bgc * k)
    return out


def round_corners(buf, size, nch, ratio=RADIUS_RATIO):
    """生成圆角版本（输出 RGBA）。"""
    r = size * ratio
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            dx = max(r - x, x - (size - 1 - r), 0)
            dy = max(r - y, y - (size - 1 - r), 0)
            inside = dx * dx + dy * dy <= r * r
            if not inside:
                cover = 0.0
            elif dx == 0 or dy == 0:
                cover = 1.0
            else:
                d = math.hypot(dx, dy)
                cover = 1.0 if d <= r else max(0.0, 1.0 - (d - r))
            o = (y * size + x) * 4
            s = (y * size + x) * nch
            for c in range(3):
                out[o + c] = buf[s + c]
            out[o + 3] = int(255 * cover)
    return out


# ---------- 可视校验 ----------
def hue_char(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    if mx < 40:
        return "."
    sat = (mx - mn) / mx
    if mx > 200 and sat < 0.13:
        return "#"
    if sat < 0.15:
        return ":" if mx > 110 else "."
    h = 0.0
    if mx == r:
        h = (60 * ((g - b) / (mx - mn))) % 360
    elif mx == g:
        h = 60 * ((b - r) / (mx - mn)) + 120
    else:
        h = 60 * ((r - g) / (mx - mn)) + 240
    for lo, hi, ch in (
        (0, 22, "p"), (22, 62, "*"), (62, 95, "*"), (95, 160, "g"),
        (160, 200, "c"), (200, 262, "b"), (262, 320, "v"), (320, 361, "p"),
    ):
        if lo <= h < hi:
            return ch
    return "?"


def ascii_map(w, h, nch, buf, cols=48, rows=24):
    cx, cy = w / cols, h / rows
    lines = []
    for oy in range(rows):
        line = ""
        for ox in range(cols):
            acc = [0, 0, 0]
            n = 0
            for y in range(int(oy * cy), min(int((oy + 1) * cy), h), max(1, int(cy) // 2)):
                for x in range(int(ox * cx), min(int((ox + 1) * cx), w), max(1, int(cx) // 2)):
                    o = (y * w + x) * nch
                    for c in range(3):
                        acc[c] += buf[o + c]
                    n += 1
            line += hue_char(*(acc[c] // max(n, 1) for c in range(3)))
        lines.append(line)
    return "\n".join(lines)


def region_stats(w, h, nch, buf):
    """分区亮度/主色，用于快速判断构图（中心是否有主体、四角是否为底色）。"""
    def avg(x0f, y0f, x1f, y1f):
        x0, x1 = int(w * x0f), int(w * x1f)
        y0, y1 = int(h * y0f), int(h * y1f)
        acc = [0, 0, 0]
        n = 0
        for y in range(y0, y1, 4):
            for x in range(x0, x1, 4):
                o = (y * w + x) * nch
                for c in range(3):
                    acc[c] += buf[o + c]
                n += 1
        return tuple(acc[c] // max(n, 1) for c in range(3))

    return {
        "左上角": avg(0, 0, 0.18, 0.18),
        "右上角": avg(0.82, 0, 1, 0.18),
        "左下角": avg(0, 0.82, 0.18, 1),
        "右下角": avg(0.82, 0.82, 1, 1),
        "中心": avg(0.4, 0.4, 0.6, 0.6),
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    brand = "--brand" in sys.argv
    size = int(_opt("--size") or SIZE)
    if not args:
        print(__doc__)
        return
    src = args[0]
    out_dir = args[1] if len(args) > 1 else os.path.dirname(src)
    w, h, nch, buf = decode_png(src)
    print(f"源图 {os.path.basename(src)}: {w}x{h}, {nch} 通道")
    suffix = ""
    if "--clean" in sys.argv:
        buf = remove_watermark(buf, w, h, nch)
        suffix += "_clean"
        print("已去除右下角水印")
    print("== 分区取色 ==")
    for name, c in region_stats(w, h, nch, buf).items():
        print(f"  {name}: rgb{c}")
    print("== 色相预览（b 蓝 v 紫 # 白 * 金/橙 g 绿 c 青 p 玫红 . 暗）==")
    print(ascii_map(w, h, nch, buf))

    base = os.path.splitext(os.path.basename(src))[0] + suffix
    if brand:
        buf = brand_blend(buf, w, h, nch, **({"strength": float(v)} if (v := _opt("--strength")) else {}))
        print("== 品牌化调色后分区取色 ==")
        for name, c in region_stats(w, h, nch, buf).items():
            print(f"  {name}: rgb{c}")
        base += "_brand"

    small = resize_area(buf, w, h, nch, size, size)
    p1 = os.path.join(out_dir, f"{base}_{size}.png")
    with open(p1, "wb") as f:
        f.write(encode_png(size, size, nch, small))
    p2 = os.path.join(out_dir, f"{base}_{size}_round.png")
    with open(p2, "wb") as f:
        f.write(encode_png(size, size, 4, round_corners(small, size, nch)))
    print(f"-> {p1}\n-> {p2}")


def _opt(name):
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


if __name__ == "__main__":
    main()
