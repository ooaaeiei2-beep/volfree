#!/usr/bin/env python3
"""volfree 实验装置 · 子进程侧：用不同方式持续写一个文件，用于验证 lsof 的可见性。

模式：
  write               普通 write，fd 保持打开
  bulk                普通 write 但每轮写 256 KB，用于观察界面上的写入速率
  hold_ro             只读打开并一直持有，不写。用于造「有人占着但没在写」的黄态
  mmap_keep           mmap(MAP_SHARED)，fd 保持打开
  mmap_close          mmap(MAP_SHARED) 后立刻 close(fd)，只留映射
  mmap_close_unlink   同上，并 unlink 文件（无名文件）
  pymmap_close        用 Python 的 mmap 模块，然后 os.close(fd)

用法：writer.py <path> <mode> <hold_seconds>
"""

import ctypes
import os
import sys
import time

MAP_SHARED = 0x0001
PROT_READ, PROT_WRITE = 0x1, 0x2
MS_SYNC = 0x0010

libc = ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                      ctypes.c_int, ctypes.c_int, ctypes.c_long]
libc.munmap.restype = ctypes.c_int
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
libc.msync.restype = ctypes.c_int
libc.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
libc.memmove.restype = ctypes.c_void_p
libc.memmove.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]

SIZE = 1 << 20
STEP = 0.3


def stamp(i, prefix=b"M"):
    return (prefix + ("%06d" % i).encode()).ljust(16, b".")


def main():
    path, mode = sys.argv[1], sys.argv[2]
    hold = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
    rounds = max(1, int(hold / STEP))

    if mode == "write":
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        print("READY", flush=True)
        for i in range(rounds):
            os.pwrite(fd, stamp(i, b"W"), 0)
            os.fsync(fd)
            time.sleep(STEP)
        os.close(fd)
        return

    if mode == "hold_ro":
        # 只读持有：判定的「黄」格（有句柄、只读、有名字）要靠它造。
        # 之前没有这个模式，只能临时写一行 python -c，截图脚本里不好复用。
        fd = os.open(path, os.O_RDONLY)
        print("READY", flush=True)
        time.sleep(hold)
        os.close(fd)
        return

    if mode == "bulk":
        # 每轮 256 KB，约 850 KB/s。
        # 只写 16 字节那种节奏（write 模式）在 diskio 上只有 1.5 KB/s 上下，
        # 低于界面 2 KB/s 的噪声门限，看不到速率——那是数据太小，不是采样坏了。
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        chunk = b"B" * (256 << 10)
        span = SIZE * 4          # 4 MB 循环覆写，不会把测试卷写满
        off = 0
        print("READY", flush=True)
        for _ in range(rounds):
            os.pwrite(fd, chunk, off)
            off = (off + len(chunk)) % span
            os.fsync(fd)
            time.sleep(STEP)
        os.close(fd)
        return

    if mode == "pymmap_close":
        import mmap
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        m = mmap.mmap(fd, SIZE, flags=mmap.MAP_SHARED,
                      prot=mmap.PROT_READ | mmap.PROT_WRITE)
        os.close(fd)          # 若模块内部 dup 了 fd，关的只是我们自己那份
        print("READY", flush=True)
        for i in range(rounds):
            m[0:16] = stamp(i, b"P")
            m.flush()
            time.sleep(STEP)
        return

    # 只读映射：用来验证「txt 句柄能否区分读写」
    if mode in ("mmap_ro", "mmap_ro_close"):
        fd = os.open(path, os.O_RDONLY)
        addr = libc.mmap(None, SIZE, PROT_READ, MAP_SHARED, fd, 0)
        if not addr or addr == 0xFFFFFFFFFFFFFFFF:
            raise OSError(ctypes.get_errno(), "mmap 失败")
        if mode == "mmap_ro_close":
            os.close(fd)
        print("READY", flush=True)
        time.sleep(hold)
        libc.munmap(addr, SIZE)
        if mode == "mmap_ro":
            os.close(fd)
        return

    # 其余三种都走裸 mmap(2)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    addr = libc.mmap(None, SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0)
    if not addr or addr == 0xFFFFFFFFFFFFFFFF:
        raise OSError(ctypes.get_errno(), "mmap 失败")
    if mode in ("mmap_close", "mmap_close_unlink"):
        os.close(fd)          # 关键：fd 关闭后映射依然有效，且内核仍能写回
    if mode == "mmap_close_unlink":
        # 不能用 os.unlink：本环境的删除钩子会把它挪进 .Trashes 并保留名字，
        # 那样 lsof 看到的仍是有名文件，判 RISKY 而不是 CAUTION。
        # 要造「真·无名」，只能绕过 Python 直接调 libc。
        libc.unlink.argtypes = [ctypes.c_char_p]
        libc.unlink.restype = ctypes.c_int
        if libc.unlink(path.encode()) != 0:
            raise OSError(ctypes.get_errno(), "unlink 失败")

    print("READY", flush=True)
    for i in range(rounds):
        libc.memmove(addr, stamp(i, b"M"), 16)
        libc.msync(addr, SIZE, MS_SYNC)
        time.sleep(STEP)

    if mode == "mmap_close_unlink":
        # 文件已无名，外部读不到；从映射读回以证明内存写入确实发生
        print("READBACK %r" % ctypes.string_at(addr, 16), flush=True)
    libc.munmap(addr, SIZE)
    if mode == "mmap_keep":
        os.close(fd)


if __name__ == "__main__":
    main()
