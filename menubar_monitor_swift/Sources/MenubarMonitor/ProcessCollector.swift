import Foundation

/// アプリ別の一覧(nilは集計中)
struct ProcessResults: Sendable {
    var memory: [MemoryGroup]
    var cpu: [UsageEntry]?
    var gpu: [UsageEntry]?
    /// 読み込みと書き込みの合計(バイト/秒)
    var disk: [UsageEntry]?
    var net: [UsageEntry]?
}

/// アプリ別の集計をバックグラウンドで行う。全プロセスの走査やnettopの実行は1回で0.1〜0.8秒ほどかかり、
/// メインスレッドで行うとその間メニューのホバーやクリックが止まるため、専用のキューで順番に処理する。
/// 速度の基準(前回の累計)を持つサンプラーはこのキューの上でだけ触る
final class ProcessCollector: @unchecked Sendable {
    private let queue = DispatchQueue(label: "local.system-monitor.MenubarMonitor.processes", qos: .utility)
    private let cpuSampler = GroupRateSampler.cpu()
    private let diskSampler = GroupRateSampler.disk()
    private let netSampler = NetProcessSampler()
    private let gpuSampler = GPUProcessSampler()

    /// 集計してcompletionをメインスレッドで呼ぶ
    func collect(_ completion: @escaping @MainActor @Sendable (ProcessResults) -> Void) {
        queue.async { [self] in
            // メモリ・CPU・ディスク・ネットワークのランキングは同じスナップショットから作る
            let snapshot = ProcessSnapshot.take()
            let results = ProcessResults(
                memory: snapshot.groupedMemory(total: MemoryInfo.total),
                cpu: cpuSampler.sample(snapshot)?.map { UsageEntry(name: $0.name, value: $0.rate) },
                gpu: gpuSampler.sample(snapshot)?.map { UsageEntry(name: $0.name, value: $0.percent) },
                disk: diskSampler.sample(snapshot)?.map { UsageEntry(name: $0.name, value: $0.rate) },
                net: netSampler.sample(snapshot)?.map { UsageEntry(name: $0.name, value: $0.total, up: $0.up, down: $0.down) })
            DispatchQueue.main.async { completion(results) }
        }
    }

    /// 次に集計したとき、間隔が空いた間の平均ではなく直近の値を出すため基準を捨てる。
    /// 集計の途中で割り込まないよう、同じキューに積む
    func reset() {
        queue.async { [self] in
            cpuSampler.reset()
            diskSampler.reset()
            netSampler.reset()
            gpuSampler.reset()
        }
    }
}
