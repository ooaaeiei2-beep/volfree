#!/usr/bin/env python3
"""volfree 实验装置 · 强卸载/强断的损坏形态。

两种「强」：
  unmount-force   写入中途 diskutil unmount force（仍会让文件系统收尾刷盘）
  yank            写入中途直接 hdiutil detach -force，不给文件系统收尾机会
                  —— 这是最接近「拔线/断电」的可复现近似

流程：铺底 → 持续写入（不 fsync）→ 强断 → 以 -nomount 重新挂上拿设备节点
     → 对设备节点跑 fsck -n → 挂载 → 逐块校验 growing.bin

用法：python3 damage.py <apfs|exfat> <unmount-force|yank>
"""

import os
import struct
import subprocess
import sys
import threading
import time

VOLS = {
    "apfs": ("/Volumes/VfMmapApfs", "/tmp/vf_mmap_apfs.dmg", "fsck_apfs"),
    "exfat": ("/Volumes/VfMmapExfat", "/tmp/vf_mmap_exfat.dmg", "fsck_exfat"),
}
CHUNK = 64 * 1024


def sh(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def prepare(mount):
    d = os.path.join(mount, "dmgtest")
    subprocess.run(["rm", "-rf", d], check=False)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "safe.bin"), "wb") as f:
        f.write(b"S" * (1 << 20))
        f.flush()
        os.fsync(f.fileno())
    for i in range(6):
        with open(os.path.join(d, "meta%d.txt" % i), "w") as f:
            f.write("meta %d\n" % i)
            f.flush()
            os.fsync(f.fileno())
    os.sync()
    return d


def burst(d, seconds, res):
    path = os.path.join(d, "growing.bin")
    i = 0
    t0 = time.time()
    try:
        f = open(path, "wb", buffering=0)
        while time.time() - t0 < seconds:
            f.write(struct.pack(">Q", i) * (CHUNK // 8))
            i += 1
            if i % 20 == 0:
                with open(os.path.join(d, "new%d.txt" % i), "w") as g:
                    g.write("x" * 4096)
            time.sleep(0.004)
    except OSError as e:
        res["err"] = "%s (errno=%s)" % (e.strerror, e.errno)
    res["chunks"] = i


def attach_nomount(img):
    code, out, err = sh(["hdiutil", "attach", "-nomount", img])
    devs = [l.split()[0] for l in out.splitlines() if l.startswith("/dev/")]
    return devs, code, (out + err)


def verify(mount):
    d = os.path.join(mount, "dmgtest")
    out = {}
    if not os.path.isdir(d):
        out["dmgtest 目录"] = "不存在"
        return out
    names = [n for n in sorted(os.listdir(d)) if not n.startswith("._")]
    out["文件数"] = len(names)

    safe = os.path.join(d, "safe.bin")
    if os.path.exists(safe):
        data = open(safe, "rb").read()
        out["safe.bin"] = "%d 字节，%s" % (
            len(data), "完好" if data == b"S" * (1 << 20) else "内容被改")
    else:
        out["safe.bin"] = "丢失"

    g = os.path.join(d, "growing.bin")
    if os.path.exists(g):
        size = os.path.getsize(g)
        data = open(g, "rb").read()
        out["growing 大小"] = "%d 字节 = %.2f 块" % (size, size / CHUNK)
        bad = None
        for i in range(len(data) // CHUNK):
            if data[i * CHUNK:(i + 1) * CHUNK] != struct.pack(">Q", i) * (CHUNK // 8):
                bad = i
                break
        if bad is not None:
            out["撕裂点"] = "第 %d 块（偏移 %d）起内容不符" % (bad, bad * CHUNK)
        elif len(data) % CHUNK:
            out["撕裂点"] = "尾部残缺 %d 字节" % (len(data) % CHUNK)
        else:
            out["撕裂点"] = "无（逐块校验全部通过）"
    else:
        out["growing.bin"] = "丢失"
    return out


def run(fs, mode):
    mount, img, fsck = VOLS[fs]
    print("=" * 92)
    print("[%s] %s" % (fs, mode))
    print("=" * 92)

    if not os.path.isdir(mount):
        sh(["hdiutil", "attach", "-nobrowse", img])
        time.sleep(1.2)

    d = prepare(mount)
    print("  铺底完成：safe.bin(1MB 已 fsync) + 6 个 meta 文件")

    res = {}
    th = threading.Thread(target=burst, args=(d, 8.0, res), daemon=True)
    th.start()
    time.sleep(2.0)

    g = os.path.join(d, "growing.bin")
    print("  强断前 growing.bin = %d 字节" % os.path.getsize(g))

    if mode == "unmount-force":
        code, out, err = sh(["diskutil", "unmount", "force", mount], timeout=120)
        print("  diskutil unmount force -> %d %s" % (code, (out or err).strip()[:110]))
        time.sleep(0.5)
        code, out, err = sh(["hdiutil", "detach", "-force", img], timeout=120)
        print("  hdiutil detach -force  -> %d" % code)
    else:
        code, out, err = sh(["hdiutil", "detach", "-force", img], timeout=120)
        print("  hdiutil detach -force（不先 unmount） -> %d %s" % (
            code, (out or err).strip()[:110]))

    th.join(timeout=10)
    print("  写线程停在 %s 个块%s" % (
        res.get("chunks", "?"),
        ("，报错：" + res["err"]) if res.get("err") else ""))
    time.sleep(1.0)

    devs, code, raw = attach_nomount(img)
    print("\n  重新挂载(-nomount) -> %d  %s" % (code, " ".join(devs) or raw.strip()[:100]))
    if not devs:
        print("  ❗ 镜像无法挂上，可能已损坏到不可识别")
        return {"fs": fs, "mode": mode, "fatal": True}

    target = devs[-1]
    code, out, err = sh([fsck, "-n", target], timeout=300)
    print("\n  %s -n %s 退出码 = %d" % (fsck, target, code))
    body = (out + err).strip()
    for line in body.splitlines()[:16]:
        print("    " + line)

    code, out, err = sh(["diskutil", "mount", target], timeout=120)
    print("\n  diskutil mount -> %d %s" % (code, (out or err).strip()[:110]))
    time.sleep(1.0)

    info = verify(mount)
    print("\n  挂载后校验：")
    for k, v in info.items():
        print("    %-12s %s" % (k, v))

    return {"fs": fs, "mode": mode, "fsck": code, "fsck_out": body, "info": info}


if __name__ == "__main__":
    fs = sys.argv[1] if len(sys.argv) > 1 else "exfat"
    mode = sys.argv[2] if len(sys.argv) > 2 else "yank"
    run(fs, mode)
