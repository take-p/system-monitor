import Darwin
import Foundation
import IOKit

/// GPU全体の使用率と、GPUが使っている統合メモリの量
struct GPUUsage {
    var percent: Double
    /// キーが無い機種ではnil(表示を省く)
    var memory: UInt64?
}

enum GPUInfo {
    /// IOAcceleratorのPerformanceStatisticsはsudo不要で読める。見つからなければnil(0%と区別して「--」表示にする)
    static func usage() -> GPUUsage? {
        var result: GPUUsage?
        IORegistry.forEachService(matching: "IOAccelerator") { entry in
            guard result == nil,
                  let stats = IORegistry.property(entry, "PerformanceStatistics") as? [String: Any],
                  let percent = (stats["Device Utilization %"] as? NSNumber)?.doubleValue else { return }
            result = GPUUsage(percent: percent, memory: (stats["In use system memory"] as? NSNumber)?.uint64Value)
        }
        return result
    }

    /// {pid: (名前, 累計GPU時間ns)}。GPUを使うプロセスごとのクライアント(AGXDeviceUserClient)に
    /// 起動からの累計GPU時間が載っている。名前は16文字に切り詰められている
    static func processTimes() -> [pid_t: (name: String, time: UInt64)] {
        var times: [pid_t: (name: String, time: UInt64)] = [:]
        IORegistry.forEachService(matching: "IOAccelerator") { accelerator in
            IORegistry.forEachChild(of: accelerator) { client in
                guard let props = IORegistry.properties(client),
                      let creator = props["IOUserClientCreator"] as? String,
                      let (pid, name) = parseCreator(creator) else { return }
                let usages = props["AppUsage"] as? [[String: Any]] ?? []
                let gpuTime = usages.reduce(UInt64(0)) { $0 + ((($1["accumulatedGPUTime"]) as? NSNumber)?.uint64Value ?? 0) }
                // 1プロセスが複数のクライアントを持つことがあるので合算する
                times[pid, default: (name, 0)].time += gpuTime
            }
        }
        return times
    }

    /// 「pid 605, WindowServer」を(605, "WindowServer")にする
    private static func parseCreator(_ text: String) -> (pid_t, String)? {
        guard text.hasPrefix("pid "), let comma = text.firstIndex(of: ",") else { return nil }
        guard let pid = pid_t(text[text.index(text.startIndex, offsetBy: 4)..<comma]) else { return nil }
        return (pid, text[text.index(after: comma)...].trimmingCharacters(in: .whitespaces))
    }
}

/// sample()を呼ぶたびに、前回からのアプリ単位のGPU使用率を返す(Activity Monitorの「% GPU」と同じ考え方)
final class GPUProcessSampler {
    private var previous: (time: TimeInterval, times: [pid_t: (name: String, time: UInt64)])?

    /// 間隔が空いた後の差分は長時間の平均になってしまうので、基準を取り直させる
    func reset() {
        previous = nil
    }

    /// [(アプリ名, 使用率%)] を使用率の高い順に返す。初回(基準の取得のみ)はnil。
    /// snapshotはXPCヘルパーを責任元アプリにまとめた後の名前を引くのに使う
    func sample(_ snapshot: ProcessSnapshot) -> [(name: String, percent: Double)]? {
        let now = monotonicNow()
        let times = GPUInfo.processTimes()
        defer { previous = (now, times) }
        guard let previous, now > previous.time else { return nil }
        let elapsedNs = (now - previous.time) * 1e9
        var groups: [pid_t: (name: String?, percent: Double)] = [:]
        for (pid, entry) in times {
            // 前回いなかったプロセスは今回分を測れないので、次回から数える
            guard let before = previous.times[pid], entry.time > before.time else { continue }
            // XPCヘルパー(WebKitのGPUプロセス等)は責任元アプリにまとめる。メモリ欄のグループ化と同じ考え方
            let root = responsiblePID(pid) ?? pid
            if root == pid { groups[root, default: (nil, 0)].name = entry.name }
            groups[root, default: (nil, 0)].percent += Double(entry.time - before.time) / elapsedNs * 100
        }
        return groups.map { root, group in
            (snapshot.procs[root]?.name ?? group.name ?? "pid:\(root)", group.percent)
        }.sorted { $0.percent > $1.percent }
    }
}
