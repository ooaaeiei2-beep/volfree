#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""volfree — macOS 卷占用诊断与安全释放工具

定位「移动硬盘推不出」的真实原因，并按风险等级给出处置建议。

与 lsof 类工具的核心差异：
  macOS 的 lsof 不像 Linux 那样在已删除文件后标注 (deleted)，路径照常显示。
  本工具对每个句柄路径做 stat 校验，把「文件已删除但句柄未释放」单独识别出来
  ——这类占用终止进程零风险，是最常见的隐形阻塞源。

用法：
  volfree                        交互选择卷并诊断（只读）
  volfree list                   列出可诊断的卷
  volfree analyze <卷名|设备>    诊断指定卷（只读）
  volfree release <卷名|设备>    处置（默认 dry-run，加 --apply 才执行）
  volfree analyze --json         机器可读输出

  以下三条是给界面用的原子动作，不做交互式提问：
  volfree status --json          一次报告所有卷
  volfree end <pid> --json       结束单个进程（身份复核 + 等级拦截）
  volfree eject <卷名> --json    只推出/卸载，不动进程

安全边界：
  - 默认只读；任何结束进程、卸载动作都要求 --apply 且逐项确认
  - 永不自动 SIGKILL，需显式 --force
  - 永不自动 force unmount，需显式 --force-unmount，且存在写入句柄时一律拒绝
  - 终止前校验 PID + 启动时间 + 可执行路径，并复核该进程仍持有本卷，防止 PID 复用误杀

本文件由 tools/bundle.py 从 volfree/ 包生成，不要直接编辑——
改动会在下次重新生成时被覆盖。要改就改包里的模块。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


# ────────────────────────── 数据结构 ──────────────────────────


# 源文件：volfree/models.py


VERSION = "0.3.0"

# ────────────────────────────── 风险等级 ──────────────────────────────
# SAFE    零风险，终止不会丢失任何数据
# CAUTION 可能丢失未保存状态，需确认
# RISKY   正在写入，强杀可能损坏文件
# NEVER   禁止操作（PID 1、窗口服务、自身等）
LEVEL_ORDER = {"NEVER": 0, "RISKY": 1, "CAUTION": 2, "SAFE": 3}

LEVEL_LABEL = {"SAFE": "安全", "CAUTION": "谨慎", "RISKY": "危险", "NEVER": "禁止"}

# ────────────────────────────── 数据结构 ──────────────────────────────
@dataclass
class Volume:
    """一个已挂载的卷。"""

    device: str          # /dev/disk6s1
    mountpoint: str      # /Volumes/My Passport
    fstype: str          # exfat
    external: bool = False
    ejectable: bool = False
    removable: bool = False
    virtual: bool = False
    volume_name: str = ""
    total_bytes: int = 0
    free_bytes: int = 0
    # 该卷是否可靠上报 link count。None = 尚未探测。
    # exFAT(fskit) 实测对已删除文件仍报 1，此时 lsof 的 +L1 完全失效，
    # 只能退化为路径校验。见 docs/文件系统语义核查.md
    link_count_reliable: Optional[bool] = None
    # 本次扫描是否完整。lsof 超时、输出被截断、退出码异常都会置 False，
    # 此时「未发现占用」不能当作结论——空列表也可能只是没扫完。
    scan_complete: bool = True

    @property
    def display(self) -> str:
        return self.volume_name or os.path.basename(self.mountpoint) or self.device

@dataclass
class Handle:
    """进程持有的一个文件句柄。"""

    fd: str                  # 3 / cwd / txt
    access: str = ""         # r / w / u / ""(未知)
    ftype: str = ""          # REG / DIR / CHR ...
    name: str = ""           # 路径，或 cwd/txt 等伪路径
    exists: Optional[bool] = None   # None = 无法判定
    # 内核给出的 link count（lsof 字段 k）。0 = 已 unlink。
    # 注意：exFAT(fskit) 对已删除文件仍报 1，不能单独依赖。
    link_count: Optional[int] = None
    # 映射句柄的可写性。None = 未探测或探测失败（未知），绝不等于「只读」。
    # lsof 的 txt 条目 access 恒为空，不补这一项就只能猜。
    mapped_writable: Optional[bool] = None

    @property
    def is_deleted(self) -> bool:
        """句柄指向的文件是否已无名字。

        两个独立信号取并集：
          - stat 失败（ENOENT）：路径确实解析不到
          - link count 为 0：内核确认已 unlink
        任一成立即视为已删除。在 APFS 上两者一致；在 exFAT 上只有前者可用。
        """
        if self.link_count == 0:
            return True
        return self.exists is False

    @property
    def is_mapping(self) -> bool:
        """是否是「把文件映射进内存」的句柄。

        lsof 用 fd=txt（程序文本/映射）与 fd=mem（共享映射）表示这类句柄，
        它们的 access 字段都没有意义，必须另外探保护位。
        """
        return self.fd in ("txt", "mem") and self.ftype == "REG"

    @property
    def write_state(self) -> str:
        """可写性三态：'yes' / 'no' / 'unknown'。

        关键约定：**unknown 不等于 no**。取不到就说取不到，
        由调用方按「可能可写」保守处理，不得据此断言只读。
        """
        if "w" in self.access or "u" in self.access:
            return "yes"
        if self.is_mapping:
            if self.mapped_writable is True:
                return "yes"
            if self.mapped_writable is False:
                return "no"
            return "unknown"
        if "r" in self.access:
            return "no"
        # cwd / ltx 这类句柄 lsof 本就不给 access，它们是只读性质，维持原判
        return "no"

    @property
    def is_writing(self) -> bool:
        """保守判定：未知按可写处理，宁可误报也不漏报。"""
        return self.write_state != "no"

    @property
    def is_certainly_read_only(self) -> bool:
        """只有明确读到「不可写」才算只读。"""
        return self.write_state == "no"

    @property
    def is_pseudo(self) -> bool:
        """name 不是绝对路径的句柄（socket、管道、空名），不参与文件级判定。

        注意 cwd 与 txt 的 name 是真实绝对路径（工作目录 / 卷上的可执行文件），
        它们同样是有效占用，不能排除。
        """
        return not self.name.startswith("/")

