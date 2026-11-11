import Foundation

/// 一块磁盘的界面状态。按 device 分桶——切卷时不会把上一块盘的
/// 展开、逐项确认、软确认带过去（旧实现是全局单值，会串台）。
struct VolumeUIState: Equatable {
    var expanded = false
    var confirmingPID: Int32?
    var pendingEjectConfirm = false
    var ejected: EjectedState?
}

/// 「已推出」是一次性确认，按设备存，不再是一个全局单例。
/// 用 device 而不是卷名：两块盘同名时不会互相收掉对方的通知。
struct EjectedState: Equatable, Identifiable {
    let device: String
    let name: String
    let mountpoint: String

    var id: String { device }
    var deviceLabel: String {
        device.replacingOccurrences(of: "/dev/", with: "")
    }
}

@MainActor
final class AppModel: ObservableObject {

    struct Toast: Identifiable, Equatable {
        let id = UUID()
        let text: String
        let tone: Tone
    }

    // MARK: - 状态

    @Published private(set) var reports: [VolumeReport] = []
    @Published private(set) var access: AccessInfo?
    /// `status --json` 的顶层版本号。没有插盘时也拿得到，
    /// 所以设置窗口的「关于」不能去读 `current?.version`——那会随插拔变空。
    @Published private(set) var version: String = ""
    @Published var selectedDevice: String?
    @Published private(set) var loading = false
    @Published private(set) var busy = false
    @Published var lastError: String?
    @Published var toast: Toast?
    /// 每卷一份界面状态，键是 device。卷拔走后只清展开/确认，
    /// 「已推出」通知保留到 6 秒计时结束。
    @Published private(set) var volumeStates: [String: VolumeUIState] = [:]
    /// 每个进程的写入速率（字节/秒）。键带 device，避免同一进程占多块盘时互相覆盖。
    @Published private(set) var rates: [BlockerKey: Double] = [:]

    /// 菜单栏图标据此着色。
    var onToneChange: ((Tone) -> Void)?
    /// 打开设置窗口（由 AppDelegate 注入，模型不直接碰 AppKit）。
    var onOpenSettings: (() -> Void)?
    /// 退出应用。同样是注入的，视图只负责把按钮接上。
    var onQuit: (() -> Void)?

    private var lastDiskio: [BlockerKey: (bytes: Int64, at: Date)] = [:]
    /// 每块盘的「已推出」自动收起计时器。
    private var ejectDismissTasks: [String: Task<Void, Never>] = [:]

    // MARK: - 派生

    /// 当前展示的卷。没手动选过就挑最严重的那块。
    var current: VolumeReport? {
        if let device = selectedDevice,
           let picked = reports.first(where: { $0.volume.device == device }) {
            return picked
        }
        var best: VolumeReport?
        for report in reports {
            if let b = best, report.tone.severity <= b.tone.severity { continue }
            best = report
        }
        return best
    }

    /// 菜单栏图标是全局告警指示器：取所有卷里最危险的等级，
    /// 与用户当前选中哪块盘无关（面板关闭时 `watch` 的色调也是这个语义）。
    ///
    /// 这里刻意不再因为「刚推出一块盘」强制返回 .safe——
    /// 别的盘可能正在写入，图标变绿是安全误导。
    var displayTone: Tone {
        var worst: Tone = .safe
        for report in reports where report.tone.severity > worst.severity {
            worst = report.tone
        }
        return worst
    }

    /// 权限受限时，结论只覆盖本用户可见的部分，界面必须说明。
    var isLimitedAccess: Bool { !(access?.isComplete ?? false) }

    /// 给「没发现占用」配一句限定语。空结论是最容易被当真的结论，
    /// 看不到全部进程时不能只写「可以拔了」。
    var accessCaveat: String? {
        guard let level = access?.level else { return nil }
        switch level {
        case "root", "full":
            return nil
        case "limited":
            return "但当前权限看不到系统进程，仍可能有其他程序占着"
        default:
            return "但无法确认权限范围，占用情况可能不完整"
        }
    }

    /// 待显示的「已推出」通知。多块盘连续推出时各自一条，互不覆盖。
    var ejectedList: [EjectedState] {
        volumeStates.values.compactMap(\.ejected).sorted { $0.device < $1.device }
    }

    // MARK: - 每卷界面状态

    func state(for device: String) -> VolumeUIState {
        volumeStates[device] ?? VolumeUIState()
    }

    /// 整体赋值而不是字典下标修改，确保 @Published 一定发出变更通知。
    private func writeState(_ s: VolumeUIState, for device: String) {
        var next = volumeStates
        next[device] = s
        volumeStates = next
    }

    func select(device: String) {
        selectedDevice = device
    }

    func toggleExpanded(for device: String) {
        var s = state(for: device)
        s.expanded.toggle()
        if !s.expanded { s.confirmingPID = nil }
        writeState(s, for: device)
    }

