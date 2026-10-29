#!/bin/sh
# 生成 volfree 的 app 图标。
#
# 图标是「面板头部那个 30pt 小图标」的放大版：蓝色对角渐变 + 白色 externaldrive.fill。
# 绘制逻辑在 make_icon.swift（用 AppKit 渲染 SF Symbol，保证与面板里逐像素同源），
# 这个脚本只负责编译、运行、打包 icns。
#
# 需要 Xcode Command Line Tools 里的 swiftc（本机可用；完整 Xcode 不需要）。
#
# 用法：
#   ./make_icon.sh          # 产出 Resources/icon-1024.png 与 Resources/AppIcon.icns
set -eu

cd "$(dirname "$0")"

BIN="build/make_icon"
mkdir -p build

echo "==> 编译 make_icon.swift"
swiftc -O make_icon.swift -o "$BIN"

echo "==> 渲染各尺寸"
# 产物先落到 build/，再分派：iconset 留给 iconutil，母版放进 Resources/
"$BIN" build
mv -f build/icon-1024.png Resources/icon-1024.png
echo "写出 Resources/icon-1024.png"

echo "==> 打包 icns"
iconutil -c icns build/AppIcon.iconset -o Resources/AppIcon.icns
echo "写出 Resources/AppIcon.icns $(wc -c < Resources/AppIcon.icns | tr -d ' ') 字节"