@dataclass
class Blocker:
    """一个占用卷的进程。"""

    pid: int
    command: str
    uid: int = -1
    exe: str = ""
    start_time: int = 0
    handles: list[Handle] = field(default_factory=list)
    level: str = "CAUTION"
    reason: str = ""
    # 扫描时所在的卷身份。发信号前用它复核「此刻是否仍持有这个卷」——
    # 扫描与处置之间进程可能已经关掉本卷句柄、PID 又被复用，
    # 只校验 pid + 启动时间 + exe 挡不住这种情况。
    device: str = ""
    mountpoint: str = ""
    # 属于其他用户（本工具无权终止）。由 assess 统一标注，处置环节据此跳过。
    other_user: bool = False

    @property
    def real_handles(self) -> list[Handle]:
        """参与文件级判定的句柄（排除 socket、管道这类伪句柄）。"""
        return [h for h in self.handles if not h.is_pseudo]

    @property
    def writing_handles(self) -> list[Handle]:
        return [h for h in self.real_handles if h.is_writing]

    @property
    def has_write(self) -> bool:
        return bool(self.writing_handles)

    @property
    def all_writes_unlinked(self) -> bool:
        """所有可写句柄都指向已删除的文件（mkstemp+unlink 模式）。

        降为 CAUTION 的前提是「没有任何有名字的可写文件」。
        只要还在写一个有名字的文件，终止就可能留下截断的现存文件，
        哪怕同时还有别的无名临时文件——所以判据是「全部」，不是「存在」。
        """
        w = self.writing_handles
        return bool(w) and all(h.is_deleted for h in w)

    @property
    def has_unlinked_write(self) -> bool:
        """存在无名可写句柄（不要求全部无名）。"""
        return any(h.is_deleted for h in self.writing_handles)

    @property
    def has_uncertain_write(self) -> bool:
        """存在可写性未知的句柄，用于把「按最坏情况处理」的理由说清楚。"""
        return any(h.write_state == "unknown" for h in self.real_handles)

    @property
    def all_deleted(self) -> bool:
        real = self.real_handles
        return bool(real) and all(h.is_deleted for h in real)

    @property
    def only_read(self) -> bool:
        """所有真实句柄都**明确**只读。未知不算只读。"""
        real = self.real_handles
        return bool(real) and all(h.is_certainly_read_only for h in real)


# ────────────────────────── libproc 封装 ──────────────────────────


# 源文件：volfree/proc.py


# ────────────────────────────── 进程信息（libproc） ──────────────────────────────
class ProcBsdInfo(ctypes.Structure):
    """libproc.h 的 struct proc_bsdinfo 子集，仅取需要的字段。"""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]

PROC_PIDTBSDINFO = 3

PROC_PIDPATHINFO_MAXSIZE = 4096

# 文件映射区的保护位。lsof 对映射文件报的 fd=txt 条目 access 恒为空，
# 拿不到「能不能写」；proc_pidinfo 能拿到，实测与 vmmap 的 rw-/rwx、r--/r-x 一致。
PROC_PIDREGIONPATHINFO = 8

VM_PROT_WRITE = 0x2

MAXPATHLEN = 1024

class VinfoStat(ctypes.Structure):
    """struct vinfo_stat，只为对齐 struct vnode_info 的布局，字段不参与判定。"""

    _fields_ = [
        ("vst_dev", ctypes.c_uint32),
        ("vst_mode", ctypes.c_uint16),
        ("vst_nlink", ctypes.c_uint16),
        ("vst_ino", ctypes.c_uint64),
        ("vst_uid", ctypes.c_uint32),
        ("vst_gid", ctypes.c_uint32),
        ("vst_atime", ctypes.c_int64),
        ("vst_atimensec", ctypes.c_int64),
        ("vst_mtime", ctypes.c_int64),
        ("vst_mtimensec", ctypes.c_int64),
        ("vst_ctime", ctypes.c_int64),
        ("vst_ctimensec", ctypes.c_int64),
        ("vst_birthtime", ctypes.c_int64),
        ("vst_birthtimensec", ctypes.c_int64),
        ("vst_size", ctypes.c_int64),
        ("vst_blocks", ctypes.c_int64),
        ("vst_blksize", ctypes.c_int32),
        ("vst_flags", ctypes.c_uint32),
        ("vst_gen", ctypes.c_uint32),
        ("vst_rdev", ctypes.c_uint32),
        ("vst_qspare", ctypes.c_int64 * 2),
    ]

class VnodeInfoPath(ctypes.Structure):
    """struct vnode_info + vip_path。"""

    _fields_ = [
        ("vip_stat", VinfoStat),
        ("vip_type", ctypes.c_int),
        ("vip_pad", ctypes.c_int),
        ("vip_fsid", ctypes.c_int32 * 2),
        ("vip_path", ctypes.c_char * MAXPATHLEN),
    ]

class ProcRegionInfo(ctypes.Structure):
    """struct proc_regioninfo。"""

    _fields_ = [
        ("pri_protection", ctypes.c_uint32),
        ("pri_max_protection", ctypes.c_uint32),
        ("pri_inheritance", ctypes.c_uint32),
        ("pri_flags", ctypes.c_uint32),
        ("pri_offset", ctypes.c_uint64),
        ("pri_behavior", ctypes.c_uint32),
        ("pri_user_wired_count", ctypes.c_uint32),
        ("pri_user_tag", ctypes.c_uint32),
        ("pri_pages_resident", ctypes.c_uint32),
        ("pri_pages_shared_now_private", ctypes.c_uint32),
        ("pri_pages_swapped_out", ctypes.c_uint32),
        ("pri_pages_dirtied", ctypes.c_uint32),
        ("pri_ref_count", ctypes.c_uint32),
        ("pri_shadow_depth", ctypes.c_uint32),
        ("pri_share_mode", ctypes.c_uint32),
        ("pri_private_pages_resident", ctypes.c_uint32),
        ("pri_shared_pages_resident", ctypes.c_uint32),
        ("pri_obj_id", ctypes.c_uint32),
        ("pri_depth", ctypes.c_uint32),
        ("pri_address", ctypes.c_uint64),
        ("pri_size", ctypes.c_uint64),
    ]

