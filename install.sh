#!/bin/sh
# Volfree 一行安装：
#   curl -fsSL https://raw.githubusercontent.com/ooaaeiei2-beep/volfree/main/install.sh | sh
#
# 做四件事：下载最新 dmg → 装进 /Applications → 去掉隔离属性 → 启动。
# 去隔离属性是必需的：app 是 adhoc 签名的，不去掉会被 Gatekeeper 拦下。
#
# 不想直接管道执行的话，可以先看一眼：
#   curl -fsSL https://raw.githubusercontent.com/ooaaeiei2-beep/volfree/main/install.sh | less
set -eu

REPO="ooaaeiei2-beep/volfree"
APP="Volfree"
DEST="/Applications/$APP.app"

say() { printf '%s\n' "$*" >&2; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "这个脚本只支持 macOS"

# 1. 取最新版本号。走 releases/latest 的 302 跳转，不调 API——避免撞未登录的 60 次/小时限流。
TAG=$(curl -fsSI "https://github.com/$REPO/releases/latest" 2>/dev/null \
      | tr -d '\r' \
      | sed -n 's|^[Ll]ocation: .*/tag/\(.*\)$|\1|p' \
      | head -1)
[ -n "${TAG:-}" ] || die "拿不到最新版本号，检查网络或代理"
VER=${TAG#v}
say "最新版本：$TAG"

# 2. 下载
TMP=$(mktemp -d)
MOUNT="$TMP/mnt"
cleanup() {
    hdiutil detach "$MOUNT" -quiet 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

DMG="$TMP/Volfree-$VER.dmg"
URL="https://github.com/$REPO/releases/download/$TAG/Volfree-$VER.dmg"
say "下载 $URL"
curl -fL --progress-bar -o "$DMG" "$URL" || die "下载失败"

# 3. 挂载
mkdir -p "$MOUNT"
hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$MOUNT" -quiet || die "dmg 挂载失败"
[ -d "$MOUNT/$APP.app" ] || die "dmg 里没有 $APP.app"

# 4. 装进 /Applications
if [ -e "$DEST" ]; then
    say "检测到已安装的版本，先退出再覆盖"
    # 只用 pkill。不要用 `osascript -e 'tell application ... to quit'`：
    # 这个 app 不可脚本化，AppleScript 会一直等回应，整个脚本就挂在那里（实测踩过）。
    pkill -f "$DEST/Contents/MacOS/$APP" 2>/dev/null || true
    i=0
    while pgrep -f "$DEST/Contents/MacOS/$APP" >/dev/null 2>&1 && [ "$i" -lt 25 ]; do
        sleep 0.2
        i=$((i + 1))
    done
    pkill -9 -f "$DEST/Contents/MacOS/$APP" 2>/dev/null || true
    sleep 0.3
    rm -rf "$DEST" || die "删不掉旧版本 $DEST"
fi

say "安装到 $DEST"
if ! cp -R "$MOUNT/$APP.app" /Applications/ 2>/dev/null; then
    die "/Applications 不可写。用 sudo 重跑，或手动把 app 拖进去"
fi

# 5. 去隔离属性——不去掉的话首次打开会被 Gatekeeper 拦下
xattr -cr "$DEST" 2>/dev/null || true

# 6. 校验并启动
if ! codesign -vvv "$DEST" >/dev/null 2>&1; then
    say "警告：签名校验未通过，app 可能无法启动"
fi
say "完成。正在启动 $APP"
# 必须把标准输入输出都断开再 open：否则 app 会继承调用方的文件描述符，
# `curl … | sh` 这种管道会因为写端一直被 app 握着而不结束（实测踩过）。
open "$DEST" </dev/null >/dev/null 2>&1
