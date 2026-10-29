#!/usr/bin/env python3
"""图标的绘制层：平底 + 线条符号。

设计取向是**极简线条**，不做材质、不做立体、不做渐变。判断依据很简单：
这个 app 的功能就是「把盘安全推出」，图标只需要把这件事说清楚，
多余的金属质感、玻璃反光、内阴影都是噪音。

被 `make_icon.py`（出 icns）和 `icon_concepts.py`（出配色对照）共用，
所以改这里的任何数值都会同时反映到成品和候选图上。

坐标一律按 1024 的逻辑画布写，由 `k = size / BASE` 缩放到实际像素。
"""

import numpy as np
from PIL import Image, ImageDraw

BASE = 1024          # 逻辑尺寸
SS = 4               # 超采样倍数，边缘靠缩小来抗锯齿

# 底色：平铺石墨，不渐变。白色线条在上面才够干净。
#
# 选它而不是蓝：这个图标在 Dock 里要和一堆彩色图标并存，石墨是唯一
# 不会和邻居抢视线的底色；蓝色版（#2F4A6B）留给面板头部的小图标用，
# 那里是 30pt 的小方块，需要和面板的蓝调呼应。
PLATE = (0x2E, 0x32, 0x38)
INK = (0xFF, 0xFF, 0xFF)

BODY_RATIO = 824 / 1024   # 超椭圆占画布的比例，和 macOS 图标惯例一致
SQUIRCLE_N = 5.0          # 超椭圆指数；纯圆角矩形会显得"胖"

# ---- 符号几何（全部是**中心线**坐标，描边向两侧各扩 STROKE/2）----
TRI_W = 420.0        # 三角底边宽
TRI_H = 364.0        # 三角高 → 高/底 = 0.867，接近等边的 0.866
STROKE = 36.0        # 线宽，全图统一，不出现两种粗细
GAP = 48.0           # 三角底边外沿到横条外沿的净空
BAR_W = 420.0        # 横条长（与三角底边等宽，两条边对齐成一根竖线）
CENTER_Y = 508.0     # 符号外框的视觉中心，比画布中心高 4（三角上轻下重）

# 小尺寸下限。16px 下按比例算出来的线宽只有 0.56px、净空只有 0.75px，
# 两个都等于没有——线被抗锯齿抹平，净空被抹掉之后三角和横条直接糊成一块。
# 所以描边和净空都要按目标像素反算下限；16px 更极端，直接退化成实心符号。
MIN_STROKE_PX = 1.6
MIN_GAP_PX = 1.3
SOLID_BELOW = 24     # 小于这个像素尺寸就不再描边，改实心


def squircle_mask(size: int, body_ratio: float = BODY_RATIO, n: float = SQUIRCLE_N) -> np.ndarray:
    """超椭圆蒙版。n=5 接近 Apple 的 squircle。"""
    body = int(size * body_ratio)
    pad = (size - body) // 2
    a = body / 2.0
    y, x = np.mgrid[0:body, 0:body]
    dx = (x - (body - 1) / 2.0) / a
    dy = (y - (body - 1) / 2.0) / a
    inside = (np.abs(dx) ** n + np.abs(dy) ** n) <= 1.0
    mask = np.zeros((size, size), dtype=np.float32)
    mask[pad:pad + body, pad:pad + body] = inside.astype(np.float32)
    return mask


def plate(size: int, rgb=PLATE) -> Image.Image:
    """平铺色底。没有渐变、没有受光、没有暗角——这些都会被读成"花里胡哨"。"""
    mask = squircle_mask(size)
    body = np.zeros((size, size, 4), dtype=np.uint8)
    body[:, :, 0] = rgb[0]
    body[:, :, 1] = rgb[1]
    body[:, :, 2] = rgb[2]
    body[:, :, 3] = (mask * 255.0).astype(np.uint8)
    return Image.fromarray(body, "RGBA")


def glyph(size: int, stroke: float = STROKE, gap: float = GAP,
          filled: bool = False, rgb=INK) -> Image.Image:
    """推出符号：一个三角 + 一条胶囊横条，全部用线条画。

    描边三角形的三个顶点要单独补圆点。PIL 的 `line` 只在折点做圆角，
    首尾点仍是平头；三角是闭合路径，起点终点重合在顶点上，不补就会缺一个角。

    横条在实心/描边两种模式下尺寸完全一致（高 = 线宽、两端圆头），
    所以小尺寸退化时只有三角从空心变实心，横条不用跟着改。

    整个符号的外框由 `stroke`/`gap` 反算后**始终居中**——小尺寸会把这两个值
    放大，如果顶点坐标写死，符号就会往一侧长出去。
    """
    k = size / BASE
    s = stroke * k
    cx = 512 * k

    # 先算外框：上沿 = 顶点 - 半个线宽，下沿 = 横条中心 + 半个线宽
    total_h = TRI_H + stroke + gap + stroke
    apex_y = CENTER_Y - total_h / 2 + stroke / 2

    hw = TRI_W * k / 2
    apex = (cx, apex_y * k)
    base_y = (apex_y + TRI_H) * k
    left = (cx - hw, base_y)
    right = (cx + hw, base_y)

    bar_cy = (apex_y + TRI_H + stroke + gap) * k
    bar_hw = BAR_W * k / 2

    layer = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(layer)

    if filled:
        d.polygon([apex, right, left], fill=255)
    else:
        for a, b in ((apex, right), (right, left), (left, apex)):
            d.line([a, b], fill=255, width=max(1, int(round(s))))
        r = s / 2
        for p in (apex, right, left):
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=255)

    d.rounded_rectangle([cx - bar_hw, bar_cy - s / 2, cx + bar_hw, bar_cy + s / 2],
                        radius=s / 2, fill=255)

    out = Image.new("RGBA", (size, size), rgb + (0,))
    out.putalpha(layer)
    return out


def stroke_for(size: int) -> float:
    """按目标像素反算线宽（返回 1024 逻辑坐标下的值）。

    64px 以上直接用 STROKE；更小的尺寸按 MIN_STROKE_PX 兜底，
    否则线会细到被抗锯齿抹平。
    """
    return max(STROKE, MIN_STROKE_PX * BASE / size)


def gap_for(size: int) -> float:
    """净空同理。净空不够时三角和横条会粘成一坨，比线细更致命。"""
    return max(GAP, MIN_GAP_PX * BASE / size)


def render(size: int, plate_rgb=PLATE, ink_rgb=INK) -> Image.Image:
    """出某个像素尺寸的成品。

    每个尺寸**独立渲染**，不是从 1024 缩放下来——小尺寸需要更粗的描边、
    更宽的净空，从大图缩下去只会得到一堆灰边和粘连的符号。
    """
    w = size * SS
    filled = size < SOLID_BELOW
    body = plate(w, plate_rgb)
    ink = glyph(w, stroke_for(size), gap_for(size), filled, ink_rgb)
    return Image.alpha_composite(body, ink).resize((size, size), Image.LANCZOS)
