# volfree 菜单栏应用

`volfree.py` 的界面外壳。**判断全部留在 Python 里**，这里只做「调用 + 呈现 + 触发动作」。

## 为什么是 SPM 而不是 .xcodeproj

本机只装了 Command Line Tools，没有完整 Xcode（`xcodebuild` 不可用）。
所以用 Swift Package 编译，再由 `build.sh` 手工组装 `.app` 包。
好处是构建过程全是文本，能进版本库、能在终端复跑。

```sh
cd app
./build.sh                 # 产出 build/Volfree.app
open build/Volfree.app     # 菜单栏出现一个硬盘图标
```

`build.sh` 会把 `../volfree.py` 一起拷进 `Contents/Resources/`，
所以打包后的 app 不依赖工作区路径。

版本号只认一个来源：`../volfree.py` 里的 `VERSION`，构建时写进 `Info.plist`。
（原先 `Resources/Info.plist` 写死 `0.1.0`，和 CLI 的版本对不上。）

要发版就打 dmg：

```sh
./make_dmg.sh              # 产出 build/Volfree-<版本>.dmg
```

dmg 里是 `Volfree.app` 加一个指向 `/Applications` 的软链。**只打包不签名**——
本机没有 Developer ID 证书，app 是 adhoc 签名的，下载方首次打开会被 Gatekeeper 拦，
处理办法写在仓库根 README 的「下载」一节。

## 想亲眼看一眼红态

手上没有「正在被写的移动硬盘」时，用这个造一块：

```sh
./demo.sh          # 造一块 200 MB 测试卷，持续写入 60 秒
./demo.sh 180      # 写 180 秒
```

它只在 `/tmp` 下建临时 dmg，退出（含 Ctrl-C）时自动推出并删除。
写入期间点菜单栏图标，就能看到红色「先别拔」和实时速率。

## 结构

| 文件 | 职责 |
|---|---|
| `main.swift` | `NSStatusItem` + `NSPopover` 外壳，插拔盘监听，轮询节流 |
| `AppModel.swift` | 状态机：选中卷、三态推导、动作编排 |
| `Runner.swift` | 调 `volfree.py` 子进程，解 JSON；双管道后台读，避免 64KB 死锁 |
| `Models.swift` | JSON 契约（与 `volume_payload()` 一一对应） |
| `PopoverView.swift` | 面板视图，对应 `design/popover.html` |
| `SettingsView.swift` | 设置窗口：权限范围、登录项、关于 |
| `Theme.swift` | 三态配色与尺寸常量（数值取自原型的 CSS 变量）+ 菜单栏图标的绘制 |
| `make_icon.swift` | 图标生成器：用 AppKit 渲染 SF Symbol，逐尺寸出图 |
| `make_icon.sh` | 编译并运行 `make_icon.swift`，打包 `Resources/AppIcon.icns`（不参与构建，产物入库） |
| `shots.sh` | 建临时卷 → 逐态渲染 → 卸卷 → 拼图，重渲 `shots/` 的入口 |
| `compose_shots.py` | 把渲染出的面板拼成 `shots/` 里的对照图（不参与构建） |

## 版式上两个容易踩的坑

1. **`Group` 上的 modifier 会逐个作用到子视图**。`reportBody` 返回的是「判定卡 + 主按钮 + …」
   一组视图，给 `Group` 加 `.padding(.bottom, 11)` 会给**每个**子视图各加一份，
   结果判定卡与按钮之间被塞了两份间距。主体必须包一层 `VStack(spacing: 0)`，
   modifier 才只作用一次。
2. **主按钮下面必须留出呼吸位**。少了这一句，按钮底边会直接贴到面板底部那条横线上，
   看起来像"压线"。`Metrics.sectionGap` 就是为这个准备的，全面板统一 11pt。

## 怎么退出这个 app

它是 `LSUIElement`：**没有 Dock 图标、没有菜单栏**，所以系统不给它任何现成的退出入口。
不自己补一个，就只能去活动监视器杀进程。给了三处：

| 位置 | 说明 |
|---|---|
| 面板底栏右侧 `⏻ 退出` | 最容易找到的那个 |
| 状态栏图标**右键** | 打开面板 / 设置… / 退出 volfree |
| 设置窗口底部 `退出 volfree` | 和「关于」放一起，符合直觉 |

