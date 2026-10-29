#!/usr/bin/env python3
"""图标配色对照：同一套线条符号，换三组底色，用来定色。

只出预览图，不生成 icns。绘制代码在 `icon_design.py`，和成品共用同一套几何。
输出到 `shots/12-图标候选.png`，是选型记录，不是成品截图。

尺寸档全部**按目标像素独立渲染**，不是把大图缩下去——否则 16/32 那两档
看到的是插值糊边，不是真实效果。

用法：
    python3 icon_concepts.py
"""

import os

from PIL import Image, ImageDraw, ImageFont

from icon_design import render

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_BOLD = os.path.expanduser("~/Library/Fonts/MiSans-Demibold.ttf")
FONT_REG = os.path.expanduser("~/Library/Fonts/MiSans-Regular.ttf")

# (名字, 底色, 线条色)
COLORWAYS = [
    ("A · 深蓝底 / 白线", (0x2F, 0x4A, 0x6B), (0xFF, 0xFF, 0xFF)),
    ("B · 石墨底 / 白线（采用）", (0x2E, 0x32, 0x38), (0xFF, 0xFF, 0xFF)),
    ("C · 浅灰底 / 深蓝线", (0xE9, 0xEE, 0xF4), (0x2F, 0x4A, 0x6B)),
]

SIZES = [256, 64, 32, 16]


def main() -> int:
    cell, pad, gap, name_w = 248, 40, 24, 330
    row_h = cell + 52
    W = pad * 2 + name_w + len(SIZES) * cell + (len(SIZES) - 1) * gap
    H = pad * 2 + 56 + len(COLORWAYS) * row_h

    canvas = Image.new("RGB", (W, H), (245, 245, 247))
    d = ImageDraw.Draw(canvas)
    f_title = ImageFont.truetype(FONT_BOLD, 30)
    f_name = ImageFont.truetype(FONT_BOLD, 24)
    f_lab = ImageFont.truetype(FONT_REG, 19)

    d.text((pad, pad), "图标配色对照（同一套线条符号）", font=f_title, fill=(28, 28, 30))

    y = pad + 56
    for name, bg, ink in COLORWAYS:
        d.text((pad + 4, y + cell // 2 - 16), name, font=f_name, fill=(28, 28, 30))
        x = pad + name_w
        for s in SIZES:
            im = render(s, bg, ink)
            shown = im.resize((cell - 48, cell - 48),
                              Image.NEAREST if s <= 64 else Image.LANCZOS)
            canvas.paste(shown, (x + 24, y + 16), shown)
            d.text((x + 24, y + cell - 24), f"{s}px", font=f_lab, fill=(110, 110, 118))
            x += cell + gap
        y += row_h

    out = os.path.join(HERE, "shots", "12-图标候选.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    canvas.save(out)
    print("写出", out, canvas.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
