import Foundation

/// 外部コマンドの実行(nettop・ps・system_profilerなど、公開APIでは取れない情報の取得に使う)
enum Command {
    /// 標準出力を返す。起動できない・終了コードが0以外ならnil
    static func run(_ path: String, _ arguments: [String]) -> Data? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return nil
        }
        // 出力がパイプの容量を超えると子プロセスが止まるので、終了を待つ前に読み切る
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return process.terminationStatus == 0 ? data : nil
    }

    static func runText(_ path: String, _ arguments: [String]) -> String? {
        run(path, arguments).flatMap { String(data: $0, encoding: .utf8) }
    }
}

/// 経過時間の計測に使う単調増加の時刻(秒)。Pythonのtime.monotonic()に相当
func monotonicNow() -> TimeInterval {
    ProcessInfo.processInfo.systemUptime
}
