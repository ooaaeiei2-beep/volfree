#!/usr/bin/env python3
"""volfree 实验装置 · 观测侧：直接调用 volfree 自己的 scan_blockers + assess。

对每种写法、每种文件系统，起一个子进程持续写同一个文件，在子进程存活期间：
  1. 连读两次文件头，证明文件确实在被改
  2. 调用 volfree.scan_blockers() 看它扫到什么
  3. 调用 volfree.assess() 看它把这个进程定成什么级
  4. 用 vmmap 交叉验证映射区确实存在

这样测的是产品真实判定，而不是另写一套探针语义。

用法：python3 probe.py
"""

import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
WRITER = HERE / "writer.py"
PY = sys.executable

sys.path.insert(0, str(PROJECT))
import volfree  # noqa: E402

VOLUMES = [
    ("apfs", "/Volumes/VfMmapApfs"),
    ("exfat", "/Volumes/VfMmapExfat"),
]
MODES = [
    ("write", "普通 write，fd 打开"),
    ("mmap_keep", "mmap，fd 保持打开"),
    ("mmap_close", "mmap 后 close(fd)"),
    ("pymmap_close", "Python mmap 模块后 close(fd)"),
    ("mmap_ro", "只读 mmap，fd 保持打开"),
    ("mmap_ro_close", "只读 mmap 后 close(fd)"),
    ("mmap_close_unlink", "mmap + close(fd) + unlink"),
]
HOLD = 7.0


def vmmap_hits(pid, needle):
    try:
        r = subprocess.run(["vmmap", str(pid)], capture_output=True,
                           text=True, timeout=40)
    except Exception as e:                                   # noqa: BLE001
        return ["<vmmap 失败: %s>" % e]
    return [l.strip() for l in r.stdout.splitlines() if needle in l]


def head(path):
    try:
        with open(path, "rb") as f:
            return f.read(16)
    except OSError:
        return b"<unreadable>"


def run_case(fs, mount, mode):
    name = "probe_%s.bin" % mode
    path = os.path.join(mount, name)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    with open(path, "wb") as f:
        f.write(b"." * (1 << 20))

    p = subprocess.Popen([PY, str(WRITER), path, mode, str(HOLD)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.stdout.readline().strip() != "READY":
        err = p.stderr.read()
        p.wait()
        return {"ok": False, "why": "子进程未就绪: %s" % err[:200]}

    h1 = head(path)
    time.sleep(0.45)
    h2 = head(path)

    vol = volfree.Volume(device="", mountpoint=mount, fstype=fs, external=True)
    blockers, warnings = volfree.scan_blockers(vol)
    mine = [b for b in blockers if b.pid == p.pid]
    for b in mine:
        volfree.assess(b)
    vm = vmmap_hits(p.pid, name)

    p.wait(timeout=HOLD + 10)
    rest = p.stdout.read().strip()
    final = head(path)

    return {
        "ok": True,
        "changing": h1 != h2, "h1": h1, "h2": h2, "final": final,
        "scanned": len(blockers),
        "mine": mine,
        "warnings": warnings,
        "vmmap": vm,
        "pid": p.pid,
        "rest": rest,
    }


def describe_handle(h):
    return "fd=%-4s access=%-3r type=%-4s exists=%-5s k=%-4s %s" % (
        h.fd, h.access, h.ftype, h.exists, h.link_count, h.name)


def main():
    rows = []
    for fs, mount in VOLUMES:
        if not os.path.isdir(mount):
            print("跳过未挂载的卷: %s" % mount)
            continue
        for mode, label in MODES:
            print(">>> %s / %s …" % (fs, mode), flush=True)
            r = run_case(fs, mount, mode)
            r.update({"fs": fs, "mode": mode, "label": label})
            rows.append(r)

    print("\n" + "=" * 100)
    print("逐例详情")
    print("=" * 100)
    for r in rows:
        print("\n[%s] %s —— %s" % (r["fs"], r["mode"], r["label"]))
        if not r["ok"]:
            print("  ⚠️  %s" % r["why"])
            continue
        print("  文件内容在变: %-3s  %r → %r   结束后 %r" % (
            "是" if r["changing"] else "否", r["h1"][:12], r["h2"][:12], r["final"][:12]))
        print("  volfree 共扫到 %d 个占用进程；vmmap 命中 %d 条映射" % (
            r["scanned"], len(r["vmmap"])))
        if not r["mine"]:
            print("  ❗ 没有扫到这个 pid %d" % r["pid"])
        for b in r["mine"]:
            print("  pid=%d cmd=%r has_write=%s level=%s" % (
                b.pid, b.command, b.has_write, b.level))
            print("     理由: %s" % b.reason)
            for h in b.handles:
                print("     · " + describe_handle(h))
        if r["warnings"]:
            for w in r["warnings"]:
                print("  ⚠️  %s" % w)
        if r.get("rest"):
            print("  子进程退出前输出: %s" % r["rest"])
        for l in r["vmmap"][:1]:
            print("  vmmap: %s" % l)

    print("\n" + "=" * 100)
    print("汇总：volfree 的判定 vs 事实")
    print("=" * 100)
    print("%-7s %-19s %-8s %-10s %-10s %s" % (
        "文件系统", "写法", "文件在变", "扫到进程", "判为写入", "给出的级别"))
    for r in rows:
        if not r["ok"]:
            print("%-7s %-19s 失败" % (r["fs"], r["mode"]))
            continue
        b = r["mine"][0] if r["mine"] else None
        ro = r["mode"].startswith("mmap_ro")
        if b is None:
            write_cell = "-"
        elif b.has_write:
            write_cell = "是" + ("（误报）" if ro else "")
        else:
            write_cell = "否" + ("（应然）" if ro else "❌漏判")
        print("%-7s %-19s %-8s %-10s %-10s %s" % (
            r["fs"], r["mode"],
            "是" if r["changing"] else "否",
            "是" if r["mine"] else "❌否",
            write_cell,
            b.level if b else "-"))

    print("\n判定语义：RISKY=有名字且可写  CAUTION=有名字只读 或 无名字可写  SAFE=无名字只读")


if __name__ == "__main__":
    main()
