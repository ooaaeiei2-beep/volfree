"""libproc 的 ctypes 封装。偏移与结构体大小都是实测值，改动前先读 docs/文件系统语义核查.md。"""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import Optional


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