class ProcRegionWithPathInfo(ctypes.Structure):
    """struct proc_regionwithpathinfo，实测 1272 字节。"""

    _fields_ = [
        ("prp_prinfo", ProcRegionInfo),
        ("prp_vip", VnodeInfoPath),
    ]

RUSAGE_INFO_V2 = 2

class RusageInfoV2(ctypes.Structure):
    """struct rusage_info_v2，实测 160 字节。

    只为了拿 `ri_diskio_byteswritten`——进程累计写盘字节数。
    界面用它做差商得到写入速率，比按文件大小猜可靠：
    mmap 写入不改变文件长度，看 size 永远是零增长。
    """

    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]

def proc_diskio_written(pid: int) -> Optional[int]:
    """进程累计写盘字节数。取不到返回 None（界面按「未知」处理，不显示速率）。"""
    lib = _get_libproc()
    if not lib:
        return None
    if not hasattr(lib, "proc_pid_rusage"):
        return None
    lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    lib.proc_pid_rusage.restype = ctypes.c_int
    info = RusageInfoV2()
    if lib.proc_pid_rusage(pid, RUSAGE_INFO_V2, ctypes.byref(info)) != 0:
        return None
    return int(info.ri_diskio_byteswritten)

_libproc = None

def _get_libproc():
    global _libproc
    if _libproc is None:
        name = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        try:
            lib = ctypes.CDLL(name, use_errno=True)
            lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            lib.proc_pidpath.restype = ctypes.c_int
            lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                         ctypes.c_void_p, ctypes.c_int]
            lib.proc_pidinfo.restype = ctypes.c_int
            _libproc = lib
        except OSError:
            _libproc = False
    return _libproc or None

def proc_executable_path(pid: int) -> str:
    """取进程可执行文件绝对路径；失败返回空串。"""
    lib = _get_libproc()
    if not lib:
        return ""
    buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
    n = lib.proc_pidpath(pid, buf, PROC_PIDPATHINFO_MAXSIZE)
    return buf.value.decode("utf-8", "replace") if n > 0 else ""

def proc_start_time(pid: int) -> int:
    """取进程启动时间（epoch 秒）；失败返回 0。用于防止 PID 复用误杀。"""
    lib = _get_libproc()
    if not lib:
        return 0
    info = ProcBsdInfo()
    n = lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info),
                         ctypes.sizeof(info))
    return int(info.pbi_start_tvsec) if n > 0 else 0

def proc_uid(pid: int) -> int:
    lib = _get_libproc()
    if not lib:
        return -1
    info = ProcBsdInfo()
    n = lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info),
                         ctypes.sizeof(info))
    return int(info.pbi_uid) if n > 0 else -1

def scan_writable_mappings(pid: int) -> dict[str, bool]:
    """列出该进程所有文件映射区，返回 {路径: 是否可写}。

    lsof 对映射文件报的 fd=txt 条目，access 字段恒为空，读不出「能不能写」。
    这里直接问内核：遍历该进程的映射区，取 pri_protection 的写位。
    实测与 vmmap 的 rw-/rwx、r--/r-x 逐字一致。

    取不到（进程已退出、权限不足）时返回空字典，调用方必须把它当成
    「未知」，不得当成「只读」。
    """
    lib = _get_libproc()
    if not lib:
        return {}
    buf = ProcRegionWithPathInfo()
    size = ctypes.sizeof(buf)
    addr = 0
    out: dict[str, bool] = {}
    while True:
        n = lib.proc_pidinfo(pid, PROC_PIDREGIONPATHINFO, addr,
                             ctypes.byref(buf), size)
        if n <= 0:
            # 走到最后一个区域后必然返回一次 EINVAL，属正常终止
            break
        path = buf.prp_vip.vip_path.decode("utf-8", "replace")
        if path:
            writable = bool(buf.prp_prinfo.pri_protection & VM_PROT_WRITE)
            for key in (path, os.path.realpath(path)):
                out[key] = out.get(key, False) or writable
        nxt = buf.prp_prinfo.pri_address + buf.prp_prinfo.pri_size
        if nxt <= addr:
            break
        addr = nxt
    return out


# ────────────────────────── 扫描与解析 ──────────────────────────


# 源文件：volfree/scan.py


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


# ────────────────────────── 风险定级 ──────────────────────────


# 源文件：volfree/policy.py


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


# ────────────────────────── 诊断组装 ──────────────────────────


# 源文件：volfree/assess.py


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


# ────────────────────────── JSON 输出 ──────────────────────────


# 源文件：volfree/payload.py


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


# ────────────────────────── 终端输出 ──────────────────────────


# 源文件：volfree/render.py


# ────────────────────────────── 输出辅助 ──────────────────────────────
class Palette:
    """ANSI 配色；非 tty 或 --no-color 时全部降级为空串。"""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def cyan(self, t: str) -> str:
        return self._wrap("36", t)

    def level(self, lv: str, text: str) -> str:
        return {"SAFE": self.green, "CAUTION": self.yellow,
                "RISKY": self.red, "NEVER": self.red}[lv](text)

# ────────────────────────────── 报告渲染 ──────────────────────────────
def human_size(n: int) -> str:
    if not n:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(n)
    for u in units:
        if val < 1024 or u == units[-1]:
            return f"{val:.1f} {u}" if u != "B" else f"{int(val)} B"
        val /= 1024
    return f"{n} B"