    func setConfirmingPID(_ pid: Int32?, for device: String) {
        var s = state(for: device)
        s.confirmingPID = pid
        writeState(s, for: device)
    }

    func setPendingEjectConfirm(_ value: Bool, for device: String) {
        var s = state(for: device)
        s.pendingEjectConfirm = value
        writeState(s, for: device)
    }

    // MARK: - 读取

    func refresh() {
        guard !loading, !busy else { return }
        loading = true
        Task {
            await loadOnce()
            loading = false
        }
    }

    /// 开发用诊断钩子，正常运行为 nil。
    var debugLog: ((String) -> Void)?

    private func loadOnce() async {
        do {
            apply(try await Runner.status())
        } catch {
            lastError = error.localizedDescription
        }
    }

    private func apply(_ payload: StatusPayload) {
        access = payload.access
        version = payload.version
        reports = payload.volumes
        lastError = nil
        updateRates(from: payload.volumes)

        if let device = selectedDevice, !reports.contains(where: { $0.volume.device == device }) {
            selectedDevice = nil
        }

        // 卷的身份变了就清对应的界面状态。
        // 同一 device 重新挂回来时收掉「已推出」通知；别的卷同名不再影响它
        //（旧实现按卷名匹配，同名卷会误收）。
        let live = Set(reports.map(\.volume.device))
        var next = volumeStates
        for device in Array(volumeStates.keys) {
            guard var s = next[device] else { continue }
            if live.contains(device) {
                if s.ejected != nil {
                    s.ejected = nil
                    next[device] = s
                }
            } else {
                s.expanded = false
                s.confirmingPID = nil
                s.pendingEjectConfirm = false
                if s.ejected == nil {
                    next.removeValue(forKey: device)
                } else {
                    next[device] = s
                }
            }
        }
        if next != volumeStates { volumeStates = next }

        onToneChange?(displayTone)
    }

    /// 用两次采样的差商算写入速率。
    ///
    /// 为什么不看文件大小：mmap 写入不改变文件长度，看 size 永远是零增长。
    /// 内核的 `diskio_written` 是累计写盘字节，差商才是真实速率。
    private func updateRates(from volumes: [VolumeReport]) {
        let now = Date()
        var fresh: [BlockerKey: Double] = [:]
        var next: [BlockerKey: (bytes: Int64, at: Date)] = [:]

        for report in volumes {
            for blocker in report.blockers {
                guard let bytes = blocker.diskioWritten else { continue }
                let key = BlockerKey(device: report.volume.device, pid: blocker.pid)
                if let prev = lastDiskio[key], bytes >= prev.bytes {
                    let dt = now.timeIntervalSince(prev.at)
                    // 间隔太短时噪声大；太长（面板关了很久又打开）则是无意义的平均值
                    if dt >= 0.4 && dt <= 10 {
                        fresh[key] = Double(bytes - prev.bytes) / dt
                    }
                }
                next[key] = (bytes, now)
            }
        }
        lastDiskio = next
        rates = fresh
        if let debugLog {
            let pairs = volumes.flatMap { report in
                report.blockers.map { b -> String in
                    let raw = b.diskioWritten.map { String($0) } ?? "nil"
                    return "\(report.volume.device)/\(b.pid):\(raw)"
                }
            }
            let got = fresh.map { "\($0.key.device)/\($0.key.pid):\(Int($0.value))" }.sorted()
            debugLog("采样 diskio=\(pairs.joined(separator: ",")) 速率=\(got.joined(separator: ","))")
        }
    }

    /// 面板重新打开时清掉旧样本，否则第一次算出来的是「关着的这几分钟的平均值」。
    func resetRateSamples() {
        lastDiskio.removeAll()
        rates.removeAll()
    }

    func rateText(_ blocker: BlockerInfo, in report: VolumeReport) -> String? {
        let key = BlockerKey(device: report.volume.device, pid: blocker.pid)
        guard let bps = rates[key], bps >= 2048 else { return nil }
        return Self.rateFormatter.string(fromByteCount: Int64(bps)) + "/s"
    }

    private static let rateFormatter: ByteCountFormatter = {
        let f = ByteCountFormatter()
        f.countStyle = .file
        f.allowedUnits = [.useKB, .useMB, .useGB]
        f.includesUnit = true
        return f
    }()

    // MARK: - 动作

    /// `report` 由视图显式传入：异步执行期间用户切了卷，也不能把动作
    /// 落到新的 `current` 上。按钮写着哪块盘，就对哪块盘动手。
    func primaryAction(_ report: VolumeReport) {
        guard !busy else { return }
        let device = report.volume.device
        switch report.tone {
        case .safe:
            setPendingEjectConfirm(false, for: device)
            ejectOnly(report)
        case .caution:
            // 有"放弃写入"时先软确认一次，与原型一致
            if report.summary.abandonedWrites > 0 && !state(for: device).pendingEjectConfirm {
                setPendingEjectConfirm(true, for: device)
                return
            }
            setPendingEjectConfirm(false, for: device)
            endThenEject(report)
        case .danger:
            break
        }
    }

