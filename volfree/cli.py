"""命令行入口：参数解析、命令分发、以及发信号前的复核。"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from typing import Optional

from .assess import diagnose, enrich_and_assess
from .models import LEVEL_LABEL, LEVEL_ORDER, VERSION, Blocker, Volume
from .payload import blockers_to_json, volume_payload
from .policy import assess
from .proc import proc_executable_path, proc_start_time, proc_uid
from .render import Palette, human_size, render_report
from .scan import (capture_bounded, detect_access_level, list_volumes,
                   parse_mount_output, path_exists, resolve_volume, run,
                   scan_blockers)


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
