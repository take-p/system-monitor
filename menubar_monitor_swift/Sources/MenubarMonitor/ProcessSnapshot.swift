import Darwin
import Foundation

// XPCサービスの「責任元(親)アプリ」を返す非公開API。Activity Monitorの「対象アプリ」列と同じ仕組み。
// com.apple.WebKit.WebContent等はlaunchd(pid 1)の子として起動されるので、ppidをたどるだけでは
// 起動元アプリ(Safari/Mail/Xcode等)に紐付けられない
@_silgen_name("responsibility_get_pid_responsible_for_pid")
private func responsibility_get_pid_responsible_for_pid(_ pid: pid_t) -> pid_t

func responsiblePID(_ pid: pid_t) -> pid_t? {
    let rpid = responsibility_get_pid_responsible_for_pid(pid)
    return rpid > 0 ? rpid : nil
}

/// rusage_infoのCPU時間はナノ秒ではなくMachの時間単位(Apple Siliconでは1=約41.7ns)なので、この比でナノ秒に直す
let machTimeToNanoseconds: Double = {
    var timebase = mach_timebase_info_data_t()
    mach_timebase_info(&timebase)
    return Double(timebase.numer) / Double(timebase.denom)
}()

/// (footprint bytes, CPU時間ns, ディスク読み書き量bytes)。他ユーザー所有プロセス等は取得できずnil
func processRusage(_ pid: pid_t) -> (footprint: UInt64, cpuTime: Double, diskIO: UInt64)? {
    var info = rusage_info_v4()
    let result = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
            proc_pid_rusage(pid, RUSAGE_INFO_V4, $0)
        }
    }
    guard result == 0 else { return nil }
    return (info.ri_phys_footprint,
            Double(info.ri_user_time + info.ri_system_time) * machTimeToNanoseconds,
            info.ri_diskio_bytesread + info.ri_diskio_byteswritten)
}

/// psのTIME列(「mmm:ss.ss」、長いと「[dd-]hh:mm:ss」)を秒にする
func parsePsTime(_ text: Substring) -> Double? {
    var clock = text, days = 0.0
    if let dash = text.firstIndex(of: "-") {
        guard let d = Double(text[..<dash]) else { return nil }
        days = d
        clock = text[text.index(after: dash)...]
    }
    var seconds = 0.0
    for part in clock.split(separator: ":") {
        guard let value = Double(part) else { return nil }
        seconds = seconds * 60 + value
    }
    return seconds + days * 86400
}

/// {pid: 累計CPU時間ns}。psは特権付きで動くので、他ユーザー所有プロセスの分も取れる
func psCPUTimes() -> [pid_t: Double] {
    guard let output = Command.runText("/bin/ps", ["-A", "-o", "pid=,time="]) else { return [:] }
    var times: [pid_t: Double] = [:]
    for line in output.split(separator: "\n") {
        let fields = line.split(separator: " ", omittingEmptySubsequences: true)
        guard fields.count == 2, let pid = pid_t(fields[0]), let seconds = parsePsTime(fields[1]) else { continue }
        times[pid] = seconds * 1e9
    }
    return times
}

struct ProcessEntry {
    var ppid: pid_t
    var name: String
    var footprint: UInt64
    /// 取得できないプロセスはnil(速度の計算から外す)
    var cpuTime: Double?
    var diskIO: UInt64?
}

/// 全プロセス(pid・ppid・名前)の一覧。sysctlのkinfo_procは他ユーザー所有のプロセスも含む
private func listProcesses() -> [(pid: pid_t, ppid: pid_t, name: String)] {
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0]
    var size = 0
    guard sysctl(&mib, 4, nil, &size, nil, 0) == 0 else { return [] }
    // 1回目と2回目の呼び出しの間にプロセスが増えることがあるので余裕を持たせる
    var buffer = [kinfo_proc](repeating: kinfo_proc(), count: size / MemoryLayout<kinfo_proc>.stride + 32)
    size = buffer.count * MemoryLayout<kinfo_proc>.stride
    guard sysctl(&mib, 4, &buffer, &size, nil, 0) == 0 else { return [] }
    return buffer.prefix(size / MemoryLayout<kinfo_proc>.stride).map { kp in
        var kp = kp
        let comm = withUnsafeBytes(of: &kp.kp_proc.p_comm) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
        let pid = kp.kp_proc.p_pid
        return (pid, kp.kp_eproc.e_ppid, extendedName(pid, comm))
    }
}

/// p_commは15文字で切れるので、実行ファイル名がその続きなら長い方を使う(psutilのname()と同じ考え方)
private func extendedName(_ pid: pid_t, _ name: String) -> String {
    guard name.utf8.count >= 15 else { return name }
    var path = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))  // PROC_PIDPATHINFO_MAXSIZE
    guard proc_pidpath(pid, &path, UInt32(path.count)) > 0 else { return name }
    let base = (String(decoding: path.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self) as NSString).lastPathComponent
    return base.hasPrefix(name) ? base : name
}

/// 全プロセスのスナップショット。メモリ・CPU・ディスク・ネットワークのランキングで共用する
struct ProcessSnapshot {
    var procs: [pid_t: ProcessEntry]
    /// {pid: グループの根のpid}。XPCヘルパーは責任元アプリにつなぎ直してある
    var roots: [pid_t: pid_t]

