import Darwin

/// CPU全体の使用率。前回呼び出しからのティック数の差分で計算する(psutil.cpu_percent(interval=None)と同じ考え方)
final class CPUSampler {
    private var previous: host_cpu_load_info?

    /// 使用率(%)。初回や取得失敗時はnil
    func sample() -> Double? {
        var info = host_cpu_load_info()
        var count = mach_msg_type_number_t(MemoryLayout<host_cpu_load_info>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                host_statistics(mach_host_self(), HOST_CPU_LOAD_INFO, $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { return nil }
        defer { previous = info }
        guard let previous else { return nil }

        // cpu_ticksは(user, system, idle, nice)の順
        func ticks(_ load: host_cpu_load_info) -> (busy: Double, total: Double) {
            let t = load.cpu_ticks
            let user = Double(t.0), system = Double(t.1), idle = Double(t.2), nice = Double(t.3)
            return (user + system + nice, user + system + idle + nice)
        }
        let now = ticks(info), before = ticks(previous)
        let total = now.total - before.total
        return total > 0 ? (now.busy - before.busy) / total * 100 : nil
    }
}
