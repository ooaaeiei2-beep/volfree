import AppKit
import ServiceManagement
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {

    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private let model = AppModel()
    private let settings = SettingsModel()
    private var refreshTimer: Timer?
    /// 空闲轮询：面板关着的时候也要让菜单栏图标跟着变，否则图标会一直停在
    /// 上一次的状态——插了盘、有程序占着，图标还是白的。
    private var idleTimer: Timer?
    /// 空闲轮询间隔。单次 `watch` 约 230ms，5 秒一次约合 4.6% 个核心；
    /// 只在**挂着候选卷**时才建计时器，没盘时零开销。
    private static let idleInterval: TimeInterval = 5
    private var settingsWindow: NSWindow?

    /// 开发用诊断输出：VOLFREE_DEBUG=1 时写到 stderr。
    private func log(_ text: String) {
        guard ProcessInfo.processInfo.environment["VOLFREE_DEBUG"] == "1" else { return }
        FileHandle.standardError.write(Data("[volfree] \(text)\n".utf8))
    }

    /// 把三态在浅色/深色两种菜单栏下的实际取色打出来。
    /// 菜单栏图标看不见这类问题，靠肉眼很难复核，靠这个数字对。
    private func logStatusColors() {
        for name in [NSAppearance.Name.aqua, .darkAqua] {
            guard let appearance = NSAppearance(named: name) else { continue }
            let hexes = [Tone.safe, .caution, .danger].map { hex($0.statusBarColor(for: appearance)) }
            log("菜单栏配色 \(name.rawValue)：常态 \(hexes[0])  黄 \(hexes[1])  红 \(hexes[2])")
        }
    }

    /// 菜单栏换肤（换壁纸、切深色）时，状态栏按钮的 `effectiveAppearance` 会变，
    /// 但**不会**有通知送到我们手上——`NSApp` 的外观可能压根没变
    /// （系统浅色 + 深色壁纸就是这种：NSApp=Aqua，按钮=VibrantDark）。
    ///
    /// `NSView.viewDidChangeEffectiveAppearance()` 会沿视图树往下传，所以在按钮上
    /// 挂一个 1×1 的观察者，用它把"外观变了"转成回调，重新生成图标。
    final class AppearanceProbe: NSView {
        var onChange: (() -> Void)?
        override func viewDidChangeEffectiveAppearance() {
            super.viewDidChangeEffectiveAppearance()
            onChange?()
        }
    }

    private var appearanceProbe: AppearanceProbe?
    /// 当前色调。换肤时要用它重画，所以得记住。
    private var currentTone: Tone = .safe
    /// 开发用强制色调（`VOLFREE_FORCE_TONE`）。设了之后不再跟随真实判定。
    private var forcedTone: Tone?
    /// 上一次真正画上去的「外观 + 色调」。启动阶段外观回调会连发十几次，用它挡掉重复绘制。
    private var lastDrawnKey: String?
    /// 外观变化的防抖任务。
    private var appearanceDebounce: DispatchWorkItem?

    /// 把某个色调画到菜单栏上。
    ///
    /// 常态用 template 图，颜色交给系统；只有黄/红才自己画非模板图。
    /// 所以**常态下换肤不需要重画**——template 图自己会适应，这也是标准路径的好处。
    private func applyStatusIcon(_ tone: Tone) {
        currentTone = tone
        guard let button = statusItem.button else { return }

        let appearance = button.effectiveAppearance
        // 常态不关心外观，把它排除在 key 之外，换肤时就不会白重画一遍
        let key = tone == .safe
            ? "safe|template"
            : "\(tone)|\(appearance.name.rawValue)"
        guard key != lastDrawnKey else { return }
        lastDrawnKey = key

        let image = StatusIcon.image(for: tone, appearance: appearance)
        button.image = image

        if tone == .safe {
            log("菜单栏图标已重绘 常态 template 图（颜色交给系统）")
        } else {
            log("菜单栏图标已重绘 外观=\(appearance.name.rawValue) 色调=\(tone) 颜色=\(hex(tone.statusBarColor(for: appearance)))")
        }

        // 菜单栏截不了图（没有录屏权限），所以把画出来的那张图落盘自己验像素。
        // 注意：常态是 template 图，落盘的是**蒙版**（黑+alpha），不是最终显示颜色——
        // 最终颜色由系统在绘制时填，文件里看不到。
        if let path = ProcessInfo.processInfo.environment["VOLFREE_DUMP_STATUS_ICON"], !path.isEmpty,
           let tiff = image.tiffRepresentation,
           let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: path))
        }
    }

    /// 外观变了，但**别立刻重画**。
    ///
    /// 启动那一下 `viewDidChangeEffectiveAppearance` 会连着触发十几次，
    /// 在 `VibrantLight` / `DarkAqua` / `VibrantDark` 之间反复横跳（这是 AppKit
    /// 自己建状态栏窗口的过程，不是我们引起的）。逐次重画不但浪费，更要命的是
    /// **万一最后一次停在浅色，图标就又被画成深色**。
    /// 所以等它安静下来再画，只认稳定后的那个外观。
    private func scheduleIconRedraw() {
        appearanceDebounce?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.applyStatusIcon(self.currentTone)
        }
        appearanceDebounce = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: work)
    }

    private func hex(_ color: NSColor) -> String {
        guard let c = color.usingColorSpace(.sRGB) else { return "?" }
        return String(format: "#%02X%02X%02X",
                      Int(c.redComponent * 255),
                      Int(c.greenComponent * 255),
                      Int(c.blueComponent * 255))
    }

    /// 登录项诊断：把 Bundle 身份、签名和 `register()` 的真实报错打出来。
    ///
    /// 界面上的开关只会显示"置灰"，看不出是位置不对、签名不对还是被系统拒了。
    /// 跑 `Volfree.app/Contents/MacOS/Volfree --login-item-probe` 看具体原因。
    private func probeLoginItem() {
        func out(_ s: String) {
            FileHandle.standardError.write(Data("\(s)\n".utf8))
        }
        func statusName(_ s: SMAppService.Status) -> String {
            switch s {
            case .notRegistered: return "notRegistered(0) 未注册"
            case .enabled: return "enabled(1) 已启用"
            case .requiresApproval: return "requiresApproval(2) 待用户批准"
            case .notFound: return "notFound(3) 系统找不到/不接受这个 app"
            @unknown default: return "unknown(\(s.rawValue))"
            }
        }

        let bundle = Bundle.main
        out("bundlePath      = \(bundle.bundlePath)")
        out("bundleIdentifier= \(bundle.bundleIdentifier ?? "(nil)")")
        out("executable      = \(bundle.executablePath ?? "(nil)")")
        out("在 /Applications = \(bundle.bundlePath.hasPrefix("/Applications/"))")
        out("当前状态        = \(statusName(SMAppService.mainApp.status))")

        if CommandLine.arguments.contains("--unregister") {
            out("--- 尝试 unregister() ---")
            do {
                try SMAppService.mainApp.unregister()
                out("unregister() 成功")
            } catch {
                let ns = error as NSError
                out("unregister() 失败 domain=\(ns.domain) code=\(ns.code) desc=\(ns.localizedDescription)")
            }
            out("unregister() 之后状态 = \(statusName(SMAppService.mainApp.status))")
            return
        }

        out("--- 尝试 register() ---")
        do {
            try SMAppService.mainApp.register()
            out("register() 成功")
        } catch {
            let ns = error as NSError
            out("register() 失败")
            out("  domain = \(ns.domain)")
            out("  code   = \(ns.code)")
            out("  desc   = \(ns.localizedDescription)")
            if let underlying = ns.userInfo[NSUnderlyingErrorKey] as? NSError {
                out("  underlying = \(underlying.domain) \(underlying.code): \(underlying.localizedDescription)")
            }
            out("  userInfo = \(ns.userInfo)")
        }
        out("register() 之后状态 = \(statusName(SMAppService.mainApp.status))")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        log("didFinishLaunching 进入")

        // 诊断入口：登录项注册为什么失败，光看 UI 是看不出来的，
        // 必须把 Bundle 身份、签名情况和 `register()` 的真实报错一起打出来。
        if CommandLine.arguments.contains("--login-item-probe") {
            probeLoginItem()
            exit(0)
        }

        NSApp.setActivationPolicy(.accessory)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            applyStatusIcon(currentTone)
            button.target = self
            button.action = #selector(statusItemClicked(_:))
            // 右键也要能收到，用来弹「退出」菜单
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])

            let probe = AppearanceProbe(frame: NSRect(x: 0, y: 0, width: 1, height: 1))
            probe.onChange = { [weak self] in
                self?.scheduleIconRedraw()
            }
            button.addSubview(probe)
            appearanceProbe = probe

            log("状态项已创建 外观=\(button.effectiveAppearance.name.rawValue)")
        } else {
            log("状态项 button 为 nil")
        }

        // 开发用：没有真实告警时也能核对黄/红两态的菜单栏图标。
        // 设了之后不再跟随真实判定，直到进程重启。
        if let raw = ProcessInfo.processInfo.environment["VOLFREE_FORCE_TONE"] {
            let forced: Tone? = raw == "caution" ? .caution
                : raw == "danger" ? .danger
                : raw == "safe" ? .safe : nil
            if let forced {
                forcedTone = forced
                applyStatusIcon(forced)
                log("强制色调=\(raw)")
            } else {
                log("VOLFREE_FORCE_TONE 取值无效：\(raw)（应为 safe / caution / danger）")
            }
        }

        // 开发用：启动后自动把面板弹出来。
        // 面板打开时才会每 2.5 秒轮询，所以这是唯一能量到"使用时真实占用"的办法
        // （命令行没法模拟点击菜单栏图标，本机也没有辅助功能权限）。
        if ProcessInfo.processInfo.environment["VOLFREE_OPEN_PANEL"] == "1" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                self?.showPopover()
            }
            log("将自动打开面板")
        }

        model.onToneChange = { [weak self] tone in
            guard let self, self.forcedTone == nil else { return }
            self.applyStatusIcon(tone)
            // 面板关着的时候继续盯着占用变化——否则图标会一直停在上一次的状态
            if !self.popover.isShown { self.startIdleTimer() }
        }
        model.onOpenSettings = { [weak self] in
            self?.showSettings()
        }
        model.onQuit = {
            NSApp.terminate(nil)
        }
        // LSUIElement 应用没有菜单栏，⌘Q 默认不生效；面板有焦点时手动接一下，
        // 否则用户按 ⌘Q 会觉得这个 app "关不掉"。
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard event.modifierFlags.contains(.command),
                  event.charactersIgnoringModifiers == "q" else { return event }
            NSApp.terminate(nil)
            return nil
        }
        // 诊断钩子只在调试时挂上，正常运行时它是 nil，不产生任何字符串拼接开销
        if ProcessInfo.processInfo.environment["VOLFREE_DEBUG"] == "1" {
            model.debugLog = { [weak self] text in self?.log(text) }
            logStatusColors()
        }

        let controller = NSHostingController(rootView: PopoverView().environmentObject(model))
        controller.sizingOptions = [.preferredContentSize]
        popover.behavior = .transient
        popover.delegate = self
        popover.contentViewController = controller
        popover.contentSize = NSSize(width: Metrics.panelWidth, height: 220)

        // 插拔盘时立刻重扫，不必等下一次轮询
        let workspace = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didMountNotification, NSWorkspace.didUnmountNotification] {
            workspace.addObserver(self, selector: #selector(volumeChanged(_:)), name: name, object: nil)
        }

        model.refresh()
        log("登录项状态=\(SMAppService.mainApp.status.rawValue)"
            + "（0 未注册 / 1 已启用 / 2 待批准 / 3 未找到）")

        // 开发用：VOLFREE_RENDER=<png> 时把面板离屏渲染成图片后退出。
        // 用于在没有录屏权限的环境里核对版式；渲染的就是真实视图，不是另一套画法。
        if let out = ProcessInfo.processInfo.environment["VOLFREE_RENDER"], !out.isEmpty {
            log("已排定离屏渲染 -> \(out)")
            // 没有真盘时也能核对「已推出」卡片
            if let name = ProcessInfo.processInfo.environment["VOLFREE_SIMULATE_EJECT"],
               !name.isEmpty {
                log("模拟已推出：\(name)")
                model.simulateEjected(name)
            }
            // 速率是两次采样的差商，只刷一次永远算不出来。
            // 所以这里补采一次，让渲染出的图带真实速率而不是空白。
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) { [weak self] in
                self?.model.refresh()
            }
            // 默认 3.2s 出图；要核对「卡片到点自动收起」就把这个值调大
            let delay = Double(ProcessInfo.processInfo.environment["VOLFREE_RENDER_DELAY"] ?? "") ?? 3.2
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                self?.renderSnapshot(to: out)
                NSApp.terminate(nil)
            }
            return
        }

        // 开发用：VOLFREE_AUTOOPEN=1 时启动后自动展开面板，便于截图核对外观。
        // 正常使用不会触发，不改变任何行为。
        if ProcessInfo.processInfo.environment["VOLFREE_AUTOOPEN"] == "1" {
            log("已排定自动展开")
            // 自动展开模式下让面板常驻，否则任何一次外部点击都会把它收起，无法截图
            popover.behavior = .applicationDefined
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
                self?.showPopover()
            }
        }
    }

    @objc private func volumeChanged(_ note: Notification) {
        model.refresh()
    }

    /// 把面板渲染成 PNG。仅开发用。
    private func renderSnapshot(to path: String) {
        let wantsSettings = ProcessInfo.processInfo.environment["VOLFREE_RENDER_SETTINGS"] == "1"
        if ProcessInfo.processInfo.environment["VOLFREE_EXPAND"] == "1" {
            model.expanded = true
        }
        // 把这次要呈现的文字照原样打出来，核对文案不用靠肉眼猜图
        if let e = model.ejected {
            log("渲染：正在显示「已推出」卡片 name=\(e.name)")
        }
        if let report = model.current {
            log("渲染：卷=\(report.volume.name) 等级=\(report.tone) 共 \(report.summary.total) 个进程")
            for blocker in report.blockers {
                log("  进程 \(blocker.command) pid=\(blocker.pid) 标签=「\(blocker.tag.text)」"
                    + " diskio=\(blocker.diskioWritten.map(String.init) ?? "nil")"
                    + " 速率=\(model.rateText(blocker.pid) ?? "（无）")")
            }
        } else {
            log("渲染：没有可展示的卷")
        }
        let root = AnyView(
            Group {
                if wantsSettings {
                    SettingsView()
                        .environmentObject(model)
                        .environmentObject(settings)
                } else {
                    PopoverView().environmentObject(model)
                }
            }
            .background(Color(nsColor: .windowBackgroundColor))
        )
        let host = NSHostingView(rootView: root)
        host.frame = NSRect(x: 0, y: 0, width: Metrics.panelWidth, height: 120)
        host.layoutSubtreeIfNeeded()

        let height = max(120, host.fittingSize.height)
        let width = max(Metrics.panelWidth, host.fittingSize.width)
        host.frame = NSRect(x: 0, y: 0, width: width, height: height)
        host.layoutSubtreeIfNeeded()

        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
            log("无法创建位图")
            return
        }
        host.cacheDisplay(in: host.bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]) else {
            log("无法编码 PNG")
            return
        }
        do {
            try data.write(to: URL(fileURLWithPath: path))
            log("已写出 \(path)  尺寸 \(Int(host.frame.width))x\(Int(height))")
        } catch {
            log("写出失败：\(error.localizedDescription)")
        }
    }

    /// 左键弹面板，右键弹菜单。菜单是「退出」唯一的常驻入口——
    /// 这个 app 是 LSUIElement，没有 Dock 图标也没有菜单栏，不给菜单就只能去活动监视器杀进程。
    @objc private func statusItemClicked(_ sender: Any?) {
        log("状态栏点击 event.type=\(NSApp.currentEvent?.type.rawValue ?? 0)")
        if NSApp.currentEvent?.type == .rightMouseUp {
            showStatusMenu()
        } else {
            togglePopover(sender)
        }
    }

    private func showStatusMenu() {
        let menu = NSMenu()
        for (title, action) in [("打开面板", #selector(menuOpenPanel)),
                                ("设置…", #selector(menuOpenSettings))] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出 volfree", action: #selector(menuQuit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        guard let button = statusItem.button else { return }
        menu.popUp(positioning: nil,
                   at: NSPoint(x: 0, y: button.bounds.height + 4),
                   in: button)
    }

    @objc private func menuOpenPanel() { showPopover() }
    @objc private func menuOpenSettings() { showSettings() }
    @objc private func menuQuit() { NSApp.terminate(nil) }

    @objc private func togglePopover(_ sender: Any?) {
        if popover.isShown {
            popover.performClose(sender)
        } else {
            showPopover()
        }
    }

    private func showPopover() {
        log("showPopover 进入")
        guard let button = statusItem.button else {
            log("button 为 nil，放弃")
            return
        }
        log("button.window=\(String(describing: button.window))")
        // 关着的这段时间里速率样本已经过期，先清掉再采
        model.resetRateSamples()
        model.refresh()
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        log("popover.isShown=\(popover.isShown)")
        // 面板打开后走 2.5 秒的完整轮询，空闲轮询让位，别两套计时器一起跑
        stopIdleTimer()
        startTimer()
    }

    /// 设置窗口。accessory 应用没有 Dock 图标，必须自己 activate 才能拿到焦点。
    private func showSettings() {
        if settingsWindow == nil {
            let controller = NSHostingController(
                rootView: SettingsView()
                    .environmentObject(model)
                    .environmentObject(settings))
            let window = NSWindow(contentViewController: controller)
            window.title = "volfree 设置"
            window.styleMask = [.titled, .closable]
            window.isReleasedWhenClosed = false
            window.center()
            settingsWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    private func startTimer() {
        stopTimer()
        // 面板打开期间才轮询：有进程新开了文件要能马上反映出来
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 2.5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.model.refresh() }
        }
    }

    private func stopTimer() {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }

    // MARK: - 空闲轮询（面板关闭时）

    /// 面板关着的时候也要盯着占用，否则菜单栏图标不会变色。
    ///
    /// 用的是 `watch` 而不是 `status`：卷的身份（外接/可推出/文件系统）在插拔之间不会变，
    /// 空闲时没必要每次重查 `diskutil`。单次 230ms 对 590ms，这是敢开空闲轮询的前提。
    private func startIdleTimer() {
        guard idleTimer == nil else { return }
        guard !model.reports.isEmpty else { return }
        idleTimer = Timer.scheduledTimer(withTimeInterval: Self.idleInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.pollIdleTone() }
        }
        log("空闲轮询已启动 间隔 \(Self.idleInterval)s，监控 \(model.reports.count) 块卷")
    }

    private func stopIdleTimer() {
        guard idleTimer != nil else { return }
        idleTimer?.invalidate()
        idleTimer = nil
        log("空闲轮询已停止")
    }

    /// 只更新菜单栏图标的色调，不动面板数据（面板打开时会走完整 `status`）。
    private func pollIdleTone() async {
        guard !popover.isShown else { return }
        let mountpoints = model.reports.map(\.volume.mountpoint)
        guard !mountpoints.isEmpty else {
            stopIdleTimer()
            return
        }
        do {
            let payload = try await Runner.watch(mountpoints: mountpoints)
            applyStatusIcon(payload.tone)
        } catch {
            // 空闲轮询失败不打扰用户，下一次 tick 会重试
            log("空闲轮询失败：\(error.localizedDescription)")
        }
    }

    func popoverDidClose(_ notification: Notification) {
        stopTimer()
        startIdleTimer()
    }
}

MainActor.assumeIsolated {
    let application = NSApplication.shared
    let delegate = AppDelegate()
    application.delegate = delegate
    // run() 阻塞到退出，delegate 在整个生命周期内被 application.delegate 持有
    application.run()
}
