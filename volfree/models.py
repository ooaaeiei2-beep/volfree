"""数据结构与常量：Volume / Handle / Blocker，以及等级表。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


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
