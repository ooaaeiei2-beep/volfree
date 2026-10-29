#!/usr/bin/env python3
"""把离屏渲染出来的面板拼成对照图，以及图标的多尺寸预览。

只在需要更新 `shots/` 时手动跑，不参与构建。
中文标注用 ~/Library/Fonts 里的 MiSans（系统字体没有可用的中文无衬线）。
"""

import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = os.path.expanduser("~/Library/Fonts/MiSans-Demibold.ttf")
FONT_REG = os.path.expanduser("~/Library/Fonts/MiSans-Regular.ttf")

BG = (245, 245, 247, 255)
CARD = (255, 255, 255, 255)
LINE = (222, 222, 227, 255)
INK = (28, 28, 30, 255)
GREY = (110, 110, 118, 255)

PAD = 48
GAP = 40
CARD_PAD = 16
CAP_H = 84


def load(path):
    return Image.open(path).convert("RGBA")


def grid(cells, cols, out, gap=GAP):
    """cells: [(png 路径, 标题, 副标题), ...]，按行排布。"""
    images = [load(p) for p, _, _ in cells]
    f_title = ImageFont.truetype(FONT_BOLD, 30)
    f_sub = ImageFont.truetype(FONT_REG, 21)

    rows = [cells[i:i + cols] for i in range(0, len(cells), cols)]
    col_w = [
        max(images[cells.index(row[c])].size[0] for row in rows if c < len(row)) + CARD_PAD * 2
        for c in range(cols)
    ]
    row_h = [max(images[cells.index(c)].size[1] for c in row) + CARD_PAD * 2 for row in rows]

    W = PAD * 2 + sum(col_w) + gap * (cols - 1)
    H = PAD * 2 + sum(CAP_H + h for h in row_h) + gap * (len(rows) - 1)

    canvas = Image.new("RGBA", (W, H), BG)
    d = ImageDraw.Draw(canvas)

    y = PAD
    for row, h in zip(rows, row_h):
        x = PAD
        for i, (path, title, sub) in enumerate(row):
            w = col_w[i]
            d.rounded_rectangle([x, y + CAP_H, x + w, y + CAP_H + h],
                                radius=16, fill=CARD, outline=LINE, width=2)
            im = images[cells.index((path, title, sub))]
            canvas.paste(im, (x + CARD_PAD, y + CAP_H + CARD_PAD), im)
            d.text((x + 4, y + 6), title, font=f_title, fill=INK)
            d.text((x + 4, y + 46), sub, font=f_sub, fill=GREY)
            x += w + gap
        y += CAP_H + h + gap

    canvas.convert("RGB").save(out, quality=95)
    print("写出", out, canvas.size)


def side(cells, out):
    grid(cells, len(cells), out)


def icns_entries(icns):
    """把 icns 拆成 {像素尺寸: PIL 图}，只取 @2x 条目。

    为什么用 @2x：它和 `Resources/icon-1024.png` 逐像素一致，是 Retina 屏真正读的那一组。
    1x 的 16/32 条目是 icns 的 `is32` 格式，`iconutil` 写入时会把 RGB 按 alpha 反除
    （见 README「图标」一节），颜色偏亮，拿它做预览会看到并不存在的缺陷。
    """
    tmp = tempfile.mkdtemp(prefix="volfree-icns-")
    try:
        # iconutil 要求 -o 指向一个**不存在**的路径，mkdtemp 建出来的目录得再下一层
        dest = os.path.join(tmp, "out.iconset")
        subprocess.run(["iconutil", "-c", "iconset", icns, "-o", dest],
                       check=True, capture_output=True)
        out = {}
        for base in (16, 32, 128, 256, 512):
            p = os.path.join(dest, f"icon_{base}x{base}@2x.png")
            if os.path.exists(p):
                out[base * 2] = Image.open(p).convert("RGBA")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pick(entries, want):
    """取"像素尺寸够用的最小条目"，缩到 want。icns 没有 64 的 @2x，只能这样兜。"""
    for px in sorted(entries):
        if px >= want:
            im = entries[px]
            return im if px == want else im.resize((want, want), Image.LANCZOS)
    return None