    func cancelPendingEject(for device: String) {
        setPendingEjectConfirm(false, for: device)
    }

    // MARK: - 「已推出」通知

    /// 「已推出」是一次性确认，不是常驻状态。盘拔走后 `reports` 里没有它了，
    /// 没有任何事件能"告诉"界面该收起来，所以给它一个明确寿命：到点自己收。
    /// 按 device 存，推出一块盘不再把整个面板占满，其余盘始终可见。
    private func showEjected(device: String, name: String, mountpoint: String) {
        let e = EjectedState(device: device, name: name, mountpoint: mountpoint)
        var s = state(for: device)
        s.ejected = e
        writeState(s, for: device)

        ejectDismissTasks[device]?.cancel()
        ejectDismissTasks[device] = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            guard let self, !Task.isCancelled else { return }
            self.clearEjected(for: device)
        }
    }

    private func clearEjected(for device: String) {
        ejectDismissTasks[device]?.cancel()
        ejectDismissTasks[device] = nil
        guard var s = volumeStates[device], s.ejected != nil else { return }
        s.ejected = nil
        var next = volumeStates
        if s == VolumeUIState() {
            next.removeValue(forKey: device)
        } else {
            next[device] = s
        }
        volumeStates = next
    }

    /// 开发用：没有真盘也能摆出「已推出」通知条核对版式与自动收起。
    /// 只在 `VOLFREE_SIMULATE_EJECT` 设了值时由 `main.swift` 调用，正常路径不走这里。
    func simulateEjected(_ name: String) {
        showEjected(device: "simulated/\(name)", name: name, mountpoint: "/Volumes/\(name)")
    }

    /// 开发用渲染钩子：展开当前卷的进程列表。
    func expandCurrentForRender() {
        guard let device = current?.volume.device else { return }
        var s = state(for: device)
        s.expanded = true
        writeState(s, for: device)
    }

    /// 开发用渲染钩子：给指定卷摆上软确认，用来核对「切卷后确认卡不会串台」。
    func simulateConfirm(for device: String) {
        setPendingEjectConfirm(true, for: device)
    }

    func endProcess(_ blocker: BlockerInfo, in report: VolumeReport, allowRisky: Bool) {
        guard !busy else { return }
        setConfirmingPID(nil, for: report.volume.device)
        busy = true
        Task {
            do {
                let result = try await Runner.end(pid: blocker.pid,
                                                  volume: report.volume.device,
                                                  allowRisky: allowRisky)
                toast = Toast(text: result.message, tone: result.ok ? .safe : .caution)
            } catch {
                toast = Toast(text: error.localizedDescription, tone: .caution)
            }
            busy = false
            await loadOnce()
            scheduleToastDismiss()
        }
    }

    private func ejectOnly(_ report: VolumeReport) {
        busy = true
        Task {
            do {
                let result = try await Runner.eject(volume: report.volume.device)
                if result.ok {
                    // 通知条本身已经写明「已推出 / 可以安全拔线」，不再叠一条同样的提示条
                    showEjected(device: report.volume.device,
                                name: report.volume.name,
                                mountpoint: report.volume.mountpoint)
                    toast = nil
                } else {
                    toast = Toast(text: result.message, tone: .danger)
                }
            } catch {
                toast = Toast(text: error.localizedDescription, tone: .danger)
            }
            busy = false
            await loadOnce()
            scheduleToastDismiss()
        }
    }

    /// 主按钮的批量路径：逐个结束可安全结束的进程，再推出。
    /// 刻意逐条调用 end 而不是 release --yes，这样每个进程的失败原因都能单独回显。
    private func endThenEject(_ report: VolumeReport) {
        busy = true
        Task {
            var failures: [String] = []
            for blocker in report.actionable {
                do {
                    let result = try await Runner.end(pid: blocker.pid,
                                                      volume: report.volume.device,
                                                      allowRisky: false)
                    if !result.ok {
                        failures.append("\(blocker.command)：\(result.message)")
                    }
                } catch {
                    failures.append("\(blocker.command)：\(error.localizedDescription)")
                }
            }

            if failures.isEmpty {
                do {
                    let result = try await Runner.eject(volume: report.volume.device)
                    if result.ok {
                        showEjected(device: report.volume.device,
                                    name: report.volume.name,
                                    mountpoint: report.volume.mountpoint)
                        toast = nil
                    } else {
                        toast = Toast(text: result.message, tone: .danger)
                    }
                } catch {
                    toast = Toast(text: error.localizedDescription, tone: .danger)
                }
            } else {
                toast = Toast(text: failures.joined(separator: "；"), tone: .caution)
            }

            busy = false
            await loadOnce()
            scheduleToastDismiss()
        }
    }

    private func scheduleToastDismiss() {
        guard let token = toast?.id else { return }
        Task {
            try? await Task.sleep(nanoseconds: 4_500_000_000)
            if toast?.id == token { toast = nil }
        }
    }
}
