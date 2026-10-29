# 实验装置

`docs/文件系统语义核查.md` 第 6、7 条结论的可复现装置。四个脚本互相独立，
都只在临时 dmg 上操作，不碰真实磁盘。

## 0. 判定矩阵（`docs/判定情况全表.md` 第 5 节的来源）

```sh
python3 experiments/matrix.py
```

不需要建卷。构造 `Handle` / `Blocker` 组合后直接调 `volfree.assess()`，打印真实返回的
等级与理由，并与「应为」对照。输出三段：

- **A. 句柄层真值表** —— `link_count`/`exists` → `is_deleted`，以及 8 种句柄 →
  `write_state` 三态（含 `映射 未知 → unknown` 这一格，它是 R2 的根）；
- **B. 进程层 22 例** —— 覆盖普通 fd 四格、映射三态 × 有名/无名、混合写入、
  cwd、伪句柄、系统辅助进程、可重启扩展、受保护进程。末行打印「共 22 例，不符 N 例」，
  **N 必须为 0**；
- **C. `assess()` 的 10 条路径顺序** —— 含 A2 / A2b 的先后关系，改动顺序会让第 14 例
  （`映射无名·未知`）从 CAUTION 跳到 RISKY。

改判定逻辑后必须整段复跑。2026-10-28 修复 R1/R2/R3 后结果为 22/22 相符。

## 先建测试卷

```sh
hdiutil create -size 200m -fs APFS  -volname VfMmapApfs  -type UDIF /tmp/vf_mmap_apfs.dmg
hdiutil create -size 200m -fs ExFAT -volname VfMmapExfat -type UDIF /tmp/vf_mmap_exfat.dmg
hdiutil attach -nobrowse /tmp/vf_mmap_apfs.dmg
hdiutil attach -nobrowse /tmp/vf_mmap_exfat.dmg
```

挂载点固定为 `/Volumes/VfMmapApfs` 与 `/Volumes/VfMmapExfat`，脚本里写死了；
换卷名要同步改 `probe.py` 的 `VOLUMES` 和 `damage.py` 的 `VOLS`。

## 1. mmap 写入的可见性（文档第 6 条）

```sh
python3 experiments/probe.py
```

对 7 种写法 × 2 种文件系统，各起一个子进程持续写同一个文件，在子进程存活期间调用
`volfree.scan_blockers()` + `assess()`，打印 volfree 真实给出的定级，并与「文件内容是否
在变」的事实对照。

修复 R1/R2/R3 之后的期望值（两种文件系统一致）：

| 写法 | 文件在变 | 级别 |
|---|---|---|
| `write` / `mmap_keep` / `mmap_close` / `pymmap_close` | 是 | RISKY |
| `mmap_ro` / `mmap_ro_close` | 否 | CAUTION（**不得**升 RISKY） |
| `mmap_close_unlink` | 是 | CAUTION（**不得**落 SAFE） |

若 `mmap_close` 回到 CAUTION，说明 `proc_pidinfo` 那条路断了；若 `mmap_close_unlink`
回到 SAFE，说明「未知/映射」又被当成只读了。

`writer.py` 是子进程侧，用 `ctypes` 直调 `mmap(2)`——**不能用 Python 的 `mmap` 模块**，
因为它在 Unix 上会保留 fd，构造不出「映射在、fd 已关」的缺口。

它还有两个非映射模式，供 `app/shots.sh` 摆截图用：

| 模式 | 用途 |
|---|---|
| `hold_ro` | 只读持有、不写。造判定的「黄」格（有句柄、只读、有名字） |
| `bulk` | 每轮写 256 KB（实测 828–936 KB/s）。让界面上的写入速率过得了 2 KB/s 门限 |

## 2. 映射保护位（文档第 6 条的修复依据）

```sh
python3 experiments/writer.py /Volumes/VfMmapApfs/rg.bin mmap_close 6 &
python3 experiments/region_probe.py <pid> rg.bin
```

输出 `prot=rw- max=rwx`；换成 `mmap_ro_close` 则输出 `prot=r-- max=r-x`，
与 `vmmap <pid>` 的 `rw-/rwx`、`r--/r-x` 逐字一致。

## 3. 强卸载的损坏形态（文档第 7 条）

```sh
python3 experiments/damage.py exfat unmount-force
python3 experiments/damage.py apfs  unmount-force
```

铺底 → 持续写入且不 fsync → 2 秒后 `diskutil unmount force` → 以 `-nomount` 重挂拿设备节点
→ `fsck -n <设备节点>` → 挂载后逐块校验。

注意 fsck 必须对着**设备节点**（`/dev/disk9s1`）跑，对着 `.dmg` 文件跑会报
「Main boot region is invalid」，那是假阳性，不是卷损坏。

`yank` 模式（不先 unmount，直接 `hdiutil detach -force`）在有进程持卷时会失败（exit 1），
所以拔线/断电场景无法用 dmg 复现。
