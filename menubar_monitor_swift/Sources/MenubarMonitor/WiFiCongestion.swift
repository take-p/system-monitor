import CoreWLAN
import Foundation

// 周辺APのスキャン結果から、Wi-Fiチャネルごとの混雑度を推定する。
// スキャン中は無線が他チャネルを聞きに行き通信が一瞬遅れるため、メニューを開いている間だけスキャンする

private let scanInterval: TimeInterval = 5
/// 直近何回分のスキャン結果を平均するか。1回のスキャンでは弱いAPが見えたり消えたりするため
private let smoothingScans = 3
/// 1チャネルの重みの合計がこの値で「混雑度100%」とみなす(強いAP 3台相当)
private let saturationScore = 3.0
/// 重みを0〜1に割り当てるRSSI範囲(dBm)。これより弱い電波は干渉としてほぼ無視できる
private let rssiFloor = -95.0, rssiCeil = -45.0
/// 2.4GHz帯は5MHz間隔に約22MHz幅のチャネルが並ぶため、5ch離れるまで隣と重なる
private let overlapSpan24GHz = 5
/// 5GHz帯でチャネルボンディングの区切りとなる先頭チャネル(W52/W53, W56, W58)
private let bondingBases5GHz = [149, 100, 36]

/// 混雑度の評価レベル。表示側で色に変換する
enum CongestionLevel {
    case free, moderate, busy, heavy
}

/// 混雑度の評価(上限%, 表示, レベル)
func congestionRating(_ percent: Double) -> Rating<CongestionLevel> {
    for (limit, label, level) in [(30.0, "空き", CongestionLevel.free), (60, "やや混雑", .moderate), (85, "混雑", .busy)]
    where percent < limit {
        return Rating(label: label, level: level)
    }
    return Rating(label: "非常に混雑", level: .heavy)
}

/// 電波が強いAPほど干渉・電波の取り合いが大きいとみなして0〜1の重みにする
private func rssiWeight(_ rssi: Int) -> Double {
    min(1, max(0, (Double(rssi) - rssiFloor) / (rssiCeil - rssiFloor)))
}

/// APが実際に電波を出している20MHzチャネルと、その重なり具合(0〜1)を返す
func occupiedChannels(band: String, primary: Int, widthMHz: Int) -> [Int: Double] {
    if band == "2.4GHz" {
        // CoreWLANでは40MHz時のセカンダリ方向が取れないため、一般的な割り当てで推定する
        var centers = [primary]
        if widthMHz == 40 {
            centers.append(primary <= 7 ? primary + 4 : primary - 4)
        }
        var overlap: [Int: Double] = [:]
        for center in centers {
            for offset in (-overlapSpan24GHz + 1)..<overlapSpan24GHz {
                let factor = 1 - Double(abs(offset)) / Double(overlapSpan24GHz)
                overlap[center + offset] = max(overlap[center + offset] ?? 0, factor)
            }
        }
        return overlap
    }
    // 5GHz/6GHzは帯域幅ごとに決まった区切りでまとめて使う(例: 80MHzなら36〜48)
    let span = widthMHz / 20 * 4
    let base = band == "5GHz" ? (bondingBases5GHz.first { primary >= $0 } ?? primary) : 1  // 6GHzは1, 5, 9, ...と4ch間隔
    let start = base + (primary - base) / span * span
    return Dictionary(uniqueKeysWithValues: stride(from: start, to: start + span, by: 4).map { ($0, 1.0) })
}

/// スキャンで見つかったAP
private struct ScannedNetwork {
    var band: String
    var primary: Int
    var widthMHz: Int
    var rssi: Int
}

/// 帯域ごとのチャネル別混雑度
struct BandCongestion {
    var band: String
    /// [(チャネル番号, 混雑度%)]
    var channels: [(number: Int, percent: Double)]
    /// 最も空いているチャネル
    var best: (number: Int, percent: Double)
    /// チャネルごとの電波が掛かっているAP数(直近のスキャン。2.4GHzは部分的な重なりも含む)
    var apCounts: [Int: Int]
}

struct CongestionSnapshot {
    var currentBand: String?
    var primary: Int?
    var widthMHz: Int?
    /// 接続中の20MHzチャネル群
    var current: Set<Int>
    /// スキャン結果がまだ無ければnil
    var bands: [BandCongestion]?
    var scanning: Bool
    /// 最後のスキャンからの経過秒数
    var age: TimeInterval?
    var apCount: Int?
    /// 接続中のチャネル群のうち、最も混んでいるチャネルの値
    var currentPercent: Double?
}

/// メニューを開いている間(activate〜deactivate)だけ、バックグラウンドで一定間隔にスキャンする。
/// スキャンは数秒かかるので、表示側は直近の結果(snapshot)を使う
final class CongestionScanner: @unchecked Sendable {
    private let wifi: CWInterface?
    private let supported: [String: [Int]]
    private let condition = NSCondition()
    // 以下はconditionのロック中に読み書きする
    private var active = false
    private var scanning = false
    private var history: [[ScannedNetwork]] = []
    private var lastScan: Date?

