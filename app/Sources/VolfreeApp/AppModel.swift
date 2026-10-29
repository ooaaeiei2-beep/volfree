import Foundation

@MainActor
final class AppModel: ObservableObject {

    struct Toast: Identifiable, Equatable {
        let id = UUID()
        let text: String
        let tone: Tone
    }

    struct EjectedState: Equatable {
        let name: String
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
    @Published var expanded = false
    @Published var confirmingPID: Int32?
    @Published var pendingEjectConfirm = false
    @Published private(set) var ejected: EjectedState?
    /// 每个进程的写入速率（字节/秒）。两次采样的差商，只在面板打开期间有意义。
    @Published private(set) var rates: [Int32: Double] = [:]

    /// 菜单栏图标据此着色。
    var onToneChange: ((Tone) -> Void)?
    /// 打开设置窗口（由 AppDelegate 注入，模型不直接碰 AppKit）。
    var onOpenSettings: (() -> Void)?
    /// 退出应用。同样是注入的，视图只负责把按钮接上。
    var onQuit: (() -> Void)?

    private var lastDiskio: [Int32: (bytes: Int64, at: Date)] = [:]
    /// 「已推出」卡片的自动收起计时器。
    private var ejectDismissTask: Task<Void, Never>?

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

    var displayTone: Tone {
        if ejected != nil { return .safe }
        return current?.tone ?? .safe
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
        // 推出了新盘、或原盘已拔走，都该退出"已推出"的静止画面
        if let e = ejected, reports.contains(where: { $0.volume.name == e.name }) {
            clearEjected()
        }
        if reports.isEmpty {
            expanded = false
            confirmingPID = nil
            pendingEjectConfirm = false
        }
        onToneChange?(displayTone)
    }

    /// 用两次采样的差商算写入速率。
    ///
    /// 为什么不看文件大小：mmap 写入不改变文件长度，看 size 永远是零增长。
    /// 内核的 `diskio_written` 是累计写盘字节，差商才是真实速率。
    private func updateRates(from volumes: [VolumeReport]) {
        let now = Date()
        var fresh: [Int32: Double] = [:]
        var next: [Int32: (bytes: Int64, at: Date)] = [:]

        for report in volumes {
            for blocker in report.blockers {
                guard let bytes = blocker.diskioWritten else { continue }
                if let prev = lastDiskio[blocker.pid], bytes >= prev.bytes {
                    let dt = now.timeIntervalSince(prev.at)
                    // 间隔太短时噪声大；太长（面板关了很久又打开）则是无意义的平均值
                    if dt >= 0.4 && dt <= 10 {
                        fresh[blocker.pid] = Double(bytes - prev.bytes) / dt
                    }
                }
                next[blocker.pid] = (bytes, now)
            }
        }
        lastDiskio = next
        rates = fresh
        if let debugLog {
            let pairs = volumes.flatMap { $0.blockers }.map { b -> String in
                let raw = b.diskioWritten.map { String($0) } ?? "nil"
                return "\(b.pid):\(raw)"
            }
            let got = fresh.map { "\($0.key):\(Int($0.value))" }.sorted()
            debugLog("采样 diskio=\(pairs.joined(separator: ",")) 速率=\(got.joined(separator: ","))")
        }
    }

    /// 面板重新打开时清掉旧样本，否则第一次算出来的是「关着的这几分钟的平均值」。
    func resetRateSamples() {
        lastDiskio.removeAll()
        rates.removeAll()
    }

    func rateText(_ pid: Int32) -> String? {
        guard let bps = rates[pid], bps >= 2048 else { return nil }
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

    func primaryAction() {
        guard let report = current, !busy else { return }
        switch report.tone {
        case .safe:
            pendingEjectConfirm = false
            ejectOnly(report)
        case .caution:
            // 有"放弃写入"时先软确认一次，与原型一致
            if report.summary.abandonedWrites > 0 && !pendingEjectConfirm {
                pendingEjectConfirm = true
                return
            }
            pendingEjectConfirm = false
            endThenEject(report)
        case .danger:
            break
        }
    }

    func cancelPendingEject() {
        pendingEjectConfirm = false
    }

    // MARK: - 「已推出」确认卡片

    /// 「已推出」是一次性确认，不是一种常驻状态。
    ///
    /// 盘拔走以后 `reports` 里就再没有它了，没有任何事件能"告诉"界面该收起来，
    /// 于是会一直停在上一块盘的结果上——换一块盘插进来也还显示旧的。
    /// 所以这里给它一个明确寿命：到点自己收，用户下次打开看到的是当下真实状态。
    private func showEjected(_ name: String) {
        let state = EjectedState(name: name)
        ejected = state
        ejectDismissTask?.cancel()
        ejectDismissTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            guard let self, !Task.isCancelled, self.ejected == state else { return }
            self.clearEjected()
        }
    }

    private func clearEjected() {
        ejectDismissTask?.cancel()
        ejectDismissTask = nil
        guard ejected != nil else { return }
        ejected = nil
        onToneChange?(displayTone)
    }

    /// 开发用：没有真盘也能把「已推出」卡片摆出来核对版式与自动收起。
    /// 只在 `VOLFREE_SIMULATE_EJECT` 设了值时由 `main.swift` 调用，正常路径不走这里。
    func simulateEjected(_ name: String) {
        showEjected(name)
    }

    func endProcess(_ blocker: BlockerInfo, allowRisky: Bool) {
        guard let report = current, !busy else { return }
        confirmingPID = nil
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
                    // 卡片本身已经写明「已推出 / 可以安全拔线」，不再叠一条同样的提示条
                    showEjected(report.volume.name)
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
                        showEjected(report.volume.name)
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
