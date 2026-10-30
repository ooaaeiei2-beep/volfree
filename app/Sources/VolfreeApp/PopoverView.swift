import SwiftUI

struct PopoverView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            divider

            // 主体自带底部呼吸位：主按钮、进程列表都收在这一块里，
            // 少了这一句按钮会直接贴在下面的横线上。
            //
            // 这里必须用 VStack 而不是 Group：reportBody 返回的是「判定卡 + 主按钮 + …」
            // 一组视图，Group 上的 modifier 会逐个作用到每个子视图上，
            // 结果判定卡与按钮之间被塞了两份间距。
            VStack(alignment: .leading, spacing: 0) {
                if let ejected = model.ejected {
                    ejectedBody(ejected)
                } else if let report = model.current {
                    reportBody(report)
                } else {
                    emptyBody
                }
            }
            .padding(.bottom, Metrics.sectionGap)

            if let toast = model.toast {
                toastView(toast)
            }

            divider
            footer
        }
        // 顺序很重要：先加内边距，再定总宽。
        // 反过来写（先 frame 再 padding）会让内边距落到宽度之外，被裁掉。
        .padding(.horizontal, Metrics.panelPadding)
        .padding(.top, 10)
        .padding(.bottom, 6)
        .frame(width: Metrics.panelWidth)
        .background(Color.clear)
    }

    // MARK: - 头部

    private var header: some View {
        HStack(spacing: 10) {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(LinearGradient(
                    colors: [Color(.sRGB, red: 0x78 / 255, green: 0x97 / 255, blue: 0xBD / 255, opacity: 1),
                             Color(.sRGB, red: 0x48 / 255, green: 0x68 / 255, blue: 0x8F / 255, opacity: 1)],
                    startPoint: .topLeading, endPoint: .bottomTrailing))
                .frame(width: 30, height: 30)
                .overlay(
                    Image(systemName: "externaldrive.fill")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(.white.opacity(0.95))
                )

            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(size: 13, weight: .semibold))
                    .lineLimit(1).truncationMode(.middle)
                Text(subtitle).font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            if model.reports.count > 1 {
                volumeSwitcher
            }
        }
        .padding(.horizontal, 4)
        .padding(.bottom, 11)
    }

    private var title: String {
        if let e = model.ejected { return e.name }
        return model.current?.volume.name ?? "volfree"
    }

    private var subtitle: String {
        if model.ejected != nil { return "已推出" }
        return model.current?.volume.metaLine ?? "没有检测到可推出的磁盘"
    }

    private var volumeSwitcher: some View {
        Menu {
            ForEach(model.reports) { report in
                Button {
                    model.selectedDevice = report.volume.device
                } label: {
                    if report.volume.device == model.current?.volume.device {
                        Label(report.volume.name, systemImage: "checkmark")
                    } else {
                        Text(report.volume.name)
                    }
                }
            }
        } label: {
            Image(systemName: "chevron.up.chevron.down")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.secondary)
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .frame(width: 18)
        .help("切换磁盘")
    }

    private var divider: some View {
        Rectangle()
            .fill(Metrics.hairline(scheme))
            .frame(height: 1)
            .padding(.horizontal, -Metrics.panelPadding)
    }

    // MARK: - 三态主体

    @ViewBuilder
    private func reportBody(_ report: VolumeReport) -> some View {
        verdict(report)
        primaryButton(report)

        if model.pendingEjectConfirm {
            softConfirm(report)
        }

        if report.summary.total > 0 {
            disclose(report)
            if model.expanded {
                processList(report)
            }
        }
    }

    private func verdict(_ report: VolumeReport) -> some View {
        let tone = report.tone.palette(for: scheme)
        return VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Circle()
                    .fill(tone.foreground)
                    .frame(width: 9, height: 9)
                    .overlay(
                        Circle().stroke(tone.foreground.opacity(0.16), lineWidth: 3)
                            .frame(width: 15, height: 15)
                    )
                    .frame(width: 9, height: 9)
                Text(verdictText(report))
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(tone.foreground)
            }
            Text(verdictReason(report))
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 17)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 10)
        .padding(.horizontal, 12)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(tone.background))
        .padding(.top, Metrics.sectionGap)
        .padding(.bottom, Metrics.sectionGap)
    }

    private func verdictText(_ report: VolumeReport) -> String {
        // 没扫完就不下结论，比任何颜色的结论都诚实
        if report.scanIncomplete { return "无法判断" }
        switch report.tone {
        case .safe:
            // 看不到全部进程时，只能说「没发现」，不能说「可以拔了」
            return model.isLimitedAccess ? "未发现占用" : "可以拔了"
        case .caution: return report.summary.abandonedWrites > 0 ? "可以推出" : "可以安全推出"
        case .danger: return "先别拔"
        }
    }

    private func verdictReason(_ report: VolumeReport) -> String {
        if report.scanIncomplete {
            return "扫描未完成（lsof 超时或输出被截断），结果不能作为能否拔出的依据"
        }
        let n = report.summary.total
        switch report.tone {
        case .safe:
            return model.accessCaveat ?? "没有程序占用这块磁盘"
        case .caution:
            if report.summary.abandonedWrites > 0 {
                return "\(n) 个程序占着，其中 \(report.summary.abandonedWrites) 个正在写已删除的临时文件"
            }
            return "\(n) 个程序占着，但都不在写入"
        case .danger:
            let risky = report.blockers.filter { $0.level == "RISKY" }
            if risky.count == 1 {
                let blocker = risky[0]
                if let rate = model.rateText(blocker.pid) {
                    return "\(blocker.command) 正在写入 \(rate)"
                }
                return "\(blocker.command) 正在写入，结束会留下半写的文件"
            }
            return "\(risky.count) 个程序正在写入"
        }
    }

    @ViewBuilder
    private func primaryButton(_ report: VolumeReport) -> some View {
        let tone = report.tone
        // 扫描不完整时不允许一键推出：可能还有没扫到的写入进程
        let disabled = tone == .danger || model.busy || report.scanIncomplete
        Button {
            model.primaryAction()
        } label: {
            Text(primaryLabel(report))
                .font(.system(size: 13, weight: .semibold))
                .frame(maxWidth: .infinity)
                .frame(height: Metrics.primaryHeight)
                .foregroundStyle(disabled ? Color.secondary : Color.white)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(disabled ? Metrics.subtleFill(scheme) : Metrics.accent(scheme))
                )
        }
        .buttonStyle(.plain)
        .disabled(disabled)
    }

    private func primaryLabel(_ report: VolumeReport) -> String {
        if model.busy { return "正在处理…" }
        if report.scanIncomplete { return "扫描不完整" }
        switch report.tone {
        case .safe: return "推出"
        case .caution:
            return report.summary.abandonedWrites > 0 ? "结束并推出" : "安全结束并推出"
        case .danger: return "等待完成"
        }
    }

    private func softConfirm(_ report: VolumeReport) -> some View {
        let tone = report.tone.palette(for: scheme)
        let n = report.summary.abandonedWrites
        let question = n > 1
            ? "会放弃 \(n) 次写入，且这些文件已无名字、无法从别处找回。"
            : "会放弃 1 次写入，且该文件已无名字、无法从别处找回。"
        return VStack(alignment: .leading, spacing: 8) {
            Text(question)
                .font(.system(size: 11.5))
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                Spacer(minLength: 0)
                smallButton("取消", filled: true) { model.cancelPendingEject() }
                smallButton("仍然结束", danger: true) { model.primaryAction() }
            }
        }
        .padding(.vertical, 9)
        .padding(.horizontal, 11)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Metrics.subtleFill(scheme))
        )
        .overlay(alignment: .leading) {
            Rectangle().fill(tone.foreground).frame(width: 3)
                .clipShape(RoundedRectangle(cornerRadius: 2))
        }
        .padding(.top, 6)
    }

    private func disclose(_ report: VolumeReport) -> some View {
        Button {
            model.expanded.toggle()
            if !model.expanded { model.confirmingPID = nil }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 8, weight: .semibold))
                    .rotationEffect(.degrees(model.expanded ? 90 : 0))
                Text(report.tone == .danger
                     ? "仍要结束…"
                     : "查看 \(report.summary.total) 个程序")
                    .font(.system(size: 11.5))
                Spacer(minLength: 0)
            }
            .foregroundStyle(.secondary)
            .frame(height: 26)
            .padding(.horizontal, 5)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.top, 7)
    }

    private func processList(_ report: VolumeReport) -> some View {
        ScrollView {
            VStack(spacing: 0) {
                ForEach(report.blockers) { blocker in
                    if model.confirmingPID == blocker.pid {
                        confirmRow(blocker)
                    } else {
                        processRow(blocker)
                    }
                }
            }
            .padding(.vertical, 2)
        }
        .frame(maxHeight: 208)
        .padding(.top, 2)
    }

    private func processRow(_ blocker: BlockerInfo) -> some View {
        let tag = tagOf(blocker)
        let tone = report_tone()
        return HStack(spacing: 8) {
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .fill(iconColor(blocker))
                .frame(width: 18, height: 18)
                .overlay(
                    Image(systemName: blocker.glyph)
                        .font(.system(size: 9.5, weight: .medium))
                        .foregroundStyle(.white)
                )

            Text(blocker.command)
                .font(.system(size: 12))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)

            Text(tag.text)
                .font(.system(size: tag.emphasized ? 10.5 : 11))
                .foregroundStyle(tag.warning ? tone.foreground : Color.secondary)
                .padding(.horizontal, tag.emphasized ? 6 : 0)
                .padding(.vertical, tag.emphasized ? 1 : 0)
                .background(
                    RoundedRectangle(cornerRadius: 4, style: .continuous)
                        .fill(tag.emphasized
                              ? (tag.warning ? tone.foreground.opacity(0.13) : Metrics.subtleFill(scheme))
                              : Color.clear)
                )

            if blocker.level == "RISKY" || blocker.flags.allWritesUnlinked {
                Button("结束…") { model.confirmingPID = blocker.pid }
                    .buttonStyle(.plain)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
            }
        }
        .padding(.horizontal, 6)
        .frame(height: Metrics.rowHeight)
    }

    private func report_tone() -> Tone.Palette {
        (model.current?.tone ?? .caution).palette(for: scheme)
    }

    /// 标签文案。正在写的进程带上速率——用户据此判断还要等多久，
    /// 而不是被笼统告知「有风险」。
    private func tagOf(_ blocker: BlockerInfo) -> (text: String, emphasized: Bool, warning: Bool) {
        let base = blocker.tag
        guard blocker.flags.hasWrite, let rate = model.rateText(blocker.pid) else {
            return base
        }
        return (base.text + " " + rate, base.emphasized, base.warning)
    }

    private func iconColor(_ blocker: BlockerInfo) -> Color {
        if blocker.level == "RISKY" {
            return Color(.sRGB, red: 0.69, green: 0.16, blue: 0.13, opacity: 1)
        }
        if blocker.flags.allWritesUnlinked {
            return Color(.sRGB, red: 0.69, green: 0.54, blue: 0.41, opacity: 1)
        }
        if blocker.exe.contains(".appex/") || blocker.exe.localizedCaseInsensitiveContains("quicklook") {
            return Color(.sRGB, red: 0.56, green: 0.56, blue: 0.58, opacity: 1)
        }
        return Color(.sRGB, red: 0.29, green: 0.56, blue: 0.85, opacity: 1)
    }

    private func confirmRow(_ blocker: BlockerInfo) -> some View {
        let abandoned = blocker.flags.allWritesUnlinked
        let question = abandoned
            ? "结束 \(blocker.command)？会放弃这次写入，该文件已无名字、无法找回。"
            : "结束 \(blocker.command)？未保存的内容会丢失。"
        return VStack(alignment: .leading, spacing: 8) {
            Text(question)
                .font(.system(size: 11.5))
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                Spacer(minLength: 0)
                smallButton("取消", filled: true) { model.confirmingPID = nil }
                smallButton("结束", danger: true) {
                    model.endProcess(blocker, allowRisky: blocker.level == "RISKY")
                }
            }
        }
        .padding(.vertical, 9)
        .padding(.horizontal, 9)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(blocker.level == "RISKY"
                      ? Color(.sRGB, red: 1.0, green: 0.23, blue: 0.19, opacity: 0.10)
                      : Metrics.subtleFill(scheme))
        )
        .padding(.vertical, 2)
    }

    private func smallButton(_ title: String, filled: Bool = false, danger: Bool = false,
                             action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 11))
                .padding(.horizontal, 10)
                .frame(height: 22)
                .foregroundStyle(danger ? Color.white : Color.primary)
                .background(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(danger
                              ? Color(.sRGB, red: 0.83, green: 0.20, blue: 0.16, opacity: 1)
                              : Metrics.subtleFill(scheme))
                )
        }
        .buttonStyle(.plain)
    }

    // MARK: - 已推出 / 空态

    private func ejectedBody(_ ejected: AppModel.EjectedState) -> some View {
        let tone = Tone.safe.palette(for: scheme)
        return VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Circle().fill(tone.foreground).frame(width: 9, height: 9)
                Text("已推出").font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(tone.foreground)
            }
            Text("可以安全拔线").font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .padding(.leading, 17)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 10)
        .padding(.horizontal, 12)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(tone.background))
        .padding(.top, Metrics.sectionGap)
    }

    private var emptyBody: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Circle().fill(Color.secondary.opacity(0.5)).frame(width: 9, height: 9)
                Text("没有可推出的磁盘").font(.system(size: 14, weight: .semibold))
            }
            Text("插上移动硬盘后会自动出现")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .padding(.leading, 17)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 10)
        .padding(.horizontal, 12)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .fill(Metrics.subtleFill(scheme)))
        .padding(.top, Metrics.sectionGap)
    }

    // MARK: - 提示条 / 底栏

    private func toastView(_ toast: AppModel.Toast) -> some View {
        let tone = toast.tone.palette(for: scheme)
        return Text(toast.text)
            .font(.system(size: 11.5))
            .foregroundStyle(tone.foreground)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 6)
            .padding(.horizontal, 9)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(tone.background))
            .padding(.top, 6)
            .padding(.bottom, Metrics.sectionGap)
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Button {
                model.onOpenSettings?()
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "gearshape")
                        .font(.system(size: 10.5))
                    Text("设置")
                        .font(.system(size: 11))
                }
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if model.isLimitedAccess {
                Button {
                    openFullDiskAccess()
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.system(size: 9))
                        Text("权限受限")
                            .font(.system(size: 11))
                    }
                    .foregroundStyle(.orange)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("只能看到本用户的进程。点此打开「完全磁盘访问」设置。")
            }

            Spacer(minLength: 8)

            Button {
                model.refresh()
            } label: {
                HStack(spacing: 5) {
                    if model.loading {
                        ProgressView().controlSize(.mini).scaleEffect(0.6).frame(width: 11, height: 11)
                    }
                    Text(model.loading ? "检查中" : "重新检查")
                        .font(.system(size: 11))
                }
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(model.loading || model.busy)

            // 这是「退出 volfree」，不是「推出磁盘」。用电源图标把两者分开，
            // 中文里「退出/推出」只差一个字，光靠文字容易看错。
            Button {
                model.onQuit?()
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "power")
                        .font(.system(size: 10))
                    Text("退出")
                        .font(.system(size: 11))
                }
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("退出 volfree（右键菜单里也有）")
        }
        .padding(.top, 8)
        .frame(height: 26)
    }

    private func openFullDiskAccess() {
        let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!
        NSWorkspace.shared.open(url)
    }
}
