"""IO 层：调用 mount / diskutil / lsof，把输出解析成 Blocker。

纯解析函数（parse_mount_output / parse_lsof_field_output）与 IO 分开，便于单独测试。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable, Optional

from .models import Blocker, Handle, Volume
from .proc import scan_writable_mappings


def run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """执行子进程，返回 (returncode, stdout, stderr)。失败不抛异常。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时：{' '.join(cmd)}"
    except FileNotFoundError:
        return 127, "", f"命令不存在：{cmd[0]}"
    except OSError as exc:
        return 1, "", str(exc)


# lsof 字段输出的体积上限。几十万文件的挂载点足以把 subprocess.run 的
# 全量缓冲吃满内存；超限即截断并降级结论，绝不当作完整扫描。
LSOF_OUTPUT_LIMIT = 32 * 1024 * 1024


def capture_bounded(cmd: list[str], limit: int = LSOF_OUTPUT_LIMIT,
                    timeout: float = 30.0) -> tuple[int, bytes, str, bool, bool]:
    """执行子进程并限制 stdout 体积。

    返回 (returncode, stdout, stderr, truncated, timed_out)。
    `subprocess.run` 会把输出整段读进内存，没有上限；这里改成按块读，
    越过 `limit` 立即杀掉子进程并置 truncated，调用方必须据此降级结论。

    超时用 `wait(timeout=)` 单独看住——子进程若在写满管道后卡住，
    单靠读循环里的时间戳判断不出来。
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = bytearray()
    err = bytearray()
    state = {"truncated": False}

    def read_stdout() -> None:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                return
            room = limit - len(out)
            if len(chunk) > room:
                out.extend(chunk[:room])
                state["truncated"] = True
                try:
                    proc.kill()
                except OSError:
                    pass
                return
            out.extend(chunk)

    def read_stderr() -> None:
        assert proc.stderr is not None
        try:
            err.extend(proc.stderr.read())
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=read_stdout, daemon=True),
               threading.Thread(target=read_stderr, daemon=True)]
    for t in readers:
        t.start()

    timed_out = False
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except OSError:
            pass
        code = proc.wait()

    for t in readers:
        t.join(1.0)
    for pipe in (proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass
    return code, bytes(out), err.decode("utf-8", "replace"), state["truncated"], timed_out


# ────────────────────────────── 卷枚举 ──────────────────────────────
_MOUNT_RE = re.compile(r"^(/dev/\S+) on (.+) \(([^)]*)\)$")


# 不参与诊断的系统挂载点前缀
_SYSTEM_MOUNT_PREFIXES = (
    "/System/Volumes/",
    "/Volumes/Recovery",
    "/private/var/",
    "/dev",
)


def parse_mount_output(text: str) -> list[tuple[str, str, str]]:
    """从 mount 输出解析出 (device, mountpoint, options)，过滤系统卷。

    挂载点可能含空格与中文，故用正则的非贪婪匹配 + 结尾括号锚定。
    """
    result = []
    for line in text.splitlines():
        m = _MOUNT_RE.match(line.strip())
        if not m:
            continue
        device, mountpoint, opts = m.group(1), m.group(2), m.group(3)
        if mountpoint == "/" or mountpoint.startswith(_SYSTEM_MOUNT_PREFIXES):
            continue
        if not device.startswith("/dev/"):
            continue
        result.append((device, mountpoint, opts))
    return result


def read_volume_info(device: str) -> dict:
    """读取 diskutil info -plist 的结构化信息；失败返回空字典。"""
    code, out, _ = run(["diskutil", "info", "-plist", device])
    if code != 0 or not out:
        return {}
    try:
        return plistlib.loads(out.encode("utf-8"))
    except Exception:
        return {}


_parent_info_cache: dict[str, dict] = {}


# 分区设备名 → 整盘设备名。BSD 命名规则：disk4s1 属于 disk4。
# 有了这个就不用"先读卷级 plist 拿到 ParentWholeDisk，再去查整盘"，
# 两次 diskutil 可以同时发起。
_PARTITION_DEV_RE = re.compile(r"^(/dev/disk\d+)s\d+$")


def _derive_parent_device(device: str) -> Optional[str]:
    """从分区设备名推整盘设备名（/dev/disk4s1 → /dev/disk4）。推不出返回 None。"""
    m = _PARTITION_DEV_RE.match(device)
    return m.group(1) if m else None


def _fetch_volume_infos(devices: Iterable[str]) -> dict[str, dict]:
    """并发读取多个设备的 `diskutil info -plist`，返回 {设备: info}。

    单次 `diskutil info` 实测 125~190ms，而卷级与整盘级两次调用原本是**串行**的
    （要先读卷级 plist 才知道父盘是谁），单卷就要等约 270ms。
    整盘名其实能从设备名直接推出来，于是两次可以同时发起——单卷省掉约一半等待，
    多卷时按卷数线性叠加的收益更明显。

    只做并发，不改任何判定：每个设备拿到的仍是同一份 plist，
    失败仍返回空字典（与串行版一致）。
    """
    unique = list(dict.fromkeys(devices))
    if not unique:
        return {}
    if len(unique) == 1:
        return {unique[0]: read_volume_info(unique[0])}
    with ThreadPoolExecutor(max_workers=min(8, len(unique))) as pool:
        results = list(pool.map(read_volume_info, unique))
    return dict(zip(unique, results))


def _parent_info(device: str) -> dict:
    """读取整盘信息（带缓存，避免同一父盘被多个分区重复查询）。"""
    if device not in _parent_info_cache:
        _parent_info_cache[device] = read_volume_info(device)
    return _parent_info_cache[device]


def _volume_flags(info: dict) -> tuple[bool, bool, bool]:
    """从 diskutil info 结果推断 (external, ejectable, virtual)。

    实测 macOS 27 的 plist 已不再稳定提供 DeviceLocation，因此主判据改为：
      - Internal=false 且非虚拟介质  → 外接
      - VirtualOrPhysical=Virtual 或 MediaName="Disk Image" → 磁盘映像
    卷级 plist 常缺这些字段，需要回落到父盘（ParentWholeDisk）。
    """
    parent = info.get("ParentWholeDisk")
    pinfo = _parent_info(f"/dev/{parent}") if parent else {}

    def pick(key: str, default=None):
        val = info.get(key)
        if val is None or val == "":
            val = pinfo.get(key, default)
        return val

    internal = pick("Internal")
    vop = str(pick("VirtualOrPhysical", "") or "").lower()
    media_name = str(pick("MediaName", "") or "")
    location = str(pick("DeviceLocation", "") or "").lower()

    virtual = (vop == "virtual") or (media_name == "Disk Image")
    if location == "external":
        external = True
    elif location == "internal":
        external = False
    else:
        external = (internal is False) and not virtual

    ejectable = bool(pick("Ejectable", False))
    return external, ejectable, virtual


def list_volumes(include_internal: bool = False) -> list[Volume]:
    """枚举候选卷。默认只保留外接或可推出的卷。"""
    _parent_info_cache.clear()
    code, out, err = run(["mount"])
    if code != 0:
        raise RuntimeError(f"无法读取挂载表：{err}")

    entries = parse_mount_output(out)

    # 卷级与整盘级的 plist 一次性并发取回来，再走原来的判定。
    # 整盘名由设备名推出（disk4s1 → disk4），所以不必等卷级 plist 返回才发起第二次查询。
    wanted: list[str] = []
    for device, _, _ in entries:
        wanted.append(device)
        parent = _derive_parent_device(device)
        if parent:
            wanted.append(parent)
    fetched = _fetch_volume_infos(wanted)
    # 预填父盘缓存：后面 _volume_flags 里的 _parent_info 就直接命中，不会再串行查一次
    for device, _, _ in entries:
        parent = _derive_parent_device(device)
        if parent and parent in fetched:
            _parent_info_cache[parent] = fetched[parent]

    volumes: list[Volume] = []
    for device, mountpoint, opts in entries:
        info = fetched.get(device) or read_volume_info(device)
        fstype = str(info.get("TypeBundle") or info.get("FilesystemType") or "").lower()
        if not fstype:
            fstype = opts.split(",")[0].strip()

        external, ejectable, virtual = _volume_flags(info)
        removable = bool(info.get("RemovableMedia") or info.get("Removable"))
        # diskutil 缺失时（例如极简环境）退化为按路径判断
        if not info:
            external = mountpoint.startswith("/Volumes/")

        if not include_internal and not (external or ejectable):
            continue

        vol = Volume(
            device=device,
            mountpoint=mountpoint,
            fstype=fstype,
            external=external,
            ejectable=ejectable,
            removable=removable,
            virtual=virtual,
            volume_name=str(info.get("VolumeName") or ""),
            total_bytes=int(info.get("TotalSize") or 0),
            free_bytes=int(info.get("FreeSpace") or 0),
        )
        volumes.append(vol)
    return volumes


def resolve_volume(target: str, include_internal: bool = False) -> Optional[Volume]:
    """按卷名、挂载点或设备号匹配目标卷。"""
    volumes = list_volumes(include_internal=include_internal)
    t = target.strip()
    for v in volumes:
        if t in (v.device, v.mountpoint, v.display, os.path.basename(v.mountpoint)):
            return v
    # 宽松匹配：忽略大小写，允许子串
    tl = t.lower()
    for v in volumes:
        if tl in v.display.lower() or tl in v.mountpoint.lower():
            return v
    return None


# ────────────────────────────── lsof 解析 ──────────────────────────────
def parse_lsof_field_output(raw: bytes) -> list[Blocker]:
    """解析 `lsof -F pcnfa0t` 的字段输出。

    字段模式避免了解析对齐表格时的空格/中文截断问题。
    加 0 参数后 name 字段以 NUL 终止，其余字段仍以换行分隔。
    """
    blockers: list[Blocker] = []
    current: Optional[Blocker] = None
    current_handle: Optional[Handle] = None

    for chunk in raw.split(b"\0"):
        for line in chunk.split(b"\n"):
            if not line:
                continue
            key, val = line[:1], line[1:]
            if key == b"p":
                try:
                    current = Blocker(pid=int(val), command="")
                except ValueError:
                    current = None
                if current:
                    blockers.append(current)
                    current_handle = None
            elif current is None:
                continue
            elif key == b"c":
                current.command = val.decode("utf-8", "replace")
            elif key == b"f":
                current_handle = Handle(fd=val.decode("utf-8", "replace"))
                current.handles.append(current_handle)
            elif key == b"a" and current_handle is not None:
                current_handle.access = val.decode("utf-8", "replace").strip()
            elif key == b"t" and current_handle is not None:
                current_handle.ftype = val.decode("utf-8", "replace").strip()
            elif key == b"n" and current_handle is not None:
                current_handle.name = val.decode("utf-8", "replace")
            elif key == b"k" and current_handle is not None:
                try:
                    current_handle.link_count = int(val.strip())
                except ValueError:
                    current_handle.link_count = None
    return [b for b in blockers if b.handles]


def scan_blockers(volume: Volume) -> tuple[list[Blocker], list[str]]:
    """扫描卷上的占用进程。返回 (占用列表, 告警信息)。"""
    warnings: list[str] = []
    # +c 0：不截断命令名（lsof 默认只保留 9 个字符，长名会被截断）
    # 字段 k：link count，0 表示已 unlink（APFS 可靠，exFAT 不可靠）
    cmd = ["lsof", "-nP", "+c", "0", "-F", "pcnfa0tk", volume.mountpoint]
    try:
        code, raw, err, truncated, timed_out = capture_bounded(
            cmd, LSOF_OUTPUT_LIMIT, 30.0)
    except FileNotFoundError:
        volume.scan_complete = False
        warnings.append("未找到 lsof 命令，无法扫描占用")
        return [], warnings
    except OSError as exc:
        volume.scan_complete = False
        warnings.append(f"lsof 调用失败：{exc}")
        return [], warnings

    if timed_out:
        volume.scan_complete = False
        warnings.append("lsof 扫描超时（30 秒），结果可能不完整")
        return [], warnings

    if truncated:
        # 截断后拿到的只是输出前缀，真正持有写入句柄的进程可能还在后面。
        # 必须降级：此时的「未发现占用」不等于「可以拔」。
        volume.scan_complete = False
        warnings.append(
            f"lsof 输出超过 {LSOF_OUTPUT_LIMIT // (1024 * 1024)}MB 上限，已截断；"
            "本次扫描不完整，不能据此判定可以拔出"
        )

    # lsof 无匹配时退出码为 1，属正常
    if code not in (0, 1):
        volume.scan_complete = False
        warnings.append(f"lsof 执行异常（退出码 {code}）：{err.strip()[:200]}")

    blockers = parse_lsof_field_output(raw)

    # 记录扫描时的卷身份，供发信号前复核。
    # 扫描到处置之间，进程可能已释放本卷句柄、PID 又被复用；
    # 只校验 pid + 启动时间 + exe 挡不住「同一个进程换了个卷」。
    for b in blockers:
        b.device = volume.device
        b.mountpoint = volume.mountpoint

    # stat 校验：识别「文件已删除但句柄未释放」
    for b in blockers:
        for h in b.handles:
            if h.is_pseudo:
                h.exists = None
                continue
            h.exists = path_exists(h.name)

    # 补探映射句柄的可写性。
    # lsof 的 txt 条目 access 恒为空，若按「不含 w/u 就是只读」处理，
    # 正在用 mmap 写盘的进程会被判成只读（实测 mmap+close 会被判为可安全推出）。
    # 探测失败时保持 None，由 Handle.write_state 归为「未知」并按可写保守处理。
    for b in blockers:
        mappings = [h for h in b.handles if h.is_mapping and not h.is_pseudo]
        if not mappings:
            continue
        table = scan_writable_mappings(b.pid)
        if not table:
            continue
        for h in mappings:
            for key in (h.name, os.path.realpath(h.name)):
                if key in table:
                    h.mapped_writable = table[key]
                    break

    # 探测该卷是否可靠上报 link count。
    # 判据：路径已经解析不到（exists=False），内核却仍报 link count >= 1。
    # 这是 exFAT(fskit) 的实测行为；出现即说明 +L1 在本卷不可用。
    # 扫描不完整时不做这项推断：截断处的半个路径必然 stat 不到，
    # 会伪造出「本卷 link count 不可靠」的假告警。
    if not volume.scan_complete:
        return blockers, warnings

    unreliable = [
        h for b in blockers for h in b.handles
        if h.exists is False and h.link_count is not None and h.link_count >= 1
    ]
    if unreliable:
        volume.link_count_reliable = False
        warnings.append(
            f"本卷不上报可靠的 link count（已删除文件的 link count 仍为 "
            f"{unreliable[0].link_count}），因此 lsof 的 +L1 无法识别已删除句柄。"
            "本工具改用路径校验作为判据，结论仍有效。"
        )
    elif any(h.link_count == 0 for b in blockers for h in b.handles):
        volume.link_count_reliable = True

    return blockers, warnings


def path_exists(path: str) -> Optional[bool]:
    """校验路径是否存在。True 存在，False 已删除，None 无法判定。

    这是本工具的核心判据：macOS 的 lsof 不标注 (deleted)，
    只能靠事后 stat 发现句柄指向的文件已经消失。
    """
    try:
        os.stat(path)
        return True
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None


# ────────────────────────────── 运行环境 ──────────────────────────────
# 探测「完全磁盘访问」所需的受保护路径：任一可读即视为已授权。
FDA_PROBE_FILES = (
    "~/Library/Safari/History.db",
    "~/Library/Messages/chat.db",
    "~/Library/Application Support/com.apple.TCC/TCC.db",
    "/Library/Application Support/com.apple.TCC/TCC.db",
)


def detect_access_level() -> tuple[str, str]:
    """判定当前进程能看到多大范围的进程。

    返回 (level, 说明)，level ∈ root / full / limited / unknown。

    为什么必须单独探测：未授权完全磁盘访问时，lsof 依然正常退出，只是
    看不到其他用户与 root 进程——此时空输出与「确实无占用」无法区分。
    把它当成安全结论会误导用户硬拔，所以宁可显式降级。
    """
    if os.geteuid() == 0:
        return "root", "以 root 运行，可见全部进程的句柄"

    denied = False
    for rel in FDA_PROBE_FILES:
        path = os.path.expanduser(rel)
        try:
            with open(path, "rb") as fh:
                fh.read(1)
            return "full", "已获完全磁盘访问，可见全部进程的句柄"
        except FileNotFoundError:
            continue
        except PermissionError:
            denied = True
            continue
        except OSError:
            continue

    if denied:
        return "limited", "未获完全磁盘访问，仅能看到本用户进程"
    return "unknown", "无法判定权限范围"


@dataclass
class EnvClue:
    """lsof 之外的旁证。用于弥补普通用户看不到 root 进程的盲区。"""

    label: str
    detail: str
    warn: bool = False


def collect_clues(vol: Volume) -> list[EnvClue]:
    """采集环境线索。

    Spotlight 的 mds 与 Time Machine 的 backupd 以 root 运行，普通用户的
    lsof 看不到它们，但它们的副作用（索引状态、备份目标）是可查的。
    """
    clues: list[EnvClue] = []

    code, out, err = run(["mdutil", "-s", vol.mountpoint])
    text = (out + "\n" + err).lower()
    # mdutil 的输出有 "Indexing disabled." 与 "Indexing and searching disabled." 两种措辞
    if "indexing enabled" in text or "索引已启用" in text:
        clues.append(EnvClue("Spotlight", "该卷索引已开启，mds 可能在后台索引并持有句柄", True))
    elif "disabled" in text or "已停用" in text:
        clues.append(EnvClue("Spotlight", "该卷索引已关闭", False))

    code, out, _ = run(["tmutil", "destinationinfo"])
    if code == 0:
        for line in out.splitlines():
            m = re.match(r"\s*Name\s*:\s*(.+?)\s*$", line)
            if m and m.group(1) == vol.display:
                clues.append(EnvClue("Time Machine", "该卷被配置为备份目标，backupd 可能正在写入", True))
                break

    for entry, label, detail in (
        (".Spotlight-V100", "索引数据", "卷上存在 .Spotlight-V100，曾被 Spotlight 索引"),
        (".fseventsd", "事件日志", "卷上存在 .fseventsd，文件系统事件仍在记录"),
    ):
        if os.path.isdir(os.path.join(vol.mountpoint, entry)):
            clues.append(EnvClue(label, detail, False))

    if not os.access(vol.mountpoint, os.W_OK):
        clues.append(EnvClue("挂载属性", "该卷以只读方式挂载", False))

    return clues