def disp_width(text: str) -> int:
    """按终端显示宽度计算字符串宽度（CJK 全角字符占两列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)

def pad(text: str, width: int, right: bool = False) -> str:
    """按显示宽度补空格，保证中英文混排的表格列对齐。"""
    gap = max(0, width - disp_width(text))
    return (" " * gap + text) if right else (text + " " * gap)

def describe_handle(h: Handle) -> str:
    if h.is_deleted:
        state = "已删除"
    elif h.exists is None:
        state = "无法判定" if not h.is_pseudo else h.fd
    else:
        state = "存在"
    acc = {"r": "只读", "w": "写入", "u": "读写"}.get(h.access, "权限未知")
    kind = {"REG": "文件", "DIR": "目录"}.get(h.ftype, h.ftype or "未知")
    return f"fd {h.fd} · {kind} · {acc} · {state}"

def render_report(vol: Volume, blockers: list[Blocker], warnings: list[str],
                  pal: Palette, access: tuple[str, str] = ("unknown", ""),
                  clues: Optional[list[EnvClue]] = None) -> str:
    clues = clues or []
    access_level, access_desc = access
    lines: list[str] = []
    lines.append(pal.bold("volfree") + pal.dim(f" v{VERSION} — 卷占用诊断"))
    lines.append("")
    lines.append(f"  目标卷    {pal.cyan(vol.display)}")
    lines.append(f"  设备      {vol.device}")
    lines.append(f"  挂载点    {vol.mountpoint}")
    flags = []
    flags.append(vol.fstype or "未知格式")
    flags.append("磁盘映像" if vol.virtual else ("外接" if vol.external else "内置"))
    flags.append("可推出" if vol.ejectable else "不可推出")
    if vol.total_bytes:
        flags.append(f"{human_size(vol.total_bytes - vol.free_bytes)}/{human_size(vol.total_bytes)} 已用")
    lines.append(f"  属性      {' · '.join(flags)}")
    # 视野范围前置：读者应当先知道自己看到的是全部还是局部
    if access_level in ("root", "full"):
        lines.append(f"  可见范围  {access_desc}")
    else:
        lines.append(f"  可见范围  {pal.yellow(access_desc)}")
    lines.append("")

    handle_count = sum(len([h for h in b.handles if not h.is_pseudo]) for b in blockers)
    lines.append(f"占用进程 {pal.bold(str(len(blockers)))} 个 / 文件句柄 {pal.bold(str(handle_count))} 个")
    lines.append("")

    if not blockers:
        if not vol.scan_complete:
            # 空列表也可能只是没扫完，不能读成「没有占用」
            lines.append(pal.yellow("  扫描不完整，无法给出「没有占用」的结论。"))
            lines.append(pal.dim("  请先排除扫描告警（见下方），再判断能否拔出。"))
        elif access_level == "limited":
            lines.append(pal.yellow("  未发现本用户可访问的占用进程。"))
            lines.append(pal.dim("  但当前权限看不到系统进程，仍可能有 root 进程持有句柄。"))
        else:
            lines.append(pal.green("  未发现占用。"))
        lines.append("")
    else:
        header = ("  " + pad("风险", 8) + pad("进程", 24) + pad("PID", 8, right=True)
                  + "  " + pad("类型", 12) + pad("句柄", 6) + "状态")
        lines.append(pal.dim(header))
        lines.append(pal.dim("  " + "─" * 72))
        for b in blockers:
            real = [h for h in b.handles if not h.is_pseudo]
            lv = pal.level(b.level, pad(LEVEL_LABEL[b.level], 8))
            cmd = pad((b.command or "?")[:22], 24)
            kind = classify_kind(b)
            states = []
            for h in real[:3]:
                if h.is_deleted:
                    states.append("已删除")
                elif h.is_writing:
                    states.append("写入中")
                elif h.exists:
                    states.append("占用")
                else:
                    states.append("未知")
            state = "/".join(dict.fromkeys(states)) or "—"
            lines.append("  " + lv + cmd + pad(str(b.pid), 8, right=True)
                         + "  " + pad(kind, 12) + pad(str(len(real)), 6) + state)
        lines.append("")
        if not vol.scan_complete:
            # 有列表也不代表列全了：截断/超时后的前缀可能恰好漏掉写盘进程
            lines.append(pal.yellow(
                "  注意：本次扫描不完整，上面这份列表可能漏掉占用进程，不能据此判定可以拔出。"))
            lines.append("")

        for b in blockers:
            lines.append(f"  {pal.level(b.level, '· ' + LEVEL_LABEL[b.level])}  {pal.bold(b.command)} ({b.pid}) — {b.reason}")
            for h in [x for x in b.handles if not x.is_pseudo][:4]:
                mark = pal.dim("  └ ") + (pal.red(h.name) if h.is_deleted else h.name)
                lines.append(f"      {mark}")
                lines.append(f"        {pal.dim(describe_handle(h))}")
            if b.exe:
                lines.append(f"      {pal.dim(b.exe)}")
            lines.append("")

    if clues:
        lines.append(pal.bold("环境线索"))
        for c in clues:
            mark = pal.yellow("!") if c.warn else pal.dim("·")
            lines.append(f"  {mark} {pad(c.label, 20)}{c.detail}")
        lines.append("")

    if warnings:
        lines.append(pal.dim("提示"))
        for w in warnings:
            lines.append(pal.dim(f"  · {w}"))
        lines.append("")

    # 结论与下一步
    lines.append(pal.bold("建议"))
    warn_clues = [c for c in clues if c.warn]
    if not blockers:
        if not vol.scan_complete:
            # 扫描没跑完就给出「可以直接推出」是假绿，与产品最核心的诚实性冲突
            lines.append(pal.yellow("  扫描不完整，暂不给出能否推出的结论。"))
            lines.append(pal.dim("  请按上方提示排除扫描问题后重试。"))
        elif access_level == "limited":
            lines.append(pal.yellow("  先补齐权限再判断：sudo volfree analyze "
                                    + f'"{vol.display}"'))
            lines.append(pal.dim("  或在「系统设置 → 隐私与安全性 → 完全磁盘访问」中给终端授权。"))
        else:
            lines.append("  可直接在访达推出。")
        if warn_clues:
            lines.append(pal.dim("  若仍失败，注意上面环境线索中标 ! 的项。"))
    else:
        safe = [b for b in blockers if b.level == "SAFE"]
        risky = [b for b in blockers if b.level == "RISKY"]
        if safe:
            names = "、".join(f"{b.command}({b.pid})" for b in safe)
            lines.append(pal.green(f"  可安全结束：{names}"))
        if risky:
            names = "、".join(f"{b.command}({b.pid})" for b in risky)
            lines.append(pal.yellow(f"  存在写入，勿强杀：{names} —— 请先在对应程序中保存并退出"))
        if not risky:
            cmd_hint = f'volfree release "{vol.display}" --apply'
            lines.append("  执行：" + pal.cyan(cmd_hint))
        else:
            lines.append("  待写入结束后再执行 release。")
        if access_level == "limited":
            lines.append(pal.dim("  注意：以上仅为本用户可见的部分，系统进程可能另有占用。"))
    return "\n".join(lines)


# ────────────────────────── 处置与命令 ──────────────────────────


# 源文件：volfree/cli.py


# ────────────────────────────── 处置 ──────────────────────────────
def ask(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"  {prompt} [需要交互确认，当前非终端环境，已跳过]")
        return False
    try:
        return input(f"  {prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False

def pid_holds_volume(pid: int, mountpoint: str) -> Optional[bool]:
    """复核：该进程此刻是否仍持有指定挂载点上的句柄。

    True  仍持有
    False 已不再持有（扫描之后释放了，或根本没持有）
    None  无法复核（lsof 不可用、超时、异常退出），调用方须按不安全处理
    """
    if not mountpoint:
        return None
    root = mountpoint.rstrip("/") or "/"
    prefix = root if root.endswith("/") else root + "/"
    cmd = ["lsof", "-nP", "+c", "0", "-F", "pn", "-p", str(pid), root]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=10, errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    # 退出码 1 表示无匹配，即该进程已不持有本卷句柄
    if p.returncode not in (0, 1):
        return None
    for line in p.stdout.splitlines():
        if line.startswith("n"):
            name = line[1:]
            if name == root or name.startswith(prefix):
                return True
    return False

def verify_identity(b: Blocker) -> tuple[bool, str]:
    """终止前复核：PID 仍存活、启动时间与可执行路径均未变，且仍持有同一个卷。"""
    if b.start_time:
        now_start = proc_start_time(b.pid)
        if now_start != b.start_time:
            return False, "进程已退出或被替换（启动时间不符），跳过"
    now_exe = proc_executable_path(b.pid)
    if b.exe and now_exe and now_exe != b.exe:
        return False, "可执行路径已变化（疑似 PID 复用），跳过"
    if now_exe == "" and b.exe:
        return False, "进程已不存在，跳过"
    # 卷绑定复核：前三项都通过，也只说明「还是同一个进程」，
    # 不说明「它此刻还占着这个卷」。扫描之后它可能已关掉句柄，
    # 此时终止它既无必要，也放大了 PID 复用窗口里的误杀风险。
    if b.mountpoint:
        held = pid_holds_volume(b.pid, b.mountpoint)
        if held is False:
            return False, "该进程已不再持有本卷句柄（可能已切换到其他卷），跳过"
        if held is None:
            return False, "无法复核该进程与本卷的绑定关系，为安全起见跳过"
    return True, ""

def send_signal(b: Blocker, sig: int) -> tuple[bool, str]:
    try:
        os.kill(b.pid, sig)
        return True, ""
    except ProcessLookupError:
        return True, "进程已退出"
    except PermissionError:
        return False, "权限不足（该进程属于其他用户或受系统保护）"
    except OSError as exc:
        return False, str(exc)

def release_volume(vol: Volume, blockers: list[Blocker], apply: bool,
                   assume_yes: bool, force: bool, force_unmount: bool,
                   pal: Palette) -> int:
    """按风险从低到高处置，最后尝试卸载。返回退出码。"""
    print()
    print(pal.bold("处置计划"))
    actionable = [b for b in blockers if b.level in ("SAFE", "CAUTION")]
    forbidden = [b for b in blockers if b.level in ("RISKY", "NEVER")]

    if forbidden:
        for b in forbidden:
            print(f"  {pal.level(b.level, '跳过')} {b.command}({b.pid}) — {b.reason}")
        print()

    if not actionable:
        print("  没有可安全终止的占用进程。")
    else:
        for b in sorted(actionable, key=lambda x: -LEVEL_ORDER[x.level]):
            print(f"  → {LEVEL_LABEL[b.level]} {b.command}({b.pid})  {b.reason}")

    if not apply:
        print()
        print(pal.dim("  当前为演练模式（dry-run），未执行任何动作。"))
        print(pal.dim(f"  确认无误后加 --apply 执行：volfree release \"{vol.display}\" --apply"))
        return 1 if blockers else 0

    # 1) 结束可安全终止的进程
    for b in sorted(actionable, key=lambda x: -LEVEL_ORDER[x.level]):
        if not ask(f"终止 {b.command}({b.pid})？{b.reason}", assume_yes):
            continue

        ok, why = verify_identity(b)
        if not ok:
            print(f"    跳过：{why}")
            continue
        ok, why = send_signal(b, signal.SIGTERM)
        if not ok:
            print(f"    {pal.red('失败')}：{why}")
            continue
        print(f"    已发送 SIGTERM")
        time.sleep(0.6)

        # 复核：进程是否已退出
        if proc_executable_path(b.pid) and force:
            if ask(f"{b.command}({b.pid}) 未退出，强制结束（SIGKILL，可能丢失数据）？", assume_yes):
                ok, why = send_signal(b, signal.SIGKILL)
                print(f"    SIGKILL {'已发送' if ok else '失败：' + why}")
                time.sleep(0.4)
        elif proc_executable_path(b.pid):
            print(f"    {pal.yellow('仍未退出')}，如需强制结束请加 --force")

    # 2) 复核句柄
    remaining, _ = scan_blockers(vol)
    remaining = enrich_and_assess(remaining)
    if remaining:
        print()
        print(pal.yellow(f"  仍有 {len(remaining)} 个进程持有句柄："))
        for b in remaining:
            print(f"    {LEVEL_LABEL[b.level]} {b.command}({b.pid})")
        print(pal.dim("  卸载可能失败，建议先处理上述进程。"))

    # 3) 卸载 / 推出
    # diskutil(1)：可移动介质只有 eject 才进入「可安全手动取出」状态，
    # 且 unmount 最长可能需要 1 分钟用于刷盘，超时必须给足。
    removable_like = vol.removable or vol.ejectable or vol.external
    verb = "eject" if removable_like else "unmount"
    label = "推出" if verb == "eject" else "卸载"
    print()
    print(pal.bold(label))
    code, out, err = run(["diskutil", verb, vol.device], timeout=120)
    if code == 0:
        print(pal.green(f"  已{label} {vol.device}"))
        if verb == "eject":
            print(pal.dim("  设备已断电，现在可以安全拔线。"))
        return 0

    if code == 124:
        print(pal.red(f"  {label}超时。"))
        print(pal.dim("  手册说明卸载最长可能需要 1 分钟用于刷盘，此刻文件系统可能仍在收尾。"
                      "请稍后重试，不要强制。"))
        return 2

    print(pal.red(f"  {label}失败：{(err or out).strip()[:200]}"))

    if force_unmount:
        writable = [b for b in remaining if b.has_write]
        if writable:
            names = "、".join(f"{b.command}({b.pid})" for b in writable)
            print(pal.red(f"  拒绝强制{label}：仍有进程持有写入句柄（{names}）。"))
            print(pal.dim("  强制卸载会跳过刷盘，未落盘的数据直接丢失；exFAT 无事务保证，"
                          "元数据可能留下撕裂状态。请先结束这些进程或等其写完。"))
            return 2
        if ask(f"强制{label} {vol.device}？（会跳过刷盘，未落盘数据将丢失）", assume_yes):
            code2, out2, err2 = run(["diskutil", verb, "force", vol.device], timeout=120)
            if code2 == 0:
                print(pal.green(f"  已强制{label}"))
                return 0
            print(pal.red(f"  强制{label}仍失败：{(err2 or out2).strip()[:200]}"))
    else:
        print(pal.dim(f"  如确认无写入，可加 --force-unmount 强制{label}。"))
    return 2

# ────────────────────────────── CLI ──────────────────────────────
def pick_volume_interactive(pal: Palette) -> Optional[Volume]:
    volumes = list_volumes()
    if not volumes:
        print("未发现外接或可推出的卷。")
        return None
    if len(volumes) == 1:
        return volumes[0]
    print(pal.bold("发现多个卷："))
    for i, v in enumerate(volumes, 1):
        print(f"  {i}) {v.display}  {pal.dim(v.device + '  ' + v.fstype)}")
    if not sys.stdin.isatty():
        return volumes[0]
    try:
        raw = input("选择卷 [1]: ").strip() or "1"
        idx = int(raw) - 1
        return volumes[idx] if 0 <= idx < len(volumes) else None
    except (ValueError, EOFError, KeyboardInterrupt):
        return None

def cmd_list(args, pal: Palette) -> int:
    volumes = list_volumes(include_internal=args.all)
    if not volumes:
        print("未发现外接或可推出的卷。")
        return 0
    for v in volumes:
        flags = [v.fstype or "?", "磁盘映像" if v.virtual else ("外接" if v.external else "内置"),
                 "可推出" if v.ejectable else "不可推出"]
        print(f"{v.display}\t{v.device}\t{v.mountpoint}\t{' · '.join(flags)}")
    return 0

def cmd_analyze(args, pal: Palette) -> int:
    vol = resolve_volume(args.volume, include_internal=args.all) if args.volume else pick_volume_interactive(pal)
    if not vol:
        print(f"未找到卷：{args.volume}" if args.volume else "未选择卷。", file=sys.stderr)
        return 2
    blockers, warnings, access, clues = diagnose(vol)
    if args.json:
        print(blockers_to_json(vol, blockers, warnings, access, clues))
    else:
        print(render_report(vol, blockers, warnings, pal, access, clues))
    return 0 if not blockers else 1

def cmd_release(args, pal: Palette) -> int:
    vol = resolve_volume(args.volume, include_internal=args.all) if args.volume else pick_volume_interactive(pal)
    if not vol:
        print(f"未找到卷：{args.volume}" if args.volume else "未选择卷。", file=sys.stderr)
        return 2
    blockers, warnings, access, clues = diagnose(vol)
    print(render_report(vol, blockers, warnings, pal, access, clues))
    return release_volume(vol, blockers, apply=args.apply, assume_yes=args.yes,
                          force=args.force, force_unmount=args.force_unmount, pal=pal)

# ────────────────────────────── 面向界面的动作 ──────────────────────────────
def _action_result(args, pal: Palette, *, ok: bool, code: int, action: str,
                   message: str, pid: Optional[int] = None,
                   alive: Optional[bool] = None) -> int:
    """统一的动作回执。界面只认 JSON，不去解析人类文案。"""
    if getattr(args, "json", False):
        payload: dict = {"ok": ok, "action": action, "message": message}
        if pid is not None:
            payload["pid"] = pid
        if alive is not None:
            payload["alive"] = alive
        print(json.dumps(payload, ensure_ascii=False))
    else:
        mark = pal.green("✓") if ok else pal.yellow("!")
        print(f"  {mark} {message}")
    return code

def cmd_status(args, pal: Palette) -> int:
    """一次报告所有候选卷，供界面轮询。比逐卷 analyze 少一半进程启动开销。"""
    volumes = list_volumes(include_internal=args.all)
    access = detect_access_level()
    reports = []
    for vol in volumes:
        blockers, warnings, _, clues = diagnose(vol)
        reports.append(volume_payload(vol, blockers, warnings, access, clues))

    if args.json:
        print(json.dumps({
            "version": VERSION,
            "access": {"level": access[0], "description": access[1]},
            "volumes": reports,
        }, ensure_ascii=False, indent=2))
        return 0

    if not reports:
        print("未发现外接或可推出的卷。")
        return 0
    for r in reports:
        v, s = r["volume"], r["summary"]
        print(f"{v['name']}\t{v['device']}\t占用 {s['total']} · "
              f"危险 {s['risky']} · 谨慎 {s['caution']} · 安全 {s['safe']}")
    return 0

def cmd_watch(args, pal: Palette) -> int:
    """只回答「这些挂载点现在有没有人占着」，供菜单栏图标在面板关闭时更新。

    为什么单独开一条命令，而不是让界面一直调 `status`：

    - `status` 每次要跑两次 `diskutil`（约 270ms）去拿卷的身份（外接/可推出/文件系统）。
      但卷的身份在**插拔之间不会变**，而占用会随时变。空闲时反复查身份纯属浪费。
    - 这里只跑 `mount`（约 6ms）+ `lsof`（约 150ms），单次从约 590ms 降到约 230ms。

    刻意**不做**的事：不判外接、不判磁盘映像、不算容量。这些字段只用于显示和推出，
    不参与占用定级（`scan_blockers` 只用到 mountpoint），所以这里不需要它们。
    """
    wanted = [v for v in (args.volume or []) if v]
    if not wanted:
        print(json.dumps({"version": VERSION, "volumes": []}, ensure_ascii=False))
        return 0

    # 从 mount 表拿设备号；顺便能识别"已经不在挂载表里"（刚被拔掉）
    code, out, _ = run(["mount"])
    table: dict[str, str] = {}
    if code == 0:
        for device, mountpoint, _opts in parse_mount_output(out):
            table[mountpoint] = device

    reports = []
    for mountpoint in sorted(set(wanted)):
        device = table.get(mountpoint)
        if not device:
            # 已不在挂载表里，交给界面的插拔通知处理，这里不猜
            continue
        vol = Volume(device=device, mountpoint=mountpoint, fstype="",
                     external=True, ejectable=True, removable=False, virtual=False,
                     volume_name=os.path.basename(mountpoint),
                     total_bytes=0, free_bytes=0)
        blockers, _warnings = scan_blockers(vol)
        blockers = enrich_and_assess(blockers)
        risky = sum(1 for b in blockers if b.level == "RISKY")
        # 与界面 Models.swift 的 `tone` 保持同一条规则：
        # 有 RISKY 就是红，否则有人占着就是黄；扫描不完整一律黄，不给绿。
        if risky:
            tone = "danger"
        elif blockers or not vol.scan_complete:
            tone = "caution"
        else:
            tone = "safe"
        reports.append({
            "mountpoint": mountpoint,
            "tone": tone,
            "total": len(blockers),
            "risky": risky,
            "writing": sum(1 for b in blockers if b.has_write),
            "complete": vol.scan_complete,
        })

    print(json.dumps({"version": VERSION, "volumes": reports}, ensure_ascii=False))
    return 0

def cmd_end(args, pal: Palette) -> int:
    """结束单个进程，不碰挂载状态。

    与 release 的分工：release 是给人用的批量流程（含推出），end 只做一件事，
    供界面「逐项结束」调用。身份复核、受保护进程、RISKY 拦截一律复用 release 的规则，
    不另开一套判断。
    """
    pid = args.pid

    if args.volume:
        vol = resolve_volume(args.volume, include_internal=args.all)
        if not vol:
            return _action_result(args, pal, ok=False, code=2, action="end", pid=pid,
                                  message=f"未找到卷：{args.volume}")
        candidates = [vol]
    else:
        candidates = list_volumes(include_internal=args.all)

    target: Optional[Blocker] = None
    for v in candidates:
        blockers, _, _, _ = diagnose(v)
        target = next((b for b in blockers if b.pid == pid), None)
        if target:
            break

    if target is None:
        # 已经不占用了，对界面而言就是成功
        return _action_result(args, pal, ok=True, code=0, action="end", pid=pid,
                              alive=False, message="进程已不再持有该卷的句柄")

    if target.level == "NEVER":
        return _action_result(args, pal, ok=False, code=2, action="end", pid=pid,
                              message=f"{target.command} 是受保护进程，拒绝结束")
    if target.level == "RISKY" and not args.allow_risky:
        return _action_result(args, pal, ok=False, code=2, action="end", pid=pid,
                              message=f"{target.command} 正在写入，需显式允许才可结束")

    ok, why = verify_identity(target)
    if not ok:
        return _action_result(args, pal, ok=False, code=2, action="end", pid=pid, message=why)
    ok, why = send_signal(target, signal.SIGTERM)
    if not ok:
        return _action_result(args, pal, ok=False, code=2, action="end", pid=pid, message=why)

    time.sleep(0.6)
    if proc_executable_path(pid):
        if not args.force:
            return _action_result(args, pal, ok=False, code=1, action="end", pid=pid,
                                  alive=True, message="已发送 SIGTERM，进程仍未退出")
        ok, why = send_signal(target, signal.SIGKILL)
        if not ok:
            return _action_result(args, pal, ok=False, code=2, action="end", pid=pid, message=why)
        time.sleep(0.4)
        if proc_executable_path(pid):
            return _action_result(args, pal, ok=False, code=2, action="end", pid=pid,
                                  alive=True, message="SIGKILL 后进程仍在")

    return _action_result(args, pal, ok=True, code=0, action="end", pid=pid,
                          alive=False, message=f"已结束 {target.command}")

def cmd_eject(args, pal: Palette) -> int:
    """只做推出/卸载，不动任何进程。界面绿态主按钮用。"""
    vol = (resolve_volume(args.volume, include_internal=args.all) if args.volume
           else pick_volume_interactive(pal))
    if not vol:
        return _action_result(args, pal, ok=False, code=2, action="eject",
                              message=f"未找到卷：{args.volume}" if args.volume else "未选择卷")

    blockers, _, _, _ = diagnose(vol)
    writable = [b for b in blockers if b.has_write]
    if writable:
        names = "、".join(f"{b.command}({b.pid})" for b in writable)
        return _action_result(args, pal, ok=False, code=2, action="eject",
                              message=f"仍有进程持有写入句柄（{names}），拒绝推出")

    removable_like = vol.removable or vol.ejectable or vol.external
    verb = "eject" if removable_like else "unmount"
    label = "推出" if verb == "eject" else "卸载"

    code, out, err = run(["diskutil", verb, vol.device], timeout=120)
    if code == 0:
        return _action_result(args, pal, ok=True, code=0, action="eject",
                              message=f"已{label} {vol.display}")
    if code == 124:
        return _action_result(args, pal, ok=False, code=2, action="eject",
                              message=f"{label}超时（最长可能需要 1 分钟刷盘），请稍后重试，不要强制")

    detail = (err or out).strip()[:200]
    if args.force_unmount:
        code2, out2, err2 = run(["diskutil", verb, "force", vol.device], timeout=120)
        if code2 == 0:
            return _action_result(args, pal, ok=True, code=0, action="eject",
                                  message=f"已强制{label}（跳过刷盘）")
        detail = (err2 or out2).strip()[:200]

    return _action_result(args, pal, ok=False, code=2, action="eject",
                          message=f"{label}失败：{detail}")

def cmd_doctor(args, pal: Palette) -> int:
    """环境自检：把「结果是否可信」的前提条件摆出来。"""
    print(pal.bold("volfree 环境自检"))
    print()

    rows: list[tuple[str, str, str, bool]] = []
    py_ok = sys.version_info >= (3, 9)
    rows.append(("Python", platform.python_version(), "需要 3.9+", py_ok))

    for tool, required in (("lsof", True), ("diskutil", True),
                           ("mdutil", False), ("tmutil", False)):
        found = shutil.which(tool) or shutil.which(tool, path="/usr/sbin:/usr/bin:/sbin:/bin")
        if found:
            rows.append((tool, found, "", True))
        else:
            rows.append((tool, "未找到", "缺失", not required))

    level, desc = detect_access_level()
    rows.append(("进程可见范围", desc, "完全磁盘访问或 root", level in ("root", "full")))

    try:
        vols = list_volumes()
        rows.append(("卷枚举", f"{len(vols)} 个可诊断卷", "", True))
    except Exception as exc:  # noqa: BLE001 - 自检要捕获一切
        rows.append(("卷枚举", f"失败：{exc}"[:40], "", False))

    mac_ver = platform.mac_ver()[0] or "未知"
    try:
        major_ok = int(mac_ver.split(".")[0]) >= 13
    except ValueError:
        major_ok = True
    rows.append(("macOS", mac_ver, "13+", major_ok))

    for name, value, hint, ok in rows:
        mark = pal.green("✓") if ok else pal.yellow("!")
        print(f"  {mark} {pad(name, 14)}{pad(value, 40)}{pal.dim(hint)}")

    print()
    if level in ("root", "full"):
        print(pal.green("  权限充足，诊断结果可信。"))
    else:
        print(pal.yellow("  权限受限：lsof 看不到系统进程（mds 等）。"))
        print(pal.dim("  日常使用建议在「系统设置 → 隐私与安全性 → 完全磁盘访问」中给终端授权；"))
        print(pal.dim("  或临时用 sudo volfree analyze \"<卷名>\" 获取完整结果。"))
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="volfree",
        description="诊断 macOS 卷被谁占用，并按风险等级安全释放后卸载。默认只读。",
        epilog="示例：volfree analyze \"My Passport\"  |  volfree release \"My Passport\" --apply",
    )
    p.add_argument("--version", action="version", version=f"volfree {VERSION}")
    p.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    p.add_argument("--all", action="store_true", help="包含内置卷")

    # 让 --no-color / --all 在子命令之后也能使用；SUPPRESS 避免子命令默认值覆盖全局设置
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                        help="关闭彩色输出")
    common.add_argument("--all", action="store_true", default=argparse.SUPPRESS,
                        help="包含内置卷")

    sub = p.add_subparsers(dest="command")

    sub.add_parser("doctor", parents=[common], help="环境自检（权限、依赖、卷枚举）")

    sp = sub.add_parser("list", parents=[common], help="列出可诊断的卷")

    sa = sub.add_parser("analyze", parents=[common], help="诊断指定卷（只读）")
    sa.add_argument("volume", nargs="?", help="卷名、挂载点或设备号")
    sa.add_argument("--json", action="store_true", help="输出 JSON")

    ss = sub.add_parser("status", parents=[common], help="一次报告所有卷（界面轮询用）")
    ss.add_argument("--json", action="store_true", help="输出 JSON")

    sw = sub.add_parser("watch", parents=[common],
                        help="只查指定挂载点的占用（界面空闲时更新菜单栏图标用）")
    sw.add_argument("--volume", action="append", metavar="挂载点",
                    help="要检查的挂载点，可重复")
    sw.add_argument("--json", action="store_true", help="输出 JSON")

    se = sub.add_parser("end", parents=[common], help="结束单个进程（界面逐项操作用）")
    se.add_argument("pid", type=int, help="进程号")
    se.add_argument("--volume", help="限定在该卷上查找（省略则搜所有候选卷）")
    se.add_argument("--force", action="store_true", help="SIGTERM 无效时允许 SIGKILL")
    se.add_argument("--allow-risky", action="store_true",
                    help="允许结束正在写入的进程（界面逐项确认后使用）")
    se.add_argument("--json", action="store_true", help="输出 JSON")

    sj = sub.add_parser("eject", parents=[common], help="只推出/卸载，不动任何进程")
    sj.add_argument("volume", nargs="?", help="卷名、挂载点或设备号")
    sj.add_argument("--force-unmount", action="store_true",
                    help="推出失败时允许强制（会跳过刷盘，有写入句柄时拒绝执行）")
    sj.add_argument("--json", action="store_true", help="输出 JSON")

    sr = sub.add_parser("release", parents=[common], help="释放占用并卸载")
    sr.add_argument("volume", nargs="?", help="卷名、挂载点或设备号")
    sr.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    sr.add_argument("--yes", action="store_true", help="跳过逐项确认")
    sr.add_argument("--force", action="store_true", help="SIGTERM 无效时允许 SIGKILL")
    sr.add_argument("--force-unmount", action="store_true", help="推出/卸载失败时允许强制（会跳过刷盘，有写入句柄时拒绝执行）")
    return p

def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pal = Palette(enabled=not args.no_color and sys.stdout.isatty())

    if args.command is None:
        # 无子命令 = 交互诊断
        vol = pick_volume_interactive(pal)
        if not vol:
            return 2
        blockers, warnings, access, clues = diagnose(vol)
        print(render_report(vol, blockers, warnings, pal, access, clues))
        return 0 if not blockers else 1

    handlers = {"doctor": cmd_doctor, "list": cmd_list,
                "analyze": cmd_analyze, "release": cmd_release,
                "status": cmd_status, "watch": cmd_watch,
                "end": cmd_end, "eject": cmd_eject}
    return handlers[args.command](args, pal)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        sys.exit(130)