另外面板有焦点时 `⌘Q` 也生效——accessory 应用没有菜单栏，系统默认不处理 ⌘Q，
所以在 `NSEvent` 的本地监听里手动接了一下。

底栏那个按钮带电源图标，是刻意的：中文里「退出 / 推出」只差一个字，
而这个面板中间正好有一个大的「推出」按钮，光靠文字容易看错。

## 菜单栏图标的颜色

不能按 `NSApp.effectiveAppearance` 判断深浅。系统的「自动」外观会让**菜单栏**跟着壁纸变深，
而 app 自己仍算浅色——那时深色图标就沉进深色菜单栏里，几乎看不见（用户实测反馈过两次）。

### 为什么最后没用 `contentTintColor`

第一版的做法是：给按钮一张 template 图，再把**动态 `NSColor`**（`NSColor(name:handler:)`）
设给 `contentTintColor`，指望 AppKit 在绘制时按当时的外观重算。**这条路是死的**，实测三件事：

1. 动态色在**赋值那一刻**就按 `NSApp` 的外观解析了，之后不再重算。
2. `NSApp` 和状态栏按钮的外观**可以不一致**。本机就是：系统浅色模式 + 深色壁纸，
   `NSApp = Aqua` 而按钮 `= VibrantDark`。
3. 更要紧的：`contentTintColor` 在 `NSStatusBarButton` 上**不管给什么颜色都画成纯黑**。

第 3 条是拿 `cacheDisplay` 把按钮实际画出来的像素抓下来测的：

| 做法 | 画出的主色 |
|---|---|
| template 图，不设 tint | `224,224,224`（可见）|
| template + **动态** tint | **`0,0,0`** |
| template + **具体白** tint | **`0,0,0`** |
| template + **具体黄** tint | **`0,0,0`** |

三种颜色全是纯黑，与用户截图里那个图标完全一致（截图里最暗 200 个像素**恰好全是 `(0,0,0)`**）。
所以上一版不是参数没调好，是这条路根本走不通。

### 现在的做法：两条路分开用

**常态走 template 图，颜色完全交给系统**——这是 Apple 文档里的标准做法，
明暗、增强对比度、降低透明度全由 AppKit 处理，我们一行都不用管，换肤也不用监听。

**黄 / 红：硬盘保持黑白，右上角加一个彩色小圆点。**

为什么不整盘染色：`externaldrive.fill` 的盖子与机身之间是一条**透明缝隙**，
两边都染上色之后这条缝隙就退化成一根细线，16px 下整个图标糊成一个色块，
硬盘的形状信息全丢了——用户说"丑"就是这个机制，不是色值没调好。
保留黑白机身、只加一个点，形状和信号都在。

| 参数 | 值 | 依据 |
|---|---|---|
| 圆点直径 / 图标高度 | `0.30` | 16px 下是 5px。再小看不清，再大会咬掉盖子一角 |
| 圆点与字形之间的透明缝隙 | `0.9` pt | 挖出来的缝隙透出菜单栏本身，深浅背景上都成立，不用猜背景色 |
| 圆点位置 | 贴右上角，内收 `0.2` pt | 不内收会被画布裁掉一角 |

**几何是怎么保住的**：底图仍用 `paletteColors` 渲染（AppKit 自己画），
只额外做"挖环 + 画点"。实测在**真实缩放上下文**里（1x / 2x / 3x），
角标区外的字形 alpha 与不加角标时**逐像素相同（最大差 0）**。

> 注意：用 `tiffRepresentation` 转存出来比对，会看到最大差 153、平均差 20。
> 那是转存时按 1x 栅格化造成的**假差异**，屏幕上不存在——
> 判断这类问题必须设 `rep.size` 用真实缩放上下文，别拿 tiff 直接比。

配色策略：**常态黑白，只有黄/红才上色**。菜单栏里彩色图标在一排系统图标中很突兀，
而"没事"这件事不需要用颜色强调；真要提醒时才值得抢视线。

| 色调 | 深色菜单栏 | 浅色菜单栏 |
|---|---|---|
| 常态 | 系统填（白） | 系统填（黑） |
| 黄（有人占着） | `#FFCC49` | `#6B3F00` |
| 红（正在写） | `#FF847A` | `#9E2119` |

试过但没采用的方案（都在 16px 下实测过）：

