"""volfree —— 移动硬盘「现在能不能安全拔」诊断工具。

源码按职责拆在本包里：

    models.py   数据结构与常量（Volume / Handle / Blocker）
    proc.py     libproc 的 ctypes 封装
    scan.py     mount / diskutil / lsof 的调用与解析
    policy.py   定级规则（数据表，顺序即优先级）
    assess.py   补全进程元信息 + 组装一次完整诊断
    payload.py  JSON 序列化
    render.py   终端渲染
    cli.py      参数解析与命令分发

**分发物不是这个包，而是仓库根的 `volfree.py`。** 它由 `tools/bundle.py` 把本包拼成
单文件，app（`Bundle.main.resourceURL/volfree.py`）与 `curl | sh` 都只认那个单文件。
改完本包记得重新生成，否则分发物会落后——`tests` 里有一条漂移守卫盯着这件事。
"""

from .assess import diagnose, enrich_and_assess
from .cli import (
    ask,
    build_parser,
    cmd_analyze,
    cmd_doctor,
    cmd_eject,
    cmd_end,
    cmd_list,
    cmd_release,
    cmd_status,
    cmd_watch,
    main,
    pid_holds_volume,
    pick_volume_interactive,
    release_volume,
    send_signal,
    verify_identity,
)
from .models import LEVEL_LABEL, LEVEL_ORDER, VERSION, Blocker, Handle, Volume
from .payload import blocker_payload, blockers_to_json, classify_kind, volume_payload
from .policy import (
    ASSESS_RULES,
    FALLBACK_LEVEL,
    FALLBACK_REASON,
    PROTECTED_EXECUTABLES,
    RESTARTABLE_APPEX_HINTS,
    SYSTEM_HELPER_HINTS,
    Rule,
    assess,
)
from .proc import (
    proc_diskio_written,
    proc_executable_path,
    proc_start_time,
    proc_uid,
    scan_writable_mappings,
)
from .render import (
    Palette,
    describe_handle,
    disp_width,
    human_size,
    pad,
    render_report,
)
from .scan import (
    FDA_PROBE_FILES,
    LSOF_OUTPUT_LIMIT,
    EnvClue,
    _parent_info_cache,
    _volume_flags,
    capture_bounded,
    collect_clues,
    detect_access_level,
    list_volumes,
    parse_lsof_field_output,
    parse_mount_output,
    path_exists,
    read_volume_info,
    resolve_volume,
    run,
    scan_blockers,
)

__all__ = [
    "VERSION", "LEVEL_ORDER", "LEVEL_LABEL",
    "Volume", "Handle", "Blocker", "EnvClue", "Rule",
    "assess", "enrich_and_assess", "diagnose",
    "ASSESS_RULES", "FALLBACK_LEVEL", "FALLBACK_REASON",
    "PROTECTED_EXECUTABLES", "SYSTEM_HELPER_HINTS", "RESTARTABLE_APPEX_HINTS",
    "scan_blockers", "parse_lsof_field_output", "parse_mount_output",
    "path_exists", "capture_bounded", "LSOF_OUTPUT_LIMIT",
    "list_volumes", "resolve_volume", "read_volume_info",
    "detect_access_level", "collect_clues", "FDA_PROBE_FILES", "run",
    "proc_executable_path", "proc_start_time", "proc_uid",
    "scan_writable_mappings", "proc_diskio_written",
    "blocker_payload", "volume_payload", "blockers_to_json", "classify_kind",
    "Palette", "render_report", "human_size", "disp_width", "pad", "describe_handle",
    "verify_identity", "pid_holds_volume", "send_signal", "release_volume",
    "pick_volume_interactive", "ask",
    "cmd_list", "cmd_analyze", "cmd_release", "cmd_status", "cmd_watch",
    "cmd_end", "cmd_eject", "cmd_doctor", "build_parser", "main",
]
