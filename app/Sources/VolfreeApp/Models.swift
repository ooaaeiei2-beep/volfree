import Foundation

// volfree 的 JSON 契约。字段名与 volfree.py 的 volume_payload() 一一对应。
// 界面只读这些机器字段，不去解析 reason 里的中文文案。

struct AccessInfo: Codable, Equatable {
    let level: String
    let desc: String

    enum CodingKeys: String, CodingKey {
        case level
        case desc = "description"
    }

    /// 只有 root 或完全磁盘访问才能看到系统进程的占用。
    var isComplete: Bool { level == "root" || level == "full" }
}

struct VolumeInfo: Codable, Equatable, Identifiable {
    let device: String
    let mountpoint: String
    let name: String
    let fstype: String
    let external: Bool
    let virtual: Bool
    let ejectable: Bool
    let removable: Bool
    let totalBytes: Int64
    let freeBytes: Int64

    enum CodingKeys: String, CodingKey {
        case device, mountpoint, name, fstype, external, virtual, ejectable, removable
        case totalBytes = "total_bytes"
        case freeBytes = "free_bytes"
    }

    var id: String { device }

    var isDiskImage: Bool { virtual }

    /// 「可用 1.0 TB / 共 1.8 TB · exFAT · 外接」
    var metaLine: String {
        var parts: [String] = []
        if totalBytes > 0 {
            parts.append("可用 \(Self.size(freeBytes)) / 共 \(Self.size(totalBytes))")
        }
        if !fstype.isEmpty { parts.append(fstype.uppercased()) }
        parts.append(virtual ? "磁盘映像" : (external ? "外接" : "内置"))
        return parts.joined(separator: " · ")
    }

    private static func size(_ bytes: Int64) -> String {
        let f = ByteCountFormatter()
        f.countStyle = .file
        f.allowedUnits = [.useMB, .useGB, .useTB]
        return f.string(fromByteCount: bytes)
    }
}

struct HandleInfo: Codable, Equatable, Identifiable {
    let fd: String
    let access: String
    let type: String
    let name: String
    let exists: Bool?
    let writeState: String

    enum CodingKeys: String, CodingKey {
        case fd, access, type, name, exists
        case writeState = "write_state"
    }

    var id: String { "\(fd)|\(name)" }

    var isDeleted: Bool { exists == false }
}

struct BlockerFlags: Codable, Equatable {
    let hasWrite: Bool
    let allWritesUnlinked: Bool
    let hasUnlinkedWrite: Bool
    let hasUncertainWrite: Bool
    let allDeleted: Bool
    let onlyRead: Bool

    enum CodingKeys: String, CodingKey {
        case hasWrite = "has_write"
        case allWritesUnlinked = "all_writes_unlinked"
        case hasUnlinkedWrite = "has_unlinked_write"
        case hasUncertainWrite = "has_uncertain_write"
        case allDeleted = "all_deleted"
        case onlyRead = "only_read"
    }
}

struct BlockerInfo: Codable, Equatable, Identifiable {
    let pid: Int32
    let command: String
    let exe: String
    let uid: Int32
    let startTime: Int
    let level: String
    let reason: String
    /// 累计写盘字节数（内核计数）。两次采样做差商得到速率。
    /// nil = 取不到，或该进程不在写——界面不得据此显示 0 B/s。
    let diskioWritten: Int64?
    let flags: BlockerFlags
    let handles: [HandleInfo]

    enum CodingKeys: String, CodingKey {
        case pid, command, exe, uid, level, reason, flags, handles
        case startTime = "start_time"
        case diskioWritten = "diskio_written"
    }

    var id: Int32 { pid }

    /// 危险等级：RISKY / NEVER 不允许在主层级结束。
    var isForbidden: Bool { level == "RISKY" || level == "NEVER" }

    /// 进程清单右侧的标签，与 design/popover.html 的四种取值一致。
    var tag: (text: String, emphasized: Bool, warning: Bool) {
        if level == "RISKY" {
            return ("写入中", true, true)
        }
        if flags.allWritesUnlinked {
            return ("已删除·写入中", true, true)
        }
        if flags.hasWrite {
            return ("写入中", true, true)
        }
        if flags.allDeleted {
            return ("已删除·只读", true, false)
        }
        return ("只读", false, false)
    }

    /// 清单图标：按可执行路径粗分，纯装饰，不参与判定。
    var glyph: String {
        let p = exe.lowercased()
        if p.contains(".appex/") || p.contains("quicklook") || p.contains("thumb") { return "eye" }
        if p.contains("finder") { return "folder" }
        if p.contains("/applications/") && (p.contains("final cut") || p.contains("premiere")) { return "film" }
        if p.contains("python") || p.contains("node") || p.contains("ruby") { return "chevron.left.forwardslash.chevron.right" }
        if p.contains("/bin/") || p.contains("/usr/libexec/") { return "gearshape" }
        return "doc"
    }
}

struct VolumeSummary: Codable, Equatable {
    let total: Int
    let neverCount: Int
    let risky: Int
    let caution: Int
    let safe: Int
    let writing: Int
    let abandonedWrites: Int

    enum CodingKeys: String, CodingKey {
        case total, risky, caution, safe, writing
        case neverCount = "never"
        case abandonedWrites = "abandoned_writes"
    }
}

struct EnvClue: Codable, Equatable, Identifiable {
    let label: String
    let detail: String
    let warn: Bool
    var id: String { "\(label)|\(detail)" }
}

struct VolumeReport: Codable, Equatable, Identifiable {
    let version: String
    let volume: VolumeInfo
    let access: AccessInfo
    let summary: VolumeSummary
    let blockers: [BlockerInfo]
    let clues: [EnvClue]
    let warnings: [String]

    var id: String { volume.device }

    /// 三态：红 = 有人在写，黄 = 有占用但没人写，绿 = 没人占用。
    var tone: Tone {
        if summary.risky > 0 { return .danger }
        return summary.total > 0 ? .caution : .safe
    }

    /// 可以安全结束的进程（主按钮一次处理掉的那批）。
    var actionable: [BlockerInfo] {
        blockers.filter { $0.level == "SAFE" || $0.level == "CAUTION" }
    }
}

struct StatusPayload: Codable, Equatable {
    let version: String
    let access: AccessInfo
    let volumes: [VolumeReport]
}

/// `watch --json` 的返回：只回答「这些挂载点现在有没有人占着」。
///
/// 面板关着的时候没有 `status` 的全量数据，但菜单栏图标还得能变颜色，
/// 所以单开一条轻量通道。它不含容量、外接标记这些字段——那些只在面板里用。
struct WatchPayload: Decodable, Equatable {
    let version: String
    let volumes: [WatchVolume]

    struct WatchVolume: Decodable, Equatable {
        let mountpoint: String
        /// 机器字段，取值 `safe` / `caution` / `danger`，与 `VolumeReport.tone` 同一套规则。
        let tone: String
        let total: Int
        let risky: Int
        let writing: Int
    }

    /// 多块盘时取最危险的那个，与面板里「先看哪块」的规则一致。
    var tone: Tone {
        if volumes.contains(where: { $0.tone == "danger" }) { return .danger }
        if volumes.contains(where: { $0.tone == "caution" }) { return .caution }
        return .safe
    }
}

struct ActionResult: Codable, Equatable {
    let ok: Bool
    let action: String
    let message: String
    let pid: Int32?
    let alive: Bool?
}
