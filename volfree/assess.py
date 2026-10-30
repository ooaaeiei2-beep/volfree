"""把扫描结果补全进程元信息后送进 policy 定级，并组装成一次完整诊断。"""

from __future__ import annotations

from typing import Iterable, Optional

from .models import LEVEL_ORDER, Blocker, Volume
from .policy import assess
from .proc import proc_executable_path, proc_start_time, proc_uid
from .scan import EnvClue, collect_clues, detect_access_level, scan_blockers


def enrich_and_assess(blockers: Iterable[Blocker]) -> list[Blocker]:
    """补全进程元信息后定级，按风险从高到低排序。"""
    result = []
    for b in blockers:
        b.exe = proc_executable_path(b.pid)
        b.start_time = proc_start_time(b.pid)
        b.uid = proc_uid(b.pid)
        result.append(assess(b))
    result.sort(key=lambda x: (-LEVEL_ORDER.get(x.level, 2), x.pid))
    return result


def diagnose(vol: Volume) -> tuple[list[Blocker], list[str], tuple[str, str], list[EnvClue]]:
    """完整诊断：扫描 + 定级 + 权限判定 + 环境线索。"""
    blockers, warnings = scan_blockers(vol)
    blockers = enrich_and_assess(blockers)
    return blockers, warnings, detect_access_level(), collect_clues(vol)
