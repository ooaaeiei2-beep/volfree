#!/usr/bin/env python3
"""生成 volfree 的 app 图标。

设计：平铺深蓝的超椭圆底 + 白色线条「推出」符号（空心三角在上、胶囊横条在下）。
不渐变、不投影、不立体——图标只需要把功能说清楚。

几何与配色全在 `icon_design.py`，这个脚本只负责出图与打包。

每个尺寸独立渲染（`render(px)`），不是把 1024 缩下去：
16/32 那两档需要更粗的描边，16px 更是直接退化成实心符号，
从大图缩只会得到一团灰边。

用法：
    python3 make_icon.py          # 生成 Resources/icon-1024.png 与 AppIcon.icns
"""

import os
import shutil
import subprocess
import sys

from icon_design import BASE, render

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ICNS = os.path.join(HERE, "Resources", "AppIcon.icns")
OUT_MASTER = os.path.join(HERE, "Resources", "icon-1024.png")

# icns 需要的全部像素尺寸（16/32/128/256/512 各 @1x/@2x）
SIZES = [16, 32, 64, 128, 256, 512, 1024]


def main() -> int:
    cache = {}

    def get(px: int):
        if px not in cache:
            cache[px] = render(px)
        return cache[px]

    master = get(BASE)
    os.makedirs(os.path.dirname(OUT_MASTER), exist_ok=True)
    master.save(OUT_MASTER)
    print("写出", OUT_MASTER)

    iconset = os.path.join(HERE, "build", "AppIcon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    for base in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            px = base * scale
            get(px).save(os.path.join(iconset, f"icon_{base}x{base}{suffix}.png"))
    # 1024 只作为 512@2x 存在，已覆盖

    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", OUT_ICNS], check=True)
    print("写出", OUT_ICNS, os.path.getsize(OUT_ICNS), "字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
