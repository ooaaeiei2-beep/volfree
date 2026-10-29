import Foundation

/// 所有判断都留在 volfree.py 里，界面只负责调用与呈现。
/// 这里不做任何"看起来像判定"的推断，避免两处逻辑漂移。
enum Runner {

    enum Failure: LocalizedError {
        case launch(String)
        case nonZero(code: Int32, stderr: String)
        case decode(String)

        var errorDescription: String? {
            switch self {
            case .launch(let m): return "无法启动 volfree：\(m)"
            case .nonZero(let c, let e): return e.isEmpty ? "volfree 退出码 \(c)" : e
            case .decode(let m): return "返回内容无法解析：\(m)"
            }
        }
    }

    /// 开发期可用环境变量指到工作区，安装后走 bundle 内的副本。
    static var scriptPath: String {
        if let p = ProcessInfo.processInfo.environment["VOLFREE_SCRIPT"], !p.isEmpty {
            return p
        }
        if let res = Bundle.main.resourceURL?.appendingPathComponent("volfree.py").path,
           FileManager.default.fileExists(atPath: res) {
            return res
        }
        return "volfree.py"
    }

    static var pythonPath: String {
        if let p = ProcessInfo.processInfo.environment["VOLFREE_PYTHON"], !p.isEmpty {
            return p
        }
        return "/usr/bin/python3"
    }

    static func status() async throws -> StatusPayload {
        let data = try await run(["status", "--json"])
        do {
            return try JSONDecoder().decode(StatusPayload.self, from: data)
        } catch {
            throw Failure.decode(String(describing: error))
        }
    }

    /// 只查指定挂载点的占用，用于面板关闭时更新菜单栏图标。
    ///
    /// 比 `status` 轻得多：不跑 `diskutil`（卷的身份在插拔之间不会变），
    /// 单次约 230ms 对 590ms。所以空闲轮询才敢开。
    static func watch(mountpoints: [String]) async throws -> WatchPayload {
        var args = ["watch", "--json"]
        for mp in mountpoints {
            args.append("--volume")
            args.append(mp)
        }
        let data = try await run(args, timeout: 20)
        do {
            return try JSONDecoder().decode(WatchPayload.self, from: data)
        } catch {
            throw Failure.decode(String(describing: error))
        }
    }

    /// 结束单个进程。`allowRisky` 只有用户逐项确认"仍要结束"后才为真。
    static func end(pid: Int32, volume: String, allowRisky: Bool) async throws -> ActionResult {
        var args = ["end", String(pid), "--volume", volume, "--json"]
        if allowRisky {
            // 用户已经明确选择"仍要结束"，SIGTERM 无效时允许升级到 SIGKILL
            args.append("--allow-risky")
            args.append("--force")
        }
        let data = try await run(args)
        return try decodeAction(data)
    }

    static func eject(volume: String) async throws -> ActionResult {
        let data = try await run(["eject", volume, "--json"])
        return try decodeAction(data)
    }

    private static func decodeAction(_ data: Data) throws -> ActionResult {
        do {
            return try JSONDecoder().decode(ActionResult.self, from: data)
        } catch {
            throw Failure.decode(String(describing: error))
        }
    }

    // MARK: - 子进程

    /// 收尾用的收集器：两个管道在后台线程读，避免任一方写满 64KB 造成死锁。
    private final class Collector: @unchecked Sendable {
        private let lock = NSLock()
        private var buffer = Data()

        func append(_ chunk: Data) {
            lock.lock()
            buffer.append(chunk)
            lock.unlock()
        }

        var data: Data {
            lock.lock()
            defer { lock.unlock() }
            return buffer
        }
    }

    private static func run(_ arguments: [String], timeout: TimeInterval = 40) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    continuation.resume(returning: try runSync(arguments, timeout: timeout))
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private static func runSync(_ arguments: [String], timeout: TimeInterval) throws -> Data {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [scriptPath] + arguments

        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        // 从访达启动时 PATH 很短，补上 diskutil/lsof 所在目录
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = env

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe
        process.standardInput = FileHandle.nullDevice

        do {
            try process.run()
        } catch {
            throw Failure.launch(error.localizedDescription)
        }

        let out = Collector()
        let err = Collector()
        let group = DispatchGroup()

        group.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            out.append(outPipe.fileHandleForReading.readDataToEndOfFile())
            group.leave()
        }
        group.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            err.append(errPipe.fileHandleForReading.readDataToEndOfFile())
            group.leave()
        }

        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline {
            usleep(40_000)
        }
        if process.isRunning {
            process.terminate()
            process.waitUntilExit()
            throw Failure.nonZero(code: -1, stderr: "volfree 执行超时")
        }

        group.wait()
        process.waitUntilExit()

        let stderr = String(data: err.data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

        // end/eject 用退出码 1 表示"动作未完成但流程正常"，仍需把 JSON 读出来
        if process.terminationStatus != 0 && out.data.isEmpty {
            throw Failure.nonZero(code: process.terminationStatus, stderr: stderr)
        }
        return out.data
    }
}
