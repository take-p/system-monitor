import Foundation

/// アプリ単位のネットワーク通信速度。macOS標準のnettopはsudo不要で、他ユーザー所有(rootなど)を含む
/// 全プロセスの送受信の累計バイト数を出せる。前回との差分を経過時間で割って速度にする。
/// (nettopの実行中に生まれて消えた短命のプロセスは数えられない)
final class NetProcessSampler {
    private var previous: (time: TimeInterval, totals: [pid_t: (name: String, received: UInt64, sent: UInt64)])?

    /// {pid: (名前, 受信bytes, 送信bytes)}。nettopの名前は15文字で切れるので、表示にはなるべく使わない
    static func snapshot() -> [pid_t: (name: String, received: UInt64, sent: UInt64)]? {
        guard let output = Command.runText("/usr/bin/nettop", ["-P", "-L", "1", "-x", "-J", "bytes_in,bytes_out"]) else {
            return nil
        }
        var totals: [pid_t: (name: String, received: UInt64, sent: UInt64)] = [:]
        for line in output.split(separator: "\n").dropFirst() {  // 1行目は列名
            // 「プロセス名.pid,受信,送信,」の形。プロセス名に「.」や「,」を含むことがあるので右から分ける
            var fields = line.hasSuffix(",") ? line.dropLast() : line[...]
            guard let sentComma = fields.lastIndex(of: ",") else { continue }
            let sent = UInt64(fields[fields.index(after: sentComma)...])
            fields = fields[..<sentComma]
            guard let receivedComma = fields.lastIndex(of: ",") else { continue }
            let received = UInt64(fields[fields.index(after: receivedComma)...])
            let namePid = fields[..<receivedComma]
            guard let dot = namePid.lastIndex(of: "."), let pid = pid_t(namePid[namePid.index(after: dot)...]),
                  let received, let sent else { continue }
            totals[pid] = (String(namePid[..<dot]), received, sent)
        }
        return totals
    }

    /// 間隔が空いた後の差分は長時間の平均になってしまうので、基準を取り直させる
    func reset() {
        previous = nil
    }

    /// [(アプリ名, 上り+下り, 上り, 下り)] をMbpsで、合計の多い順に返す。初回(基準の取得のみ)はnil
    func sample(_ snapshot: ProcessSnapshot) -> [(name: String, total: Double, up: Double, down: Double)]? {
        let now = monotonicNow()
        guard let totals = Self.snapshot() else { return nil }
        defer { previous = (now, totals) }
        guard let previous, now > previous.time else { return nil }
        let elapsed = now - previous.time
        var groups: [pid_t: (up: Double, down: Double)] = [:]
        var names: [pid_t: String] = [:]
        for (pid, entry) in totals {
            // 前回nettopに出ていなかったプロセスは、その間に通信を始めた(累計は0から)ものとして全量を数える
            let before = previous.totals[pid] ?? (entry.name, 0, 0)
            guard entry.received >= before.received, entry.sent >= before.sent else { continue }
            let down = entry.received - before.received, up = entry.sent - before.sent
            guard down + up > 0 else { continue }
            // スナップショットに無い(直後に終了した等)プロセスは単独のグループにし、nettopの名前を使う
            let root = snapshot.roots[pid] ?? pid
            if snapshot.procs[root] == nil { names[root] = entry.name }
            groups[root, default: (0, 0)].up += Double(up) * 8 / elapsed / 1_000_000
            groups[root, default: (0, 0)].down += Double(down) * 8 / elapsed / 1_000_000
        }
        return groups.map { root, group in
            (names[root] ?? snapshot.groupName(root), group.up + group.down, group.up, group.down)
        }.sorted { $0.total > $1.total }
    }
}
