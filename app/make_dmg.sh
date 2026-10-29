#!/bin/sh
# 打一个可以直接分发的 dmg：Volfree.app + 「应用程序」快捷方式。
#
# 只做打包，不做签名——本机没有 Developer ID 证书，app 是 adhoc 签名的。
# 下载方首次打开会被 Gatekeeper 拦，处理办法写在仓库 README 的「下载」一节。
set -e
cd "$(dirname "$0")"

APP="build/Volfree.app"
[ -d "$APP" ] || { echo "缺少 $APP，先跑 ./build.sh" >&2; exit 1; }

VER=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist")
OUT="build/Volfree-$VER.dmg"

# 暂存目录：放 app 和指向 /Applications 的软链，用户拖进去就行
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$OUT"
hdiutil create -volname "Volfree $VER" -srcfolder "$STAGE" -ov -format UDZO -quiet "$OUT"

echo "完成：$PWD/$OUT"
echo "体积：$(du -h "$OUT" | cut -f1)"
