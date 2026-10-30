#!/bin/sh
# 把 SPM 产物打成 .app。没有完整 Xcode 也能构建，所以不依赖 .xcodeproj。
set -eu

cd "$(dirname "$0")"

CONFIG="${CONFIG:-release}"
APP="build/Volfree.app"

echo "==> 编译（${CONFIG}）"
# --disable-sandbox：SwiftPM 默认用 sandbox-exec 编译清单，
# 在受限的自动化环境里会报 "sandbox_apply: Operation not permitted"。
swift build -c "$CONFIG" --product VolfreeApp --disable-sandbox

BIN=".build/$CONFIG/VolfreeApp"
if [ ! -x "$BIN" ]; then
    echo "构建产物不存在：$BIN" >&2
    exit 1
fi

echo "==> 组装 $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Volfree"
cp Resources/Info.plist "$APP/Contents/Info.plist"
cp ../volfree.py "$APP/Contents/Resources/volfree.py"

# 版本号只认一个来源：volfree.py 里的 VERSION（它又来自 volfree/models.py）。
# 别在 Info.plist 里写死版本——发版时两个数字会打架，这个坑踩过一次。
VER=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' ../volfree.py)
if [ -n "$VER" ]; then
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" "$APP/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VER" "$APP/Contents/Info.plist"
else
    echo "警告：没能从 ../volfree.py 读到 VERSION，沿用 Info.plist 里的旧值" >&2
fi

# 图标：由 make_icon.sh（内部编译 make_icon.swift）生成，缺了就直接报错，
# 免得又 build 出一个没图标的 app
if [ ! -f Resources/AppIcon.icns ]; then
    echo "缺少 Resources/AppIcon.icns，先跑：./make_icon.sh" >&2
    exit 1
fi
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

echo "==> 签名（adhoc）"
codesign --force --sign - "$APP" >/dev/null 2>&1 || {
    echo "签名失败" >&2
    exit 1
}

echo "==> 校验"
codesign -vvv "$APP" 2>&1 | sed 's/^/    /'
/usr/libexec/PlistBuddy -c "Print :LSUIElement" "$APP/Contents/Info.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Print :CFBundleIconFile" "$APP/Contents/Info.plist" >/dev/null
test -f "$APP/Contents/Resources/AppIcon.icns" || {
    echo "图标没进包" >&2
    exit 1
}

echo
echo "完成：$PWD/$APP"
echo "运行：open \"$PWD/$APP\""