| 方案 | 结果 |
|---|---|
| 整盘染色 | 糊成一个色块，盖子缝隙被吃掉 |
| 边缘描边（外扩 1px 上色） | 变成"粉边/发光"，16px 下像失焦，最差 |
| 只给指示灯上色 | 指示灯孔在 16px 下只有约 1px，看不见 |
| 空心轮廓版（`externaldrive`） | 32px 精致，16px 线条断续 |
| 系统角标变体 `.badge.exclamationmark` | 角标在**左下**且占 1/3 宽，16px 下感叹号糊成一团 |

### 换肤时怎么知道要重画

**常态不用管**——template 图自己会适应，`applyStatusIcon` 里常态的缓存键不含外观，
换肤时直接跳过重画。

只有黄/红才需要重画，而这时 `NSApp` 的外观可能压根不变（浅色模式 + 深色壁纸就是），
收不到任何通知。但 `NSView.viewDidChangeEffectiveAppearance()` 会沿视图树往下传——
在状态栏按钮上挂一个 1×1 的 `AppearanceProbe`，把"外观变了"转成回调。

**回调要防抖 0.25 秒再画。** 启动那一下它会连着触发十几次，在
`VibrantLight` / `DarkAqua` / `VibrantDark` 之间反复横跳（AppKit 自己建状态栏窗口的过程）。
逐次重画不但浪费，更要命的是**万一最后一次停在浅色，图标就又被画成深色**。
实测加防抖前重绘 13 次、加防抖后 2 次（初始一次 + 稳定后一次）。

用 `VOLFREE_DEBUG=1` 跑起来会打印实际取色：

```
[volfree] 菜单栏配色 NSAppearanceNameAqua：常态 #1E1E1E  黄 #6B3F00  红 #9E2119
[volfree] 菜单栏配色 NSAppearanceNameDarkAqua：常态 #FFFFFF  黄 #FFCC49  红 #FF847A
[volfree] 菜单栏图标已重绘 常态 template 图（颜色交给系统）
```

黄/红两态没有真实告警时也能核对，`VOLFREE_FORCE_TONE` 会把色调钉住：

```sh
VOLFREE_DEBUG=1 VOLFREE_FORCE_TONE=caution build/Volfree.app/Contents/MacOS/Volfree
```

菜单栏截不了图（本机没有录屏权限），所以另有一个开关把**画出来的那张图**落盘，
直接查像素——这是唯一能确认"真的画成白色了"的办法：

```sh
VOLFREE_DEBUG=1 VOLFREE_DUMP_STATUS_ICON=/tmp/icon.png build/Volfree.app/Contents/MacOS/Volfree
```

实测落盘的 21×16 图，不透明像素 RGB 全部是 `255,255,255`。

## 图标

`Resources/AppIcon.icns` 由 `make_icon.sh` 生成，产物入库，构建时不重新生成：

```sh
./make_icon.sh                 # 内部会 swiftc 编译 make_icon.swift
```

### 设计来源：面板头部那个 30pt 小图标

图标不再"设计"，而是**把面板头部那个小图标按 app 图标比例放大**。那个图标是
蓝色对角渐变方块 + 白色 `externaldrive.fill`，用户看过实物之后直接指定了它。

这么做有个额外好处：**不存在"复刻得像不像"的问题**。符号由 AppKit 渲染同一个 SF Symbol，
和面板里那个是同一份数据。之前四轮手绘（平白 ⏏ → 等距硬盘盒 → 精修 ⏏ → 材质稿）
全部作废，归档在 `deprecated/`，那里记着手绘路线真正的教训。

比例不是估的，是从实机截图上量出来的（底板 60×60）：

| 参数 | 值 | 怎么来的 |
|---|---|---|
| 符号宽 / 底板宽 | `48.3%` | 截图实测 29 / 60 |
| 符号高 / 底板高 | `36.7%` | 截图实测 22 / 60 |
| 符号中心偏移 | `x -0.5 / y 0` | 基本正居中，只是留白左右不完全对称 |
| 渐变 | `#7897BD → #48688F`，`angle -45` | 与面板头部同一对色、同一方向 |
| 底板形状 | 超椭圆 `n=5`，占 `824/1024` | macOS 图标惯例，等效圆角比 `0.221`，官方模板是 `0.225` |

### 两个必须用"墨迹"而不是 `image.size` 的地方

