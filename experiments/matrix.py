#!/usr/bin/env python3
"""从 volfree 的真实实现里穷举判定表（docs/判定情况全表.md 第 5 节的来源）。

不靠读代码推断，而是构造 Handle / Blocker 组合，直接调用 volfree.assess()，
把「每种情况 → 当前给出什么等级、什么理由」打出来，并与「应为」对照。

用法：python3 experiments/matrix.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import volfree as V  # noqa: E402


def H(fd="3", access="r", ftype="REG", name="/Volumes/D/a.bin",
      exists=True, link_count=1, mapped=None):
    return V.Handle(fd=fd, access=access, ftype=ftype, name=name,
                    exists=exists, link_count=link_count,
                    mapped_writable=mapped)


NAMED_RO = lambda: H("3", "r", "REG", "/Volumes/D/doc.psd", True, 1)
NAMED_W = lambda: H("3", "u", "REG", "/Volumes/D/doc.psd", True, 1)
UNLINK_RO = lambda: H("6", "r", "REG", "/Volumes/D/tmp.bin", False, 0)
UNLINK_W = lambda: H("6", "u", "REG", "/Volumes/D/tmp.bin", False, 0)
MAP_W = lambda: H("txt", "", "REG", "/Volumes/D/map.bin", True, 1, True)
MAP_RO = lambda: H("txt", "", "REG", "/Volumes/D/map.bin", True, 1, False)
MAP_UNK = lambda: H("txt", "", "REG", "/Volumes/D/map.bin", True, 1, None)
MAP_W_DEL = lambda: H("txt", "", "REG", "/Volumes/D/map.bin", False, 0, True)
MAP_RO_DEL = lambda: H("txt", "", "REG", "/Volumes/D/map.bin", False, 0, False)
MAP_UNK_DEL = lambda: H("txt", "", "REG", "/Volumes/D/map.bin", False, 0, None)
CWD = lambda: H("cwd", "r", "DIR", "/Volumes/D", True, 1)
SOCK = lambda: H("7", "u", "IPv4", "->0x1234", None, None)

EXE_APP = "/Applications/Final Cut Pro.app/Contents/MacOS/Final Cut Pro"
EXE_SYS = "/System/Library/CoreServices/SomeHelper"
EXE_APPEX = "/System/Library/QuickLook/Preview.appex/Contents/MacOS/Preview"
EXE_PROT = "/System/Library/CoreServices/WindowServer"

# (说明, 句柄, exe, 应为)
CASES = [
    ("只读·有名字", [NAMED_RO()], EXE_APP, "CAUTION"),
    ("写入·有名字", [NAMED_W()], EXE_APP, "RISKY"),
    ("只读·无名字", [UNLINK_RO()], EXE_APP, "SAFE"),
    ("写入·无名字", [UNLINK_W()], EXE_APP, "CAUTION"),
    ("只读有名 + 只读无名", [NAMED_RO(), UNLINK_RO()], EXE_APP, "CAUTION"),
    ("写入有名 + 写入无名", [NAMED_W(), UNLINK_W()], EXE_APP, "RISKY"),
    ("写入有名 + 只读无名", [NAMED_W(), UNLINK_RO()], EXE_APP, "RISKY"),
    ("只读有名 + 写入无名", [NAMED_RO(), UNLINK_W()], EXE_APP, "CAUTION"),
    ("映射有名·可写", [MAP_W()], EXE_APP, "RISKY"),
    ("映射有名·只读", [MAP_RO()], EXE_APP, "CAUTION"),
    ("映射有名·未知", [MAP_UNK()], EXE_APP, "RISKY"),
    ("映射无名·可写", [MAP_W_DEL()], EXE_APP, "CAUTION"),
    ("映射无名·只读", [MAP_RO_DEL()], EXE_APP, "SAFE"),
    ("映射无名·未知", [MAP_UNK_DEL()], EXE_APP, "CAUTION"),
    ("映射有名·可写 + 只读 fd", [MAP_W(), NAMED_RO()], EXE_APP, "RISKY"),
    ("映射有名·只读 + 只读 fd", [MAP_RO(), NAMED_RO()], EXE_APP, "CAUTION"),
    ("映射有名 + 写入 fd", [MAP_RO(), NAMED_W()], EXE_APP, "RISKY"),
    ("cwd 在卷上", [CWD()], "/bin/zsh", "CAUTION"),
    ("只有伪句柄（socket）", [SOCK()], EXE_APP, "CAUTION"),
    ("只读 + 系统辅助进程", [NAMED_RO()], EXE_SYS, "CAUTION"),
    ("只读 + 可重启扩展", [NAMED_RO()], EXE_APPEX, "SAFE"),
    ("写入 + 受保护进程", [NAMED_W()], EXE_PROT, "NEVER"),
]

print("=" * 120)
print("A. 句柄层真值表")
print("=" * 120)
print("%-10s %-7s %-11s %-9s %-12s" % ("link_count", "exists", "is_deleted", "access", "write_state"))
for lk, ex in [(0, True), (0, False), (1, False), (1, True), (2, True), (None, False), (None, None)]:
    h = H(exists=ex, link_count=lk, access="r")
    print("%-10s %-7s %-11s %-9s %-12s" % (lk, ex, h.is_deleted, "r", h.write_state))
print()
print("%-22s %-9s %-16s %s" % ("句柄", "access", "mapped_writable", "write_state"))
for label, h in [
    ("普通 fd 只读", H("3", "r", "REG", "/a", True, 1)),
    ("普通 fd 只写", H("3", "w", "REG", "/a", True, 1)),
    ("普通 fd 读写", H("3", "u", "REG", "/a", True, 1)),
    ("普通 fd 空 access", H("3", "", "REG", "/a", True, 1)),
    ("映射 可写", MAP_W()),
    ("映射 只读", MAP_RO()),
    ("映射 未知", MAP_UNK()),
    ("cwd", H("cwd", "", "DIR", "/d", True, 1)),
]:
    print("%-22s %-9s %-16s %s" % (label, repr(h.access), h.mapped_writable, h.write_state))

print()
print("=" * 120)
print("B. 进程层：每种句柄组合，volfree 当前给出的等级")
print("=" * 120)
print("%-26s %-7s %-8s %-7s %-8s %-8s %-9s %s" % (
    "情况", "has_w", "all_unl", "uncert", "only_r", "等级", "应为", "理由"))
print("-" * 120)

bad = 0
for label, handles, exe, should in CASES:
    b = V.Blocker(pid=9000, command="X", exe=exe, handles=handles)
    V.assess(b, self_pid=1)
    ok = b.level == should
    if not ok:
        bad += 1
    print("%-26s %-7s %-8s %-7s %-8s %-8s %-9s %s%s" % (
        label,
        "是" if b.has_write else "否",
        "是" if b.all_writes_unlinked else "否",
        "是" if b.has_uncertain_write else "否",
        "是" if b.only_read else "否",
        b.level, should, b.reason[:30],
        "" if ok else "  ← 不符"))

print()
print("共 %d 例，不符 %d 例" % (len(CASES), bad))
print()
print("=" * 120)
print("C. assess() 的判定路径顺序（先命中先返回）")
print("=" * 120)
for line in [
    "A1  pid∈{0,1,自身} 或 exe∈PROTECTED_EXECUTABLES        → NEVER",
    "A1b uid≠当前用户 且无写入（后置改写，不覆盖 RISKY）      → NEVER",
    "A2  has_write 且 全部可写句柄都无名                      → CAUTION",
    "A2b has_write 且 存在可写性未知的句柄                    → RISKY（按最坏情况）",
    "A3  has_write                                           → RISKY",
    "A4  all_deleted                                         → SAFE",
    "A5  exe 命中 RESTARTABLE_APPEX_HINTS                    → SAFE",
    "A6  only_read 且 exe 以 SYSTEM_HELPER_HINTS 开头         → CAUTION",
    "A7  only_read 且有 cwd 句柄                              → CAUTION",
    "A8  only_read                                           → CAUTION",
    "A9  兜底                                                → CAUTION",
]:
    print("  " + line)