def icon_preview(icns, out):
    """把 icns 里各档尺寸铺开，浅色/深色各一行，用来核对小尺寸是否还认得出符号。

    图标最容易翻车的是 16/32 那两档：母版看着没问题，缩下去符号就糊成一团。
    这里贴的是 icns 里的真实条目，不是另画一遍——预览和成品必须是同一份数据，
    否则改了生成脚本却忘了重新打包，预览会照样好看。
    """
    sizes = [16, 32, 64, 128, 256]
    entries = icns_entries(icns)
    f_title = ImageFont.truetype(FONT_BOLD, 26)
    f_lab = ImageFont.truetype(FONT_REG, 16)

    gap, pad, label_h = 34, 36, 26
    row_h = max(sizes) + label_h + pad * 2
    w = pad * 2 + sum(sizes) + gap * (len(sizes) - 1)
    W = PAD * 2 + w
    H = PAD * 2 + 44 + row_h * 2 + 24

    canvas = Image.new("RGBA", (W, H), BG)
    d = ImageDraw.Draw(canvas)
    d.text((PAD, PAD), "AppIcon.icns 各尺寸（取自包内 @2x 条目，缩到标称尺寸并排）",
           font=f_title, fill=INK)

    rows = [((255, 255, 255, 255), GREY), ((28, 28, 30, 255), (168, 168, 176, 255))]

    for i, (bg, fg) in enumerate(rows):
        y0 = PAD + 44 + i * (row_h + 24)
        d.rounded_rectangle([PAD, y0, PAD + w, y0 + row_h],
                            radius=16, fill=bg, outline=LINE, width=2)
        x = PAD + pad
        for s in sizes:
            im = pick(entries, s)
            if im is None:
                x += s + gap
                continue
            # 底部对齐：小尺寸落在同一条基线上，方便横向比清晰度
            canvas.paste(im, (x, y0 + pad + max(sizes) - s), im)
            d.text((x + s / 2, y0 + row_h - pad - label_h + 4),
                   f"{s}px", font=f_lab, fill=fg, anchor="ma")
            x += s + gap

    canvas.convert("RGB").save(out)
    print("写出", out, canvas.size)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    shots = os.path.join(here, "shots")

    def p(name):
        return os.path.join(shots, name)

    grid([
        (p("01-绿.png"), "绿 · 没人在用",
         "权限受限，所以只说「未发现占用」"),
        (p("02-黄.png"), "黄 · 有人占着但没在写",
         "主按钮一次结束并推出"),
        (p("03-黄-放弃写入.png"), "黄 · 放弃写入",
         "写的是已删除的临时文件，结束会丢且找不回"),
        (p("04-红.png"), "红 · 正在写",
         "只给「等待完成」，不提供一键拔"),
    ], 2, p("四态对照.png"))

    # 06 与 07 是把两张单图拼在一起，源图（06a/06b、07a/07b）留在 shots/ 里，
    # 方便单独引用其中一张。
    grid([
        (p("06a-绿-面板.png"), "权限受限时的绿态",
         "只敢说「未发现占用」，不说「没人用」"),
        (p("06b-设置.png"), "设置窗口",
         "登录项已注册（app 在 /Applications 下才能注册）"),
    ], 2, p("06-绿-权限受限与设置.png"))

    grid([
        (p("07a-红-收起.png"), "红 · 收起",
         "速率按进程给，两个样本才算得出来"),
        (p("07b-红-展开.png"), "红 · 展开",
         "每个进程各自的写入速率"),
    ], 2, p("07-红-写入速率.png"))

    # 图标预览直接读包内那份 icns——预览与成品必须是同一份数据，
    # 否则改了生成脚本却忘了重新打包，预览会照样好看。
    icns = os.path.join(here, "Resources", "AppIcon.icns")
    if os.path.exists(icns):
        icon_preview(icns, p("11-图标.png"))


