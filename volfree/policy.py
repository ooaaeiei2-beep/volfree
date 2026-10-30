"""定级规则。规则写成数据表，顺序即优先级；这里不含任何 IO。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from .models import Blocker


# 绝不允许结束的进程（按可执行路径尾部匹配）
PROTECTED_EXECUTABLES = {
    "/sbin/launchd",
    "/usr/libexec/launchd",
    "/System/Library/CoreServices/WindowServer",
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/Metadata.framework/Support/mds",
}


# 系统级辅助进程：终止后由 launchd 自动重启，通常无害，但可能影响系统功能
SYSTEM_HELPER_HINTS = ("/System/Library/", "/usr/libexec/", "/usr/sbin/")


# 会被系统按需重启的 UI 辅助类扩展（缩略图 / QuickLook / Finder 扩展）
RESTARTABLE_APPEX_HINTS = (".appex/", "QuickLook", "Thumbnail", "Thumb")


@dataclass(frozen=True)
class Rule:
    """一条定级规则：条件 → (等级, 理由)。

    `condition` 是给人看的条件摘要，`experiments/matrix.py` 直接打印它，
    所以文档里的判定表与本表同源，不会各写一份然后悄悄漂移。
    """

    name: str
    condition: str
    level: str
    reason: str
    when: Callable[[Blocker, int], bool]


def _has_protected_identity(b: Blocker, self_pid: int) -> bool:
    return b.pid in (0, 1, self_pid) or b.exe in PROTECTED_EXECUTABLES


def _is_restartable_appex(b: Blocker, self_pid: int) -> bool:
    return any(hint in b.exe for hint in RESTARTABLE_APPEX_HINTS)


def _is_system_helper_readonly(b: Blocker, self_pid: int) -> bool:
    return b.only_read and any(b.exe.startswith(p) for p in SYSTEM_HELPER_HINTS)


def _has_cwd_handle(b: Blocker, self_pid: int) -> bool:
    return any(h.fd == "cwd" for h in b.handles)


ASSESS_RULES: tuple[Rule, ...] = (
    Rule("A1", "pid∈{0,1,自身} 或 exe∈PROTECTED_EXECUTABLES", "NEVER",
         "系统核心进程或本工具自身，不可终止",
         _has_protected_identity),
    # A2 必须排在 A2b 之前：全部可写句柄都无名时，即使其中有 unknown，
    # 也不存在会被截断的有名字文件，CAUTION 成立。
    # 反过来（A2b 在前）会把「映射无名·未知」误升为 RISKY。
    Rule("A2", "has_write 且 all_writes_unlinked", "CAUTION",
         "正在写一个已被删除的文件；终止会放弃这次写入，"
         "且该文件已无名字、无法从别处找回（不会损坏其他文件）",
         lambda b, p: b.has_write and b.all_writes_unlinked),
    Rule("A2b", "has_write 且 has_uncertain_write", "RISKY",
         "有句柄的读写状态无法确认（可能是内存映射写入），"
         "按最坏情况处理：终止可能留下截断或半写的文件",
         lambda b, p: b.has_write and b.has_uncertain_write),
    Rule("A3", "has_write", "RISKY",
         "持有写入句柄，终止可能留下截断或半写的文件",
         lambda b, p: b.has_write),
    Rule("A4", "all_deleted", "SAFE",
         "句柄指向的文件已删除且为只读，终止不会丢失任何数据",
         lambda b, p: b.all_deleted),
    Rule("A5", "exe 命中 RESTARTABLE_APPEX_HINTS", "SAFE",
         "系统按需重启的预览/缩略图扩展，终止后自动重建",
         _is_restartable_appex),
    Rule("A6", "only_read 且 exe 以 SYSTEM_HELPER_HINTS 开头", "CAUTION",
         "系统辅助进程持只读句柄，终止可能影响系统功能",
         _is_system_helper_readonly),
    Rule("A7", "only_read 且有 cwd 句柄", "CAUTION",
         "工作目录位于该卷上，离开该目录或结束该终端即可释放",
         lambda b, p: b.only_read and _has_cwd_handle(b, p)),
    Rule("A8", "only_read", "CAUTION",
         "持只读句柄，终止可能丢失该程序未保存的界面状态",
         lambda b, p: b.only_read),
)


# A9：没有任何规则命中时的兜底
FALLBACK_LEVEL = "CAUTION"


FALLBACK_REASON = "占用状态无法完全判定，建议先尝试正常退出该程序"


def _assess_level(blocker: Blocker, self_pid: Optional[int] = None) -> Blocker:
    """给单个占用进程定级并给出理由。

    判据是二维的：文件是否还有名字（link 状态）× 句柄是否可写。
    四格分别对应：
        有名字 + 可写  → RISKY   可能留下截断/半写的现存文件
        无名字 + 可写  → CAUTION 会放弃进行中的写入，但不损坏现存文件
        有名字 + 只读  → CAUTION 只丢程序内存态
        无名字 + 只读  → SAFE    数据已不可达，无写入，终止零损失
    优先级：受保护进程 > 上述四格 > 系统可重启扩展 > 系统辅助 > cwd > 其余。
    具体规则见 `ASSESS_RULES`（顺序即优先级）。
    """
    self_pid = self_pid or os.getpid()
    for rule in ASSESS_RULES:
        if rule.when(blocker, self_pid):
            blocker.level = rule.level
            blocker.reason = rule.reason
            return blocker
    blocker.level = FALLBACK_LEVEL
    blocker.reason = FALLBACK_REASON
    return blocker


def _apply_uid_policy(b: Blocker) -> Blocker:
    """标注「属于其他用户」的进程——本工具无权终止，不必等到 EPERM 才发现。

    刻意**不覆盖已有的写入等级**：正在写的进程必须继续显示为高风险，
    否则会被读成「反正杀不掉，那就可以直接拔」——这正是产品要避免的误读。
    只有「无写入」的情形才改判 NEVER：既没有数据风险、又无权终止，如实说明。
    """
    if b.uid < 0 or b.uid == os.getuid():
        return b
    b.other_user = True
    if b.has_write:
        b.reason = (f"{b.reason}；但该进程属于其他用户（uid {b.uid}），"
                    "本工具无权终止，需以 root 权限处理")
        return b
    b.level = "NEVER"
    b.reason = (f"该进程属于其他用户（uid {b.uid}），本工具无权终止；"
                "如需释放需以 root 权限处理")
    return b


def assess(blocker: Blocker, self_pid: Optional[int] = None) -> Blocker:
    """定级 + 权限标注。判定语义见 `_assess_level`。"""
    return _apply_uid_policy(_assess_level(blocker, self_pid))
