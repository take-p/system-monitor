import Darwin
import Foundation

/// CPUの使用率。前回呼び出しからのティック数の差分で計算する(psutil.cpu_percent(interval=None)と同じ考え方)
final class CPUSampler {
    /// コアごとの(busy, total)の累計ティック
    private var previous: [(busy: Double, total: Double)]?

    /// コアごとの累計ティック。各コアの値は(user, system, idle, nice)の順に並ぶ
    private static func coreTicks() -> [(busy: Double, total: Double)]? {
        var count: natural_t = 0
        var info: processor_info_array_t?
        var infoCount: mach_msg_type_number_t = 0
        guard host_processor_info(mach_host_self(), PROCESSOR_CPU_LOAD_INFO, &count, &info, &infoCount) == KERN_SUCCESS,
              let info else { return nil }
        defer {
            vm_deallocate(mach_task_self_, vm_address_t(bitPattern: info), vm_size_t(Int(infoCount) * MemoryLayout<integer_t>.stride))
        }
        let states = Int(CPU_STATE_MAX)
        return (0..<Int(count)).map { core in
            func tick(_ state: Int32) -> Double { Double(UInt32(bitPattern: info[core * states + Int(state)])) }
            let user = tick(CPU_STATE_USER), system = tick(CPU_STATE_SYSTEM)
            let idle = tick(CPU_STATE_IDLE), nice = tick(CPU_STATE_NICE)
            return (user + system + nice, user + system + idle + nice)
        }
    }

    /// (全体の使用率%, コアごとの使用率%)。初回や取得失敗時はnil
    func sample() -> (total: Double, cores: [Double])? {
        guard let now = Self.coreTicks() else { return nil }
        defer { previous = now }
        guard let previous, previous.count == now.count else { return nil }
        var busy = 0.0, total = 0.0
        let cores = zip(now, previous).map { current, before in
            let coreBusy = current.busy - before.busy, coreTotal = current.total - before.total
            busy += coreBusy
            total += coreTotal
            return coreTotal > 0 ? min(max(coreBusy / coreTotal * 100, 0), 100) : 0
        }
        return (total > 0 ? busy / total * 100 : 0, cores)
    }

    /// コア番号順に「P1」「E1」のようなラベルを返す。Apple SiliconはCPUノードにcluster-type(P=高性能/E=高効率)があり、
    /// logical-cpu-idがhost_processor_infoの番号と一致する。取れない場合は「Core 1」形式にする
    static func coreLabels(count: Int) -> [String] {
        var types: [Int: String] = [:]
        IORegistry.forEachService(matching: "IOPlatformDevice") { entry in
            guard let cluster = IORegistry.property(entry, "cluster-type") as? Data,
                  let id = IORegistry.property(entry, "logical-cpu-id") else { return }
            let number: Int?
            if let n = id as? NSNumber {
                number = n.intValue
            } else if let d = id as? Data, d.count >= 4 {
                number = Int(d.withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) })
            } else {
                number = nil
            }
            if let number {
                types[number] = String(decoding: cluster.prefix { $0 != 0 }, as: UTF8.self)
            }
        }
        guard types.keys.sorted() == Array(0..<count) else {
            return (0..<count).map { "Core \($0 + 1)" }
        }
        var seen: [String: Int] = [:]
        return (0..<count).map { core in
            let type = types[core]!
            seen[type, default: 0] += 1
            return "\(type)\(seen[type]!)"
        }
    }
}
