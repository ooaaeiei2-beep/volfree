#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 volfree/ 包拼回单文件 volfree.py。

**为什么要有这一步**：源码拆成包是为了好读好改，但分发物必须是单文件——
`app/Sources/VolfreeApp/Runner.swift` 只认 `Bundle.main.resourceURL/volfree.py`，
README 的 CLI 下载位也是单文件。两个诉求只能靠「源码分模块 + 生成单文件」同时满足。

用法：
    python3 tools/bundle.py          重新生成仓库根的 volfree.py
    python3 tools/bundle.py --check   只比对，不写盘（CI / 单测用）

生成规则：
  1. 模块按固定顺序拼接，顺序即定义顺序（models → proc → scan → policy → assess → payload → render → cli）
  2. 每个模块的 `import` / `from` 行全部剥掉，统一由文件头的 import 块提供
  3. 模块文档字符串剥掉（它描述的是模块，不是这个单文件）
  4. 末尾补 `if __name__ == "__main__"` 入口

所以模块之间**只能**用 `from .x import y` 然后裸名调用，
不能写 `proc.proc_start_time(...)` 这种带模块前缀的写法——拼成一个文件后那个前缀就不存在了。
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "volfree"
OUTPUT = ROOT / "volfree.py"

ORDER = ["models", "proc", "scan", "policy", "assess", "payload", "render", "cli"]

HEADER = '''#!/usr/bin/env python3
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
'''

FOOTER = '''
if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\\n已中断。", file=sys.stderr)
        sys.exit(130)
'''

BANNERS = {
    "models": "数据结构",
    "proc": "libproc 封装",
    "scan": "扫描与解析",
    "policy": "风险定级",
    "assess": "诊断组装",
    "payload": "JSON 输出",
    "render": "终端输出",
    "cli": "处置与命令",
}


def strip_module(text: str) -> str:
    """去掉模块文档字符串与所有 import 行。"""
    tree = ast.parse(text)
    lines = text.splitlines()
    drop = set()

    if tree.body and isinstance(tree.body[0], ast.Expr):
        node = tree.body[0]
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            drop.update(range(1, node.end_lineno + 1))

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            drop.update(range(node.lineno, node.end_lineno + 1))

    kept = [ln for i, ln in enumerate(lines, start=1) if i not in drop]
    # 去掉因剥离产生的连续空行
    out: list[str] = []
    for ln in kept:
        if ln.strip() == "" and out and out[-1].strip() == "":
            continue
        out.append(ln)
    return "\n".join(out).strip("\n")


def build() -> str:
    parts = [HEADER.rstrip("\n")]
    for name in ORDER:
        path = PKG / f"{name}.py"
        body = strip_module(path.read_text(encoding="utf-8"))
        banner = f"# {'─' * 26} {BANNERS[name]} {'─' * 26}"
        parts.append(banner)
        parts.append(f"# 源文件：volfree/{name}.py")
        parts.append(body)
    parts.append(FOOTER.strip("\n"))
    return "\n\n\n".join(parts) + "\n"


def main(argv: list[str]) -> int:
    text = build()
    check = "--check" in argv

    if check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current == text:
            print("volfree.py 与 volfree/ 包一致")
            return 0
        print("volfree.py 已落后于 volfree/ 包，请重新生成：python3 tools/bundle.py",
              file=sys.stderr)
        return 1

    OUTPUT.write_text(text, encoding="utf-8")
    OUTPUT.chmod(0o755)
    print(f"已生成 {OUTPUT}（{len(text.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
