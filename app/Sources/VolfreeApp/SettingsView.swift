import SwiftUI
import ServiceManagement

/// 登录时自动启动。SMAppService 是 macOS 13+ 的官方做法，
/// 它会自己维护登录项，不需要我们往 LaunchAgents 目录写 plist。
enum LaunchAtLogin {
    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    static var requiresApproval: Bool {
        SMAppService.mainApp.status == .requiresApproval
    }

    /// app 是否在 `/Applications` 下。
    ///
    /// `SMAppService.mainApp` 要求 app 装在「应用程序」文件夹里；
    /// 从构建目录运行时注册的是那个临时路径，重建一次就失效，所以这里自己判位置。
    static var isInApplications: Bool {
        Bundle.main.bundlePath.hasPrefix("/Applications/")
    }

    static func set(_ enabled: Bool) throws {
        if enabled {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
    }
}

@MainActor
final class SettingsModel: ObservableObject {
    @Published var launchAtLogin: Bool = LaunchAtLogin.isEnabled
    @Published var launchError: String?
    @Published var needsApproval: Bool = LaunchAtLogin.requiresApproval
    /// 能不能注册，只看**位置**，不看 `status`。
    ///
    /// 这里原来写的是 `status != .notFound`，是个理解错误：`SMAppService.mainApp`
    /// 在**尚未注册**时返回的正是 `.notFound`(3)，而不是 `.notRegistered`(0)。
    /// 拿 `.notFound` 当"位置不被接受"，等于把开关永久禁用——用户根本点不到
    /// `register()`，也就永远注册不上。（实测：`register()` 一次就成功，
    /// 状态随即变成 `.enabled`(1)。）
    @Published var canRegister: Bool = LaunchAtLogin.isInApplications

    /// 开关动作放在这里，视图只负责把 Binding 接上。
    /// （CLT 工具链里没有 SwiftUI 的宏插件，`@State` 用不了，
    /// 所以状态一律放 ObservableObject。）
    func setLaunchAtLogin(_ enabled: Bool) {
        do {
            try LaunchAtLogin.set(enabled)
            launchError = nil
            needsApproval = LaunchAtLogin.requiresApproval
        } catch {
            launchError = "设置失败：\(error.localizedDescription)"
        }
        // 以系统实际状态为准，不信刚才那次调用的返回值
        launchAtLogin = LaunchAtLogin.isEnabled
        canRegister = LaunchAtLogin.isInApplications
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var settings: SettingsModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            permissionSection
            Divider().padding(.vertical, 14)
            generalSection
            Divider().padding(.vertical, 14)
            aboutSection
        }
        .padding(20)
        .frame(width: 420)
    }

    // MARK: - 权限

    private var permissionSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("进程可见范围").font(.system(size: 12, weight: .semibold))

            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: model.isLimitedAccess
                      ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                    .foregroundStyle(model.isLimitedAccess ? Color.orange : Color.green)
                    .font(.system(size: 11))
                Text(model.access?.desc ?? "尚未探测")
                    .font(.system(size: 11.5))
                    .fixedSize(horizontal: false, vertical: true)
            }

            if model.isLimitedAccess {
                Text("没有完全磁盘访问时，lsof 依然正常退出，只是看不到系统进程。"
                     + "此时「没有占用」与「看不到占用」无法区分——面板会如实标注，不会替你下结论。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                Button("打开「完全磁盘访问」设置") {
                    openFullDiskAccess()
                }
                .controlSize(.small)
            }
        }
    }

    // MARK: - 通用

    private var generalSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("通用").font(.system(size: 12, weight: .semibold))

            Toggle("登录时自动启动", isOn: Binding(
                get: { settings.launchAtLogin },
                set: { settings.setLaunchAtLogin($0) }
            ))
            .toggleStyle(.checkbox)
            .disabled(!settings.canRegister)

            if !settings.canRegister {
                Text("系统不接受当前位置的 app 注册登录项。把 Volfree.app 放进「应用程序」文件夹后再开这项。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let error = settings.launchError {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            } else if settings.needsApproval {
                Text("已在系统中登记，但还需要在「系统设置 → 通用 → 登录项」里允许一次。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Text("面板打开时每 2.5 秒完整重扫一次；关闭时每 5 秒只查一次占用（不查磁盘信息），所以菜单栏图标照样会变色。没插盘时完全不轮询。")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - 关于

    private var aboutSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("关于").font(.system(size: 12, weight: .semibold))
            Text("volfree \(model.version.isEmpty ? "—" : model.version) — 判断由 Python 完成，界面只负责呈现。")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(Runner.scriptPath)
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(.tertiary)
                .lineLimit(2)
                .truncationMode(.middle)
                .textSelection(.enabled)

            // 这个 app 没有 Dock 图标、没有菜单栏，退出入口只能自己给。
            // 面板底栏和状态栏右键菜单各有一个，设置窗口里这个是最好找的那个。
            Button("退出 volfree") {
                NSApp.terminate(nil)
            }
            .controlSize(.small)
            .padding(.top, 4)
        }
    }

    private func openFullDiskAccess() {
        let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!
        NSWorkspace.shared.open(url)
    }
}
