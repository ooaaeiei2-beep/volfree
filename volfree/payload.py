"""JSON 序列化。界面只读这里的机器字段，绝不解析 reason 里的中文。"""

from __future__ import annotations

import json
import os
from typing import Optional

from .models import VERSION, Blocker, Volume
from .proc import proc_diskio_written


def classify_kind(b: Blocker) -> str:
    exe = b.exe
    if ".appex/" in exe:
        return "系统扩展"
    if exe.startswith("/System/Library/CoreServices/Finder.app"):
        return "访达"
    base = os.path.basename(exe).lower()
    # 解释器与 shell 优先于路径判定：/usr/bin/python3 更可能是用户脚本而非守护进程
    if base.startswith(("python", "node", "ruby", "perl", "php")):
        return "脚本进程"
    if base in ("zsh", "bash", "fish", "sh", "tcsh", "ksh", "dash") or \
            b.command in ("Terminal", "iTerm2"):
        return "终端"
    if exe.startswith(("/System/", "/usr/", "/bin/", "/sbin/")):
        return "系统进程"
    if "/Applications/" in exe:
        return "用户应用"
    return "其他"


def blocker_payload(b: Blocker) -> dict:
    """单个占用进程的机器可读描述。

    `flags` 是给界面用的：界面不该去解析 `reason` 里的中文来判断「是不是在放弃写入」。
    判定结果只有一处来源（assess），界面只负责把布尔翻成文案。
    """
    return {
        "pid": b.pid, "command": b.command, "exe": b.exe, "uid": b.uid,
        "start_time": b.start_time, "level": b.level, "reason": b.reason,
        # 累计写盘字节数。界面用两次采样的差商算速率——
        # 只有「正在写」的进程才有意义，其余一律给 None，免得界面把
        # 启动时的读盘算成写入。
        "diskio_written": proc_diskio_written(b.pid) if b.has_write else None,
        "flags": {
            "has_write": b.has_write,
            "all_writes_unlinked": b.all_writes_unlinked,
            "has_unlinked_write": b.has_unlinked_write,
            "has_uncertain_write": b.has_uncertain_write,
            "all_deleted": b.all_deleted,
            "only_read": b.only_read,
            # 属于其他用户：界面据此提示「需 root 权限」，而不是把按钮点出一个失败
            "other_user": b.other_user,
        },
        "handles": [
            {"fd": h.fd, "access": h.access, "type": h.ftype,
             "name": h.name, "exists": h.exists,
             "write_state": h.write_state}
            for h in b.handles
        ],
    }


def volume_payload(vol: Volume, blockers: list[Blocker], warnings: list[str],
                   access: tuple[str, str] = ("unknown", ""),
                   clues: Optional[list[EnvClue]] = None) -> dict:
    counts = {"NEVER": 0, "RISKY": 0, "CAUTION": 0, "SAFE": 0}
    for b in blockers:
        counts[b.level] = counts.get(b.level, 0) + 1
    return {
        "version": VERSION,
        "volume": {
            "device": vol.device, "mountpoint": vol.mountpoint, "name": vol.display,
            "fstype": vol.fstype, "external": vol.external, "virtual": vol.virtual,
            "ejectable": vol.ejectable, "removable": vol.removable,
            "total_bytes": vol.total_bytes, "free_bytes": vol.free_bytes,
        },
        "access": {"level": access[0], "description": access[1]},
        "summary": {
            "total": len(blockers),
            "never": counts["NEVER"], "risky": counts["RISKY"],
            "caution": counts["CAUTION"], "safe": counts["SAFE"],
            "writing": sum(1 for b in blockers if b.has_write),
            # 会被放弃的写入次数：界面据此决定主按钮要不要先软确认
            "abandoned_writes": sum(1 for b in blockers if b.all_writes_unlinked),
            # 扫描是否完整。False 时界面必须降级——空列表不等于「没有占用」
            "complete": vol.scan_complete,
        },
        "blockers": [blocker_payload(b) for b in blockers],
        "clues": [{"label": c.label, "detail": c.detail, "warn": c.warn} for c in (clues or [])],
        "warnings": warnings,
    }


def blockers_to_json(vol: Volume, blockers: list[Blocker], warnings: list[str],
                     access: tuple[str, str] = ("unknown", ""),
                     clues: Optional[list[EnvClue]] = None) -> str:
    payload = volume_payload(vol, blockers, warnings, access, clues)
    return json.dumps(payload, ensure_ascii=False, indent=2)
