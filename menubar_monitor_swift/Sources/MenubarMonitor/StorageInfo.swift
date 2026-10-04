import Foundation
import IOKit

/// 起動ボリュームの容量(バイト)
struct StorageUsage {
    var total: Int64
    /// Finder/システム設定と同じく、パージ可能領域を含めた「重要な用途に使える空き容量」
    var available: Int64
}

enum StorageInfo {
    /// statfs("/")はAPFSの読み取り専用システムボリューム分しか数えず、実使用量と大きくずれるので使わない
    static func usage() -> StorageUsage? {
        // URLはリソース値をキャッシュするため、毎回新しく作る
        let url = URL(fileURLWithPath: "/")
        guard let values = try? url.resourceValues(forKeys: [.volumeTotalCapacityKey, .volumeAvailableCapacityForImportantUsageKey]),
              let total = values.volumeTotalCapacity,
              let available = values.volumeAvailableCapacityForImportantUsage else { return nil }
        return StorageUsage(total: Int64(total), available: available)
    }
}

/// ディスク全体の読み書き速度(MB/s)とビジー率(%)
struct DiskIO {
    var read: Double
    var write: Double
    var busy: Double
}

/// sample()を呼ぶたびに、前回からの読み書き速度・ビジー率を返す。
/// ビジー率は読み書きの処理にかかった累計時間の増分を経過時間で割ったもの。SSDは多数の読み書きを並行して
/// 処理し、その時間が重なって数えられるため100%を超えることがあるので、100%で打ち切って「混み具合」の目安にする
final class DiskIOSampler {
    private struct Counters {
        var readBytes: UInt64 = 0, writeBytes: UInt64 = 0
        /// 読み書きにかかった累計時間(ns)
        var readTime: UInt64 = 0, writeTime: UInt64 = 0
    }

    private var previous: (time: TimeInterval, counters: Counters)?

    /// 全ディスクの合計。psutil.disk_io_counters()と同じくIOBlockStorageDriverの統計を読む
    private static func counters() -> Counters {
        var total = Counters()
        IORegistry.forEachService(matching: "IOBlockStorageDriver") { entry in
            guard let stats = IORegistry.property(entry, "Statistics") as? [String: Any] else { return }
            func value(_ key: String) -> UInt64 { (stats[key] as? NSNumber)?.uint64Value ?? 0 }
            total.readBytes += value("Bytes (Read)")
            total.writeBytes += value("Bytes (Write)")
            total.readTime += value("Total Time (Read)")
            total.writeTime += value("Total Time (Write)")
        }
        return total
    }

    func sample() -> DiskIO {
        let now = monotonicNow()
        let counters = Self.counters()
        defer { previous = (now, counters) }
        var result = DiskIO(read: 0, write: 0, busy: 0)
        // カウンタのリセット(ディスクの付け外しなど)で減った場合は捨てる
        guard let previous, now > previous.time,
              counters.readBytes >= previous.counters.readBytes,
              counters.writeBytes >= previous.counters.writeBytes else { return result }
        let elapsed = now - previous.time
        result.read = Double(counters.readBytes - previous.counters.readBytes) / elapsed / 1_000_000
        result.write = Double(counters.writeBytes - previous.counters.writeBytes) / elapsed / 1_000_000
        let busyNs = Double(counters.readTime &- previous.counters.readTime) + Double(counters.writeTime &- previous.counters.writeTime)
        result.busy = min(max(busyNs / (elapsed * 1e9) * 100, 0), 100)
        return result
    }
}