SF Symbol 的 `image.size` **含四周留白**。第一版按它缩放，符号只到底板宽的 **39.4%**，
比目标的 48.3% 明显偏小；按它居中，符号还会偏一点。

所以 `make_icon.swift` 里 `inkBounds()` 会把符号画进一张 1024 的位图，
逐像素扫 alpha 求**实际墨迹** bbox，再据此算缩放与偏移。修完实测 48.1%，
中心偏移 `x -1.0 / y +0.5`——与目标 48.3% / `x -0.5` 的残差来自 SF Symbol 版本的字形差异。

> 量墨迹时不能 `image.draw(in:)` 撑满整块画布，那样会被缩放，量出来的不是原尺寸。
> 必须 `image.draw(in: NSRect(origin: .zero, size: image.size))` 按原始尺寸画在角落。

### 小尺寸要把符号放大

16px 下底板只有 12.9px 宽，按 48.3% 算符号只有 **6.2px**——上盖分隔线和指示灯会一起糊掉，
整个符号读起来只是一个白块（实测过，见 `shots/11-图标.png`）。

所以按尺寸给三档比例，**每个尺寸独立渲染**，不是把 1024 缩下去：

| 档位 | 符号宽 / 底板宽 | 原因 |
|---|---|---|
| ≥48px | `48.3%` | 与面板头部一致 |
| 24–47px | `56%` | 32px 下 48.3% 已勉强可辨，放大后上盖线更稳 |
| <24px | `66%` | 16px 下必须牺牲留白换"认得出" |

小尺寸放大后 16px 能看到上盖分隔线了。这一档是图标唯一真正难的地方，
改任何参数都要单独看它。

`build.sh` 会检查 `AppIcon.icns` 是否存在、是否进了包，缺了直接失败——
免得又 build 出一个没图标的 app。`Info.plist` 里挂的是 `CFBundleIconFile=AppIcon`。

### 预览与校验

`shots/11-图标.png` 是各档尺寸的预览（16/32/64/128/256，浅色深色各一行）。
`compose_shots.py` **直接拆 `Resources/AppIcon.icns`** 取图，不另画一遍——
预览和成品必须是同一份数据，否则改了生成脚本却忘了重新打包，预览会照样好看。

拆出来的是 `@2x` 条目：它和母版逐像素一致，是 Retina 屏真正读的那一组。
1x 的 16/32 条目有偏（见下），拿它做预览会看到并不存在的缺陷。

验证系统到底读到了什么，不要靠肉眼看 Finder：

```sh
iconutil -c iconset build/Volfree.app/Contents/Resources/AppIcon.icns -o /tmp/check.iconset
```

逐个条目与 `build/AppIcon.iconset/`（打包的输入）比像素，已实测：

| 条目 | 结果 |
|---|---|
| `icon_16x16@2x.png`(32) / `icon_32x32@2x.png`(64) | 逐像素一致 |
| `icon_128x128` 及以上全部条目 | 逐像素一致 |
| `icon_16x16.png`(16) / `icon_32x32.png`(32) | **有偏，最大差 184/198** |

后两个是 icns 的旧式 `is32` 条目（24 位 RLE + 独立 alpha 掩码），
`iconutil` 写入时会把 RGB 按 alpha **反除**（实测 `46 → 97`，比值正好 `255/121`）。
试过喂预乘 alpha 的 PNG 来抵消，16px 有改善但 32px 反而更差——两种尺寸落到了不同的
icns 元素类型，没有统一解法。

**影响可忽略**：Retina 屏渲染 16/32pt 时读的是 `@2x` 条目（逐像素一致），
1x 条目只在非 Retina 显示器上生效。偏差也只出现在方形边缘的半透明像素上，
表现为一圈极淡的亮边，在浅色背景上不可见、在深色背景上像一道描边高光。

另外 `NSWorkspace.shared.icon(forFile:)` 能确认系统确实挂上了图标（会返回 32 个表示）。

> 注：用 `NSWorkspace` 取回再重绘时，顶部会出现一条 20px 的白色渐变边——
> 那是系统给 app 图标加的顶部高光（IconServices 侧），**不在 icns 文件里**。
> 判据就是上面那条：文件本身与母版逐像素一致，不透明区 bbox 也一致（`y 100..923 / x 100..923`）。


## 两个界面上的「不敢替你下结论」

这两处不是文案修饰，是判断本身的边界：

