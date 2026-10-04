import Foundation
import Darwin

/// メモリ全体の内訳(バイト)。Activity Monitorの表示に合わせた分類
struct MemoryUsage {
    var app: UInt64
    var wired: UInt64
    var compressed: UInt64
    /// キャッシュされたファイル
    var cached: UInt64
    var free: UInt64
    var total: UInt64

    /// Activity Monitorの「使用済みメモリ」(キャッシュされたファイルは含まない)
    var used: UInt64 { app + wired + compressed }
    var percent: Double { Double(used) / Double(total) * 100 }
}

enum MemoryInfo {
    static let total = ProcessInfo.processInfo.physicalMemory

    /// Python版はvm_statの出力を読んでいたが、同じ値をhost_statistics64で直接取る
    static func usage() -> MemoryUsage? {
        var stats = vm_statistics64()
        var count = mach_msg_type_number_t(MemoryLayout<vm_statistics64>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &stats) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                host_statistics64(mach_host_self(), HOST_VM_INFO64, $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { return nil }
        let pageSize = UInt64(getpagesize())
        let compressed = UInt64(stats.compressor_page_count) * pageSize
        // File-backed pagesはActivity Monitorが「キャッシュされたファイル」としてアプリメモリから分ける分類
        let cached = UInt64(stats.external_page_count) * pageSize
        // Activity Monitorの「アプリメモリ」に相当: (anonymous(internal) - purgeable) * page_size
        let anonymous = UInt64(stats.internal_page_count), purgeable = UInt64(stats.purgeable_count)
        let app = (anonymous > purgeable ? anonymous - purgeable : 0) * pageSize
        let wired = UInt64(stats.wire_count) * pageSize
        let accounted = app + wired + compressed + cached
        return MemoryUsage(
            app: app, wired: wired, compressed: compressed, cached: cached,
            free: total > accounted ? total - accounted : 0, total: total
        )
    }
}