    init() {
        wifi = CWWiFiClient.shared().interface()
        // このMacが使える20MHzチャネル一覧(国ごとの電波法の制限を反映済み)
        var supported: [String: Set<Int>] = [:]
        for channel in wifi?.supportedWLANChannels() ?? [] {
            if let band = channelBandNames[channel.channelBand.rawValue] {
                supported[band, default: []].insert(channel.channelNumber)
            }
        }
        self.supported = supported.mapValues { $0.sorted() }
        if wifi != nil {
            Thread.detachNewThread { [self] in run() }
        }
    }

    func activate() {
        condition.withLock {
            active = true
            condition.signal()
        }
    }

    func deactivate() {
        condition.withLock { active = false }
    }

    private func run() {
        guard let wifi else { return }
        while true {
            condition.withLock {
                while !active { condition.wait() }
                scanning = true
            }
            let networks = Self.scan(wifi)
            condition.withLock {
                scanning = false
                if let networks {
                    history.append(networks)
                    if history.count > smoothingScans { history.removeFirst() }
                    lastScan = Date()
                }
            }
            Thread.sleep(forTimeInterval: scanInterval)
        }
    }

    /// 周辺のAPを返す。位置情報の許可が無いとSSID/BSSIDはnilになるが、チャネルとRSSIは取得できる
    private static func scan(_ wifi: CWInterface) -> [ScannedNetwork]? {
        guard let networks = try? wifi.scanForNetworks(withName: nil, includeHidden: true) else { return nil }
        return networks.compactMap { network in
            guard let channel = network.wlanChannel,
                  let band = channelBandNames[channel.channelBand.rawValue],
                  let widthMHz = channelWidthsMHz[channel.channelWidth.rawValue] else { return nil }
            return ScannedNetwork(band: band, primary: channel.channelNumber, widthMHz: widthMHz, rssi: network.rssiValue)
        }
    }

    /// 全帯域のチャネル別混雑度と、接続中のチャネルの情報をまとめて返す(Wi-Fiが無ければnil)
    func snapshot() -> CongestionSnapshot? {
        guard let wifi, !supported.isEmpty else { return nil }
        let channel = wifi.wlanChannel()
        let band = channel.flatMap { channelBandNames[$0.channelBand.rawValue] }
        let widthMHz = channel.flatMap { channelWidthsMHz[$0.channelWidth.rawValue] }
        var current = Set<Int>(), primary: Int?
        if let channel, let band, let widthMHz {
            primary = channel.channelNumber
            // 2.4GHzは重なりの裾まで含めず、実際の中心チャネルだけを接続中とする
            current = Set(occupiedChannels(band: band, primary: channel.channelNumber, widthMHz: widthMHz)
                .filter { $0.value == 1 }.keys)
        }
        let (history, lastScan, scanning) = condition.withLock { (self.history, self.lastScan, self.scanning) }
        var result = CongestionSnapshot(currentBand: band, primary: primary, widthMHz: widthMHz, current: current,
                                        scanning: scanning, age: lastScan.map { Date().timeIntervalSince($0) })
        guard let latest = history.last else { return result }

        // チャネルごとの混雑スコア(重みの合計)を、直近数回のスキャンで平均する
        var scores: [String: Double] = [:]
        for networks in history {
            for network in networks {
                let weight = rssiWeight(network.rssi)
                for (number, overlap) in occupiedChannels(band: network.band, primary: network.primary, widthMHz: network.widthMHz) {
                    scores["\(network.band)/\(number)", default: 0] += weight * overlap / Double(history.count)
                }
            }
        }
        var bands: [BandCongestion] = []
        for name in ["2.4GHz", "5GHz", "6GHz"] {
            guard let numbers = supported[name], !numbers.isEmpty else { continue }
            let channels = numbers.map { ($0, min(100, (scores["\(name)/\($0)"] ?? 0) / saturationScore * 100)) }
            // 同じ混雑度なら番号の小さいチャネルを優先して提示する
            let best = channels.min { ($0.1, $0.0) < ($1.1, $1.0) }!
            var apCounts: [Int: Int] = [:]
            for number in numbers {
                apCounts[number] = latest.filter {
                    $0.band == name && occupiedChannels(band: $0.band, primary: $0.primary, widthMHz: $0.widthMHz)[number] != nil
                }.count
            }
            bands.append(BandCongestion(band: name, channels: channels.map { (number: $0.0, percent: $0.1) },
                                        best: (best.0, best.1), apCounts: apCounts))
        }
        result.bands = bands
        result.apCount = latest.count
        if let band, let percents = bands.first(where: { $0.band == band })?.channels {
            result.currentPercent = percents.filter { current.contains($0.number) }.map(\.percent).max() ?? 0
        }
        return result
    }
}
