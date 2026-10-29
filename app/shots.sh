#!/bin/sh
# 重渲 shots/ 里的真机截图。
#
# 为什么要脚本：这些图不是设计稿，是 VOLFREE_RENDER 把真实视图渲染出来的，
# 而每张图都需要一块处于特定状态的卷（没进程 / 只读持有 / 放弃写入 / 正在写）。
# 手工摆一遍容易漏图，也容易把临时卷忘在 /Volumes 里。
#
# 跑法：./shots.sh
#   会建一块 200MB 临时 dmg 挂在 /Volumes/VfShot，退出时（含失败）自动卸载并删除。
#   持有进程全部由 experiments/writer.py 提供，不引入别的装置。
set -eu
cd "$(dirname "$0")"

VOL=VfShot
MNT="/Volumes/$VOL"
DMG=/tmp/vf_shot.dmg
BIN=build/Volfree.app/Contents/MacOS/Volfree
PY=/usr/bin/python3
OUT=shots

PIDS=""

cleanup() {
    # 这里必须 set +e：收摊途中任何一步失败都不能让后面的清理被跳过，
    # 否则会留下一块挂着的卷和一个 200MB 的 dmg。
    set +e
    for p in $PIDS; do kill "$p" 2>/dev/null; done
    if [ -d "$MNT" ]; then
        hdiutil detach "$MNT" -force -quiet 2>/dev/null
    fi
    rm -f "$DMG"
    rmdir "$MNT" 2>/dev/null
}
trap cleanup EXIT INT TERM

[ -x "$BIN" ] || { echo "先跑 ./build.sh" >&2; exit 1; }
mkdir -p "$OUT"

# 上一轮如果没收拾干净，先拆掉
if [ -d "$MNT" ]; then
    hdiutil detach "$MNT" -force -quiet 2>/dev/null || true
fi

echo "==> 建卷"
hdiutil create -size 200m -fs APFS -volname "$VOL" -type UDIF "$DMG" -ov -quiet
hdiutil attach -nobrowse "$DMG" >/dev/null
# hdiutil 的 deprecation 警告走 stderr，这里不管它

# 造一块「有人开着」的文件
echo "hello" > "$MNT/doc.psd"

start_holder() {
    # $1 = writer.py 模式，$2 = 持有秒数
    $PY ../experiments/writer.py "$MNT/doc.psd" "$1" "$2" >/dev/null 2>&1 &
    PIDS="$PIDS $!"
    # 等它真的把句柄拿到手，否则渲染出来是空的
    sleep 1.5
}

stop_holders() {
    set +e
    for p in $PIDS; do kill "$p" 2>/dev/null; done
    PIDS=""
    wait 2>/dev/null
    # 等 lsof 那边彻底看不到，否则下一张会串进上一张的进程
    sleep 1
    set -e
}

render() {
    # $1 = 输出文件，其余 = 追加的环境变量
    out="$1"; shift
    env "$@" VOLFREE_RENDER="$PWD/$OUT/$out" "$BIN" 2>/dev/null
    echo "    $out"
}

echo "==> 01 绿（卷上没人）"
render "01-绿.png"

echo "==> 02 黄（只读持有）"
start_holder hold_ro 60
render "02-黄.png"
stop_holders

echo "==> 03 黄·放弃写入（写已删除的临时文件）"
start_holder mmap_close_unlink 60
render "03-黄-放弃写入.png"
stop_holders

echo "==> 04 红（正在写）"
start_holder bulk 60
# 速率要两个样本才出得来，3.2s 只够采到一次
render "04-红.png" VOLFREE_RENDER_DELAY=6
stop_holders

echo "==> 05 黄·展开进程"
start_holder hold_ro 60
render "05-黄-展开进程.png" VOLFREE_EXPAND=1
stop_holders

echo "==> 06 绿·权限受限 + 设置窗口"
render "06a-绿-面板.png"
render "06b-设置.png" VOLFREE_RENDER_SETTINGS=1

echo "==> 07 红·写入速率（收起 + 展开）"
start_holder bulk 90
render "07a-红-收起.png" VOLFREE_RENDER_DELAY=6
render "07b-红-展开.png" VOLFREE_RENDER_DELAY=6 VOLFREE_EXPAND=1
stop_holders

echo "==> 卸卷"
hdiutil detach "$MNT" -force -quiet
rm -f "$DMG"
rmdir "$MNT" 2>/dev/null || true

echo "==> 09 已推出 / 10 空态（都要没有卷）"
render "09-已推出.png" VOLFREE_SIMULATE_EJECT=移动硬盘
render "10-空态.png"

echo "==> 拼对照图"
# 系统自带的 /usr/bin/python3 没有 PIL，拼图要另找一个能 import PIL 的解释器。
# 找不到就只跳过拼图——底图已经渲好了，不该因为这一步让整轮白跑。
PY_PIL=""
for c in "${VOLFREE_PYTHON_PIL:-}" python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -n "$c" ] && "$c" -c "import PIL" 2>/dev/null; then
        PY_PIL="$c"
        break
    fi
done
if [ -n "$PY_PIL" ]; then
    "$PY_PIL" compose_shots.py
else
    echo "    跳过：没找到装了 PIL 的解释器（可用 VOLFREE_PYTHON_PIL 指定）" >&2
fi

echo
echo "完成。临时卷已卸载。"
