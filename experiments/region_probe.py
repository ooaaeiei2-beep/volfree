#!/usr/bin/env python3
"""验证：能否用 proc_pidinfo(PROC_PIDREGIONPATHINFO) 精确拿到映射的保护位。

如果能，volfree 就可以在不解析 vmmap 文本、不依赖 lsof 空 access 的前提下，
判断某个进程对某个文件的映射是否可写。

用法：python3 region_probe.py <pid> [路径片段]
"""

import ctypes
import sys

PROC_PIDREGIONPATHINFO = 8
MAXPATHLEN = 1024


class VinfoStat(ctypes.Structure):
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


class Fsid(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]


class VnodeInfo(ctypes.Structure):
    _fields_ = [
        ("vi_stat", VinfoStat),
        ("vi_type", ctypes.c_int),
        ("vi_pad", ctypes.c_int),
        ("vi_fsid", Fsid),
    ]


class VnodeInfoPath(ctypes.Structure):
    _fields_ = [
        ("vip_vi", VnodeInfo),
        ("vip_path", ctypes.c_char * MAXPATHLEN),
    ]


class ProcRegionInfo(ctypes.Structure):
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
    _fields_ = [
        ("prp_prinfo", ProcRegionInfo),
        ("prp_vip", VnodeInfoPath),
    ]


libc = ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
libc.proc_pidinfo.restype = ctypes.c_int
libc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                              ctypes.c_void_p, ctypes.c_int]

VM_PROT_READ, VM_PROT_WRITE, VM_PROT_EXECUTE = 1, 2, 4


def prot_str(p):
    return ("r" if p & VM_PROT_READ else "-") + \
           ("w" if p & VM_PROT_WRITE else "-") + \
           ("x" if p & VM_PROT_EXECUTE else "-")


def scan(pid, needle=None):
    buf = ProcRegionWithPathInfo()
    size = ctypes.sizeof(buf)
    addr = 0
    rows = []
    while True:
        ctypes.set_errno(0)
        n = libc.proc_pidinfo(pid, PROC_PIDREGIONPATHINFO, addr,
                              ctypes.byref(buf), size)
        if n <= 0:
            err = ctypes.get_errno()
            if err not in (0, 3):        # 3 = ESRCH，进程已退出
                print("  proc_pidinfo 失败 errno=%d（结构体大小 %d）" % (err, size))
            break
        path = buf.prp_vip.vip_path.decode("utf-8", "replace")
        if path:
            rows.append((buf.prp_prinfo.pri_protection,
                         buf.prp_prinfo.pri_max_protection,
                         path))
        nxt = buf.prp_prinfo.pri_address + buf.prp_prinfo.pri_size
        if nxt <= addr:
            break
        addr = nxt
    if needle:
        rows = [r for r in rows if needle in r[2]]
    return rows, size


if __name__ == "__main__":
    pid = int(sys.argv[1])
    needle = sys.argv[2] if len(sys.argv) > 2 else None
    rows, size = scan(pid, needle)
    print("struct proc_regionwithpathinfo = %d 字节" % size)
    print("命中 %d 个映射区：" % len(rows))
    for cur, mx, path in rows:
        print("  prot=%s max=%s  %s" % (prot_str(cur), prot_str(mx), path))