1. **权限受限时的绿态**：`access.level` 是 `limited` 时，lsof 照常退出，
   只是看不到系统进程。「没有占用」和「看不到占用」这时无法区分，
   所以判定文案从「可以拔了」降级为「未发现占用」，
   理由换成「但当前权限看不到系统进程，仍可能有其他程序占着」。
2. **登录项开关**：`SMAppService.mainApp` 要求 app 装在 `/Applications` 下，
   所以开关是否可用只看**位置**（`Bundle.main.bundlePath` 前缀），不看 `status`。
   从 `build/` 直接运行时置灰并写明原因，而不是让用户点一下再看到失败。

   > 这里踩过一个坑，值得记下来：原先的判据是 `status != .notFound`。
   > 但 `.notFound`(3) 恰恰是**尚未注册**时的正常返回值（不是 `.notRegistered`(0)），
   > 于是开关被永久禁用——用户根本点不到 `register()`，也就永远注册不上。
   > 实测 `register()` 一次就成功，状态随即变成 `.enabled`(1)。
   > 诊断命令：`Volfree.app/Contents/MacOS/Volfree --login-item-probe`
   > 会打印 Bundle 身份、位置、当前状态和 `register()` 的真实报错。

   > **重装 app 会把登录项顶掉。** 重新 `rm -rf /Applications/Volfree.app` 再拷一份进去之后，
   > `status` 会退回 `notRegistered`——BTM 里的登记是跟着 app 实例走的。
   > 所以每次覆盖安装后，要去设置里重新勾一次「登录时自动启动」。
   > 验证：`sfltool dumpbtm | grep -A6 "Name: volfree"` 看到
   > `Disposition: [enabled, allowed, notified]` 才算生效。

## 界面只读机器字段，不解析中文

`volfree.py status --json` 的每个进程带 `flags`（`has_write`、`all_writes_unlinked`…），
每块卷带 `summary`（`risky` / `caution` / `abandoned_writes`…）。
界面据此决定颜色、文案和按钮，**不去 grep `reason` 里的中文**——
否则改一句提示语就会把界面弄坏。

## 界面调用的命令

| 场景 | 命令 |
|---|---|
| 面板打开时轮询 / 首次加载 | `volfree.py status --json` |
| 面板关闭时更新菜单栏图标 | `volfree.py watch --volume <挂载点> --json`（可重复 `--volume`） |
| 逐项「结束…」 | `volfree.py end <pid> --volume <卷> --json` |
| 红态「仍要结束…」 | 同上再加 `--allow-risky --force` |
| 绿态主按钮 | `volfree.py eject <卷> --json` |
| 黄态主按钮 | 逐个 `end` 完再 `eject` |

黄态刻意不走 `release --yes`：逐条调用才能把**每个进程的失败原因**分别回显。

## 开发用开关（环境变量，不影响正常使用）

| 变量 | 作用 |
|---|---|
| `VOLFREE_SCRIPT` | 指定 `volfree.py` 路径（默认用包内副本） |
| `VOLFREE_PYTHON` | 指定解释器（默认 `/usr/bin/python3`） |
| `VOLFREE_DEBUG=1` | 把启动流程写到 stderr |
| `VOLFREE_AUTOOPEN=1` | 启动后自动展开面板，并让它常驻（便于人工看） |
| `VOLFREE_RENDER=<png>` | 把面板离屏渲染成图片后退出 |
| `VOLFREE_RENDER_SETTINGS=1` | 配合上一项，改为渲染设置窗口 |
| `VOLFREE_EXPAND=1` | 配合渲染，默认展开进程列表 |
| `VOLFREE_RENDER_DELAY=<秒>` | 出图时间，默认 3.2；核对「到点自动收起」时调大 |
| `VOLFREE_SIMULATE_EJECT=<名字>` | 没有真盘也能摆出「已推出」卡片，仅配合渲染用 |
| `VOLFREE_FORCE_TONE=caution\|danger` | 钉住菜单栏色调，没有真实告警也能核对黄/红两态 |
| `VOLFREE_DUMP_STATUS_ICON=<png>` | 把菜单栏图标落盘查像素（菜单栏截不了图） |
| `VOLFREE_OPEN_PANEL=1` | 启动后自动弹出面板，用于测量"使用时"的真实占用 |