    static func take() -> ProcessSnapshot {
        var procs: [pid_t: ProcessEntry] = [:]
        for (pid, ppid, name) in listProcesses() {
            // 他ユーザー所有プロセス(rootのloginなど)はrusageが取れないが、ppid/nameは取れているので
            // 0としてツリーに残し、親子関係の連結を保つ
            let usage = processRusage(pid)
            procs[pid] = ProcessEntry(ppid: ppid, name: name, footprint: usage?.footprint ?? 0,
                                      cpuTime: usage?.cpuTime, diskIO: usage?.diskIO)
        }
        // WindowServer等の他ユーザー所有プロセスはCPU使用率の上位に来やすいので、psの値(10ms精度)で補う
        if procs.values.contains(where: { $0.cpuTime == nil }) {
            let psTimes = psCPUTimes()
            for (pid, entry) in procs where entry.cpuTime == nil {
                procs[pid]?.cpuTime = psTimes[pid]
            }
        }
        // XPCヘルパーは、責任元アプリが判明する限り常にそのアプリのグループへまとめる
        for pid in procs.keys {
            guard let rpid = responsiblePID(pid), rpid != pid, procs[rpid] != nil else { continue }
            procs[pid]?.ppid = rpid
        }
        var roots: [pid_t: pid_t] = [:]
        for pid in procs.keys {
            _ = findRoot(pid, procs, &roots)
        }
        return ProcessSnapshot(procs: procs, roots: roots)
    }

    private static func findRoot(_ pid: pid_t, _ procs: [pid_t: ProcessEntry], _ cache: inout [pid_t: pid_t]) -> pid_t {
        if let root = cache[pid] { return root }
        // ppid<=1(launchd)や親プロセスが既に存在しない場合、自分自身をツリーの根とする
        // (責任元のつなぎ直しで循環することがあるので、たどった経路も覚えておく)
        var path: [pid_t] = []
        var current = pid
        while true {
            if let root = cache[current] { current = root; break }
            guard let ppid = procs[current]?.ppid, ppid > 1, procs[ppid] != nil, !path.contains(ppid), ppid != current else {
                break
            }
            path.append(current)
            current = ppid
        }
        for p in path + [pid] { cache[p] = current }
        return current
    }

    func groupName(_ root: pid_t) -> String {
        procs[root]?.name ?? "pid:\(root)"
    }

    /// アプリ単位のメモリ使用量 [(根のpid, 名前, プロセス数, MB, 全体に対する%)] を多い順に返す
    func groupedMemory(total: UInt64) -> [(root: pid_t, name: String, count: Int, megabytes: Double, percent: Double)] {
        var groups: [pid_t: (footprint: UInt64, count: Int)] = [:]
        for (pid, entry) in procs {
            let root = roots[pid] ?? pid
            groups[root, default: (0, 0)].footprint += entry.footprint
            groups[root, default: (0, 0)].count += 1
        }
        return groups.map { root, group in
            (root, groupName(root), group.count, Double(group.footprint) / (1024 * 1024),
             Double(group.footprint) / Double(total) * 100)
        }.sorted { $0.megabytes > $1.megabytes }
    }
}

/// sample()を呼ぶたびに、スナップショットの値の前回からの増分を、グループ単位の1秒あたりの量で返す。
/// divisorで割った値を返す(CPUなら「全コアで1秒」を100%にする値で割って使用率にする)
final class GroupRateSampler {
    private let value: (ProcessEntry) -> Double?
    private let divisor: Double
    private var previous: (time: TimeInterval, totals: [pid_t: Double])?

    init(divisor: Double = 1, value: @escaping (ProcessEntry) -> Double?) {
        self.value = value
        self.divisor = divisor
    }

    /// CPU全体(全コア)を100%とする。合計がメニューの見出しの使用率とおおむね一致する
    static func cpu() -> GroupRateSampler {
        let cores = Double(ProcessInfo.processInfo.activeProcessorCount)
        return GroupRateSampler(divisor: 1e9 * cores / 100) { $0.cpuTime }
    }

    /// 読み込みと書き込みの合計(バイト/秒)
    static func disk() -> GroupRateSampler {
        GroupRateSampler { $0.diskIO.map { Double($0) } }
    }

    /// 間隔が空いた後の差分は長時間の平均になってしまうので、基準を取り直させる
    func reset() {
        previous = nil
    }

    /// [(アプリ名, 1秒あたりの量)] を多い順に返す。初回(基準の取得のみ)はnil
    func sample(_ snapshot: ProcessSnapshot) -> [(name: String, rate: Double)]? {
        let now = monotonicNow()
        let totals = snapshot.procs.compactMapValues(value)
        defer { previous = (now, totals) }
        guard let previous, now > previous.time else { return nil }
        let elapsed = now - previous.time
        var groups: [pid_t: Double] = [:]
        for (pid, total) in totals {
            // 前回いなかったプロセス(pidの再利用を含む)は今回分を測れないので、次回から数える
            let delta = total - (previous.totals[pid] ?? total)
            if delta > 0 {
                groups[snapshot.roots[pid] ?? pid, default: 0] += delta / elapsed / divisor
            }
        }
        return groups.map { (snapshot.groupName($0.key), $0.value) }.sorted { $0.rate > $1.rate }
    }
}
