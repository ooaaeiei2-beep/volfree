"""终端渲染。判定逻辑不在这里，这里只把已有结论排成可读的文本。"""

from __future__ import annotations

import os
import unicodedata
from typing import Optional

from .models import LEVEL_LABEL, VERSION, Blocker, Handle, Volume
from .payload import classify_kind


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