`VOLFREE_RENDER` 是为**没有录屏权限**的环境准备的：它渲染的就是真实视图代码，
不是另画一套。`shots/` 里的图就是这么来的。

```sh
VOLFREE_RENDER=/tmp/x.png build/Volfree.app/Contents/MacOS/Volfree
VOLFREE_RENDER_SETTINGS=1 VOLFREE_RENDER=/tmp/s.png build/Volfree.app/Contents/MacOS/Volfree
```

渲染前会先 `model.refresh()` 走一遍真实的 `status --json`，
所以图片反映的是当前机器状态，不是假数据。

## 每次轮询的成本

两档轮询，按面板开合切换：

| 时机 | 命令 | 间隔 | 单次成本 |
|---|---|---|---|
| 面板打开 | `status --json`（全量） | 2.5s | 墙钟 ~690ms / CPU ~670ms |
| 面板关闭 | `watch --json`（只查占用） | 5s | 墙钟 ~254ms / CPU ~243ms |
| 没插盘 | 不起计时器 | — | **0** |

实测（`proc_pid_rusage` 采样，`ps` 在沙箱里不可用）：

| 场景 | 结果 |
|---|---|
| 面板关闭（空闲 90 秒） | App 自身 CPU **0.00%**，footprint **15.4MB** 零增长 |
| 面板打开（持续轮询 90 秒） | App 自身 CPU 0.01–0.06%，footprint 25MB 零增长 |
| 折算占空比 | 面板关闭约 **4.9%**、面板打开约 **13%** 个核心（仅挂着盘时） |

内存不涨，这点是稳的。开销全在**子进程**里：每次轮询要起 Python + 若干 `diskutil` + `lsof`，
所以活动监视器里看到的是 Volfree 占 0%，一堆短命进程一闪而过。

### 为什么空闲要单独开一条 `watch` 命令

因为菜单栏图标得能自己变色。早先只在面板打开时轮询，结果是：**插着盘、有程序占着，
图标还是白的**——没人扫，自然没人发现。

但直接空闲跑 `status` 太贵（13% 个核心）。`status` 里约 270ms 花在两次 `diskutil` 上，
那是用来拿卷的身份（外接/可推出/文件系统）的，而**卷的身份在插拔之间不会变**。
所以 `watch` 只跑 `mount` + `lsof`，单次从 690ms 降到 254ms，空闲才敢开。

`watch` 刻意不判外接、不判磁盘映像、不算容量——这些字段只用于显示和推出，
不参与占用定级（`scan_blockers` 只用到 mountpoint）。色调规则与界面
`Models.swift` 的 `tone` 完全一致，由单测 `TestWatchCommand` 守着。

### 已经做过的：把两次 diskutil 并发起来

整盘名可以从设备名直接推出来（`disk5s1 → disk5`），所以"先读卷级 plist 拿到
`ParentWholeDisk` 再查整盘"这个串行依赖可以拆掉，两次查询同时发起。

实测（2 个卷、8 轮交替采样取中位）：

| | 墙钟 | CPU |
|---|---|---|
| 串行 | 792ms | 564ms |
| 并发 | **593ms** | 572ms |

**省的是等待，不是算力**——CPU 在噪声范围内持平（并发甚至略高一点，线程与 diskutil
内部竞争的开销）。所以这一步改善的是"面板打开后多久出数据"，**不降低占用**。
判定结果逐字段比对过，新旧完全一致。

### 想真正降 CPU 只有两条路

1. **常驻 worker**：省掉每次 80ms 的 Python 启动（约占 14%）。代价是要管进程生命周期。
2. **减少 `diskutil` 调用**：整盘级那次是为了拿 `VirtualOrPhysical` / `MediaName` 判
   「是否磁盘映像」，卷级 plist 里没有这两个字段，省不掉。要省就得换判据来源
   （`ioreg` 31ms 能拿到 `Removable`/`Ejectable`，但拿不到 `Internal`），
   属于**改判定语义**，按项目规矩要先读 `docs/文件系统语义核查.md` 并复跑 22 例矩阵。

## 写入速率怎么来的

`lsof` 读不到 mmap 句柄的可写性，也读不到写入量；而 mmap 写入**不改变文件长度**，
看 size 永远零增长。所以走 `proc_pid_rusage(pid, RUSAGE_INFO_V2)` 的
`ri_diskio_byteswritten`（进程累计写盘字节数），两次采样差商得速率。

