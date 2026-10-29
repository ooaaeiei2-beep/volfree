#!/bin/sh
# 造一块「正被写入」的测试盘，用来亲眼看面板的红态与写入速率。
#
#   ./demo.sh           造一块盘并开始写入，60 秒后自动收摊
#   ./demo.sh 180       写 180 秒
#
# 全程只碰 /tmp 下的临时 dmg，不动任何真实磁盘。
# 退出（含 Ctrl-C）时会把测试盘推出并删掉。

HOLD="${1:-60}"
DMG=/tmp/volfree-demo.dmg
VOL=/Volumes/VfDemo
PY="${VOLFREE_PYTHON:-/usr/bin/python3}"
HERE=$(cd "$(dirname "$0")" && pwd)
WRITER_PID=

# 收摊必须能跑完，所以这里关掉 set -e：
# 前一步失败（比如进程已经自己退了）不该让后一步不执行。
cleanup() {
    set +e
    echo
    echo "==> 收摊"
    [ -n "$WRITER_PID" ] && kill "$WRITER_PID" 2>/dev/null
    WRITER_PID=
    hdiutil detach "$VOL" -force >/dev/null 2>&1
    rm -f "$DMG"
    echo "    已推出并删除 $VOL"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

set -eu

echo "==> 建测试卷（200 MB，exFAT）"
rm -f "$DMG"
hdiutil create -size 200m -fs ExFAT -volname VfDemo -type UDIF "$DMG" >/dev/null 2>&1
hdiutil attach -nobrowse "$DMG" >/dev/null 2>&1
echo "    已挂载 $VOL"

# bulk 模式：普通 write，fd 一直开着、文件有名 —— 最典型的红态，
# 且每轮 256 KB，够让面板显示出写入速率。
"$PY" "$HERE/../experiments/writer.py" "$VOL/busy.bin" bulk "$HOLD" &
WRITER_PID=$!
sleep 1

cat <<EOF

    一块盘正在被写（${HOLD} 秒）。
    点菜单栏的硬盘图标，应该看到红色「先别拔」，并带写入速率。

    想试「结束并推出」：在面板里点「仍要结束…」展开进程列表。
    提前结束按 Ctrl-C。

EOF

wait "$WRITER_PID" 2>/dev/null
WRITER_PID=
echo
echo "==> 写入已停，面板应变绿（点面板里的「重新检查」刷新）"
sleep 3
