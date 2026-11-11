#!/usr/bin/python3
"""多盘界面离屏渲染 / 回归用的模拟数据源。

配合 `VOLFREE_SCRIPT=<本文件> VOLFREE_RENDER=<png>` 使用：让真实 app 读这份假
JSON 渲染多块磁盘的界面，不必真的插三块盘。`app/shots.sh` 末尾用它生成
`shots/12-多盘.png`。

环境变量：
  VOLFREE_FAKE_VOLUMES=1|2|3|5   候选卷数量（默认 3）
  VOLFREE_FAKE_SAME_PID=1        两块盘出现同一 pid，验证速率键按 (device,pid) 隔离
  VOLFREE_FAKE_ABANDON=1         第二块盘造「放弃写入」，验证软确认文案带盘名

只供截图与回归使用，不参与构建和分发；判定逻辑仍全部在 volfree.py 里。
"""
import json
import os
import sys
import time

VERSION = "0.3.1"
ACCESS = {"level": "limited", "description": "只能看到本用户进程"}
TICK = int(time.time() * 1_000_000)


def handle(fd="3", access="r", ftype="REG", name="/Volumes/x/a.txt",
           exists=True, write_state="no"):
    return {"fd": fd, "access": access, "type": ftype, "name": name,
            "exists": exists, "write_state": write_state}


def flags(has_write=False, all_writes_unlinked=False, all_deleted=False,
          only_read=True):
    return {
        "has_write": has_write,
        "all_writes_unlinked": all_writes_unlinked,
        "has_unlinked_write": all_writes_unlinked,
        "has_uncertain_write": False,
        "all_deleted": all_deleted,
        "only_read": only_read,
        "other_user": False,
    }


def blocker(pid, command, exe, level, has_write=False, all_deleted=False,
            seed=1):
    # diskio 按调用时刻递增：两次采样做差商能得到可显示速率。
    # 系数按 seed 变，同一 pid 出现在两块盘上时速率不同，便于核对键是否隔离。
    diskio = seed * 100_000_000 + TICK * (2 + seed) if has_write else None
    return {
        "pid": pid, "command": command, "exe": exe, "uid": 501,
        "start_time": 1730000000, "level": level, "reason": "演示数据",
        "diskio_written": diskio,
        "flags": flags(has_write=has_write, all_deleted=all_deleted,
                       only_read=not has_write),
        "handles": [handle(access="w" if has_write else "r",
                           write_state="yes" if has_write else "no",
                           exists=not all_deleted)],
    }


def report(device, mountpoint, name, fstype, total, free, blockers,
           risky=0, caution=0, safe=0, never=0, writing=0, abandoned=0,
           tone="safe"):
    return {
        "version": VERSION,
        "volume": {
            "device": device, "mountpoint": mountpoint, "name": name,
            "fstype": fstype, "external": True, "virtual": False,
            "ejectable": True, "removable": True,
            "total_bytes": total, "free_bytes": free,
        },
        "access": ACCESS,
        "summary": {
            "total": len(blockers), "never": never, "risky": risky,
            "caution": caution, "safe": safe, "writing": writing,
            "abandoned_writes": abandoned, "complete": True,
        },
        "blockers": blockers,
        "clues": [], "warnings": [],
        "_tone": tone,
    }


def all_volumes():
    same = os.environ.get("VOLFREE_FAKE_SAME_PID") == "1"
    b_pid = 4211 if same else 5102
    b_disk = blocker(b_pid, "Python" if same else "mds_stores",
                     "/usr/bin/python3" if same
                     else "/System/Library/Frameworks/CoreServices.framework/mds_stores",
                     "SAFE", has_write=same, all_deleted=True, seed=2)
    return [
        report("/dev/disk4s1", "/Volumes/我的U盘", "我的U盘", "exfat",
               64000000000, 12000000000,
               [blocker(4211, "Python", "/usr/bin/python3", "RISKY",
                        has_write=True, seed=1),
                blocker(4220, "访达",
                        "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
                        "CAUTION")],
               risky=1, caution=1, writing=1, tone="danger"),
        report("/dev/disk5s1", "/Volumes/备份盘", "备份盘", "apfs",
               2000000000000, 800000000000,
               [b_disk,
                blocker(5103, "QuickLook",
                        "/System/Library/Frameworks/QuickLook.framework/QuickLook",
                        "SAFE", all_deleted=True)],
               safe=2, tone="caution"),
        report("/dev/disk6s2", "/Volumes/TimeMachine", "TimeMachine", "apfs",
               1000000000000, 300000000000, [], tone="safe"),
        report("/dev/disk7s1", "/Volumes/素材盘", "素材盘", "exfat",
               500000000000, 90000000000,
               [blocker(7101, "访达",
                        "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
                        "SAFE", all_deleted=True)],
               safe=1, tone="caution"),
        report("/dev/disk8s1", "/Volumes/归档", "归档", "apfs",
               4000000000000, 1500000000000, [], tone="safe"),
    ]


def clean(r):
    r = dict(r)
    r.pop("_tone", None)
    return r


def main():
    args = sys.argv[1:]
    n = int(os.environ.get("VOLFREE_FAKE_VOLUMES", "3"))
    volumes = all_volumes()[:n]
    if os.environ.get("VOLFREE_FAKE_ABANDON") == "1" and len(volumes) >= 2:
        b = volumes[1]
        b["summary"]["abandoned_writes"] = 1
        b["summary"]["writing"] = 1
        b["summary"]["safe"] = 1
        first = b["blockers"][0]
        first["level"] = "CAUTION"
        first["flags"]["has_write"] = True
        first["flags"]["all_writes_unlinked"] = True
        first["flags"]["has_unlinked_write"] = True
        first["flags"]["only_read"] = False
        first["diskio_written"] = 3 * 100_000_000 + TICK * 5
    if args and args[0] == "status":
        print(json.dumps({"version": VERSION, "access": ACCESS,
                          "volumes": [clean(v) for v in volumes]},
                         ensure_ascii=False))
        return
    if args and args[0] == "watch":
        vols = []
        for v in volumes:
            s = v["summary"]
            vols.append({"mountpoint": v["volume"]["mountpoint"], "tone": v["_tone"],
                         "total": s["total"], "risky": s["risky"],
                         "writing": s["writing"]})
        print(json.dumps({"version": VERSION, "volumes": vols}, ensure_ascii=False))
        return
    print(json.dumps({"ok": True, "action": "noop", "message": "", "pid": None,
                      "alive": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