采样间隔限制在 `0.4s ≤ Δt ≤ 10s`：太短会被调度抖动放大，太长则数值失去时效。
进程刚出现、只有一个样本时**不显示速率**，而不是显示 0——0 会被误读成「已经写完」。

还有一道 `≥ 2 KB/s` 的门限：低于这个量级时，进程的写盘计数和系统背景噪声分不开，
显示出来只会误导。`experiments/writer.py` 的 `bulk` 模式就是为验证这一点加的——
`write` 模式每轮只写 16 字节，`diskio` 上只有 1.5 KB/s，落在门限下面看不到速率。

## 截图

`shots/` 里的图都是 `VOLFREE_RENDER` 真机渲染的，不是设计稿。

重渲全部图只要一条命令：

```sh
./shots.sh
```

它会建一块 200MB 的临时 dmg 挂在 `/Volumes/VfShot`，用 `experiments/writer.py`
造出各态需要的持有进程，逐张渲染，最后卸卷删盘——**失败也会收摊**（trap 里 `set +e`，
否则中途一步失败会让后面的清理被跳过，留下挂着的卷和 200MB 的 dmg）。

为什么不手工摆：这些图依赖「卷处于什么状态」，手工摆一遍容易漏图，
也容易把临时卷忘在 `/Volumes` 里。另外后台进程在工具调用之间会被回收，
所以必须一次跑完，不能分几条命令摆一张渲一张。

单张调试时直接调：

```sh
VOLFREE_RENDER=/tmp/x.png build/Volfree.app/Contents/MacOS/Volfree
VOLFREE_RENDER_SETTINGS=1 VOLFREE_RENDER=/tmp/s.png build/Volfree.app/Contents/MacOS/Volfree
```

拼图由 `compose_shots.py` 做（`shots.sh` 末尾会自动调用）：

```sh
python3 compose_shots.py       # 重建 四态对照 / 06 / 07 / 11-图标
```

注意系统自带的 `/usr/bin/python3` 没有 Pillow，拼图要另找解释器；
`shots.sh` 会自动探一个能 `import PIL` 的，也可以用 `VOLFREE_PYTHON_PIL` 指定。

| 文件 | 内容 |
|---|---|
| `01-绿.png` … `05-黄-展开进程.png` | 四态与黄态展开 |
| `06-绿-权限受限与设置.png` | 权限受限时绿态文案降级 + 设置窗口登录项置灰 |
| `07-红-写入速率.png` | 红态收起/展开，带真实速率 |
| `08-按钮压线-前后.png` | 主按钮与底栏横线的间距修复前后 |
| `09-已推出.png` | 推出后的一次性确认卡片（6 秒后自动收起） |
| `10-空态.png` | 没插盘 |
| `11-图标.png` | `AppIcon.icns` 各档尺寸的预览（直接拆包内 icns，不另画） |
| `四态对照.png` | 四态并排 |

`06a/06b`、`07a/07b` 是拼 `06`、`07` 用的单张源图，留在目录里方便单独引用其中一张。

## 「已推出」为什么只有 6 秒

盘拔走以后 `reports` 里就再没有它了，没有任何事件能"通知"界面该收起这张卡，
于是会一直停在上一块盘的结果上——换一块盘插进来也还显示旧的。
所以它被当成**一次性确认**而不是一种常驻状态：6 秒后自己收，
期间如果同一块盘又挂回来（说明推出没真正生效）则立即收。

## 已知未做

- 多卷时靠头部下拉切换，没有做「全部卷」总览
- 速率只按进程给，没做「整卷合计写入速度」
- 面板关闭时只跑 `watch`（查占用、不管速率），所以关着的时候看不到实时写入速率
- 空闲轮询固定 5 秒，没做成自适应（比如有写入时加快、安静时放慢）
- 未做签名分发：adhoc 签名，换机器需重新构建；登录项也需要 app 在
  `/Applications` 下才能注册
- **开机是否真的会被拉起，没验证过**——需要重启才能确认。目前只能确认到系统层
  已登记且启用：`sfltool dumpbtm` 里能看到
  `Name: volfree / Identifier: 2.com.xushuda.volfree / Disposition: [enabled, allowed, notified]`，
  `SMAppService.mainApp.status == .enabled`。adhoc 签名的 app 在这一步之后的实际拉起
  是否有问题，要等一次真实登录才知道。
