import CoreWLAN
import Darwin
import Foundation
import SystemConfiguration

/// 電波品質の評価レベル。表示側で色・Wi-Fiアイコンの段数に変換する
enum SignalLevel: Int {
    case poor, weak, fair, good, excellent
}

struct Rating<Level> {
    var label: String
    var level: Level
}

/// 評価の(下限値, 表示, レベル)。規格で決まった値はなく、機器メーカーの設計ガイド等で一般的な目安
private let signalRatings: [(Int, String, SignalLevel)] = [
    (-50, "非常に良い", .excellent), (-60, "良い", .good), (-67, "普通", .fair), (-80, "弱い", .weak),
]
private let snrRatings: [(Int, String, SignalLevel)] = [
    (40, "非常に良い", .excellent), (25, "良い", .good), (15, "普通", .fair), (10, "弱い", .weak),
]

private func rateQuality(_ value: Int, _ ratings: [(Int, String, SignalLevel)]) -> Rating<SignalLevel> {
    for (threshold, label, level) in ratings where value >= threshold {
        return Rating(label: label, level: level)
    }
    return Rating(label: "悪い", level: .poor)
}

/// Wi-Fiの電波品質
struct WiFiQuality {
    /// 電波の強さ(RSSI, dBm)
    var signal: Int
    var signalRating: Rating<SignalLevel>
    /// ノイズ(dBm)。ノイズ単体には一般的な評価基準が無いので値のみ
    var noise: Int?
    /// 電波の強さとノイズの差(dB)
    var snr: Int?
    var snrRating: Rating<SignalLevel>?
}

/// 起動からの合計
struct NetworkTotals {
    var bytesReceived: UInt64 = 0
    var bytesSent: UInt64 = 0
    var errorsIn: UInt64 = 0
    var errorsOut: UInt64 = 0
    /// 受信側のドロップ数。macOSでは送信側のドロップ数は取得できない
    var dropsIn: UInt64 = 0
}

struct NetworkStatus {
    /// デフォルト経路のインターフェース名(en0など)
    var iface: String?
    /// "Wi-Fi" / "Ethernet" / "Unknown"
    var kind: String
    /// リンク速度(Mbps)。Wi-Fiは刻々と変わる送信レート
    var linkSpeed: Double?
    /// 現在の規格・チャネル幅・ストリーム数での理論最大速度(Mbps)。不明ならnil
    var maxRate: Double?
    /// 「Wi-Fi 5 (802.11ac, 5GHz) / 80MHz (ch 52–64)」の形の接続の説明(インターフェース名は表示側で足す)
    var connection: String?
    var quality: WiFiQuality?
    /// 下り/上りの速度(Mbps)
    var download: Double
    var upload: Double
    var peakDownload: Double
    var peakUpload: Double
    var totals: NetworkTotals
}

// MARK: - Wi-Fiの規格

private let phyModeNames: [Int: String] = [
    1: "802.11a", 2: "802.11b", 3: "802.11g", 4: "802.11n", 5: "802.11ac", 6: "802.11ax", 7: "802.11be",
]
private let wifiGenerations = ["802.11n": "Wi-Fi 4", "802.11ac": "Wi-Fi 5", "802.11ax": "Wi-Fi 6", "802.11be": "Wi-Fi 7"]
let channelBandNames: [Int: String] = [1: "2.4GHz", 2: "5GHz", 3: "6GHz"]
let channelWidthsMHz: [Int: Int] = [1: 20, 2: 40, 3: 80, 4: 160]

/// 規格・チャネル幅ごとの1ストリームあたり理論最大速度(Mbps)と、その時の最大MCS。
/// 最短ガードインターバル(11n/ac: 400ns, 11ax/be: 0.8us)での値
private let maxRatePerStream: [String: [Int: (rate: Double, mcs: Int)]] = [
    "802.11n": [20: (72.2, 7), 40: (150.0, 7)],
    "802.11ac": [20: (86.7, 8), 40: (200.0, 9), 80: (433.3, 9), 160: (866.7, 9)],
    "802.11ax": [20: (143.4, 11), 40: (286.8, 11), 80: (600.5, 11), 160: (1201.0, 11)],
    "802.11be": [20: (172.1, 13), 40: (344.1, 13), 80: (720.6, 13), 160: (1441.2, 13)],
]
/// 空間ストリームを持たない旧規格の最大速度(Mbps)
private let legacyMaxRate = ["802.11a": 54.0, "802.11b": 11.0, "802.11g": 54.0]
/// MCS 0〜13の「変調ビット数×符号化率」。最大MCSとの比から任意MCSの速度を求める
private let mcsEfficiency: [Double] = [0.5, 1, 1.5, 2, 3, 4, 4.5, 5, 6, 20.0 / 3, 7.5, 25.0 / 3, 9, 10]

/// 送信レートを「そのMCSでの1ストリームあたり速度」で割ってストリーム数を求める
private func estimateSpatialStreams(standard: String?, widthMHz: Int, mcs: Int, rate: Double) -> Int? {
    if standard == "802.11n" && mcs >= 8 {
        // 11nのMCS番号はストリーム数を含む(0-7: 1本, 8-15: 2本, ...)
        return mcs / 8 + 1
    }
    guard let standard, let max = maxRatePerStream[standard]?[widthMHz], (0...max.mcs).contains(mcs) else { return nil }
    let perStream = max.rate * mcsEfficiency[mcs] / mcsEfficiency[max.mcs]
    // 長いガードインターバルでは速度が最大15%程度下がるが、四捨五入で吸収できる
    return Swift.max(1, Int((rate / perStream).rounded()))
}

private func maxRate(standard: String?, widthMHz: Int?, streams: Int?) -> Double? {
    guard let standard else { return nil }
    if let legacy = legacyMaxRate[standard] { return legacy }
    guard let widthMHz, let streams, let max = maxRatePerStream[standard]?[widthMHz] else { return nil }
    return max.rate * Double(streams)
}

/// CoreWLANの公開APIでは空間ストリーム数が取れないため、system_profilerのMCSと送信レートから逆算する。
/// system_profilerは数秒かかるのでバックグラウンドで更新する。MCSとレートは同じ瞬間の値とは限らず
/// 1本と誤判定されることがあるため、同じ接続(インターフェース・チャネル・幅)で観測した最大値を採用する
final class SpatialStreams: @unchecked Sendable {
    static let shared = SpatialStreams()
    private static let checkInterval: TimeInterval = 30

    private let lock = NSLock()
    private var streams: [String: Int] = [:]
    private var started = false

    private static func key(_ iface: String, _ channel: Int, _ widthMHz: Int) -> String {
        "\(iface)/\(channel)/\(widthMHz)"
    }

    func get(iface: String, channel: Int, widthMHz: Int) -> Int? {
        lock.withLock { streams[Self.key(iface, channel, widthMHz)] }
    }

    func start() {
        let shouldStart = lock.withLock {
            defer { started = true }
            return !started
        }
        guard shouldStart else { return }
        Thread.detachNewThread { [self] in
            while true {
                update()
                Thread.sleep(forTimeInterval: Self.checkInterval)
            }
        }
    }

    private func update() {
        guard let data = Command.run("/usr/sbin/system_profiler", ["SPAirPortDataType", "-json"]),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let airport = (json["SPAirPortDataType"] as? [[String: Any]])?.first,
              let interfaces = airport["spairport_airport_interfaces"] as? [[String: Any]] else { return }
        for itf in interfaces {
            guard let name = itf["_name"] as? String,
                  let info = itf["spairport_current_network_information"] as? [String: Any],
                  // 例: "44 (5GHz, 20MHz)"
                  let channelText = info["spairport_network_channel"] as? String,
                  let match = channelText.firstMatch(of: /^(\d+) \(.*?(\d+)MHz/),
                  let channel = Int(match.1), let widthMHz = Int(match.2),
                  let mcs = (info["spairport_network_mcs"] as? NSNumber)?.intValue,
                  let rate = (info["spairport_network_rate"] as? NSNumber)?.doubleValue,
                  let estimated = estimateSpatialStreams(standard: info["spairport_network_phymode"] as? String,
                                                         widthMHz: widthMHz, mcs: mcs, rate: rate) else { continue }
            let key = Self.key(name, channel, widthMHz)
            lock.withLock { streams[key] = max(estimated, streams[key] ?? 0) }
        }
    }
}

// MARK: - リンク情報

/// Wi-Fiの規格・チャネルの説明と、理論最大速度の計算に使う値
private struct WiFiLink {
    var linkSpeed: Double?
    var maxRate: Double?
    var connection: String?
    var quality: WiFiQuality?
}

private func wifiLink(_ wifi: CWInterface, iface: String) -> WiFiLink {
    var link = WiFiLink()
    let rate = wifi.transmitRate()
    link.linkSpeed = rate > 0 ? rate : nil
    let standard = phyModeNames[wifi.activePHYMode().rawValue]
    let channel = wifi.wlanChannel()
    let band = channel.flatMap { channelBandNames[$0.channelBand.rawValue] }
    let widthMHz = channel.flatMap { channelWidthsMHz[$0.channelWidth.rawValue] }
    // 6GHz帯を使う802.11axはWi-Fi 6Eと呼ばれる
    let generation = standard == "802.11ax" && band == "6GHz" ? "Wi-Fi 6E" : standard.flatMap { wifiGenerations[$0] }
    if standard != nil || channel != nil {
        let detail = [standard, band].compactMap { $0 }.joined(separator: ", ")
        var text = (generation ?? "Wi-Fi") + (detail.isEmpty ? "" : " (\(detail))")
        if let channel {
            let number = channel.channelNumber
            var channelsText = "ch \(number)"
            if let band, let widthMHz, widthMHz > 20 {
                // 複数チャネルを束ねて使っている場合(チャネルボンディング)は、実際に使っている範囲で示す。
                // 2.4GHzは重なりの裾まで含めず、中心チャネルだけを使用中とする
                let used = occupiedChannels(band: band, primary: number, widthMHz: widthMHz)
                    .filter { $0.value == 1 }.keys.sorted()
                if used.count > 1, let first = used.first, let last = used.last {
                    channelsText = "ch \(first)–\(last)"
                }
            }
            text += " / \(widthMHz.map { "\($0)MHz" } ?? "?") (\(channelsText))"
        }
        link.connection = text
    }
    // 未接続時はRSSIが0になるので表示しない
    let rssi = wifi.rssiValue(), noise = wifi.noiseMeasurement()
    if rssi < 0 {
        var quality = WiFiQuality(signal: rssi, signalRating: rateQuality(rssi, signalRatings))
        if noise < 0 {
            quality.noise = noise
            quality.snr = rssi - noise
            quality.snrRating = rateQuality(rssi - noise, snrRatings)
        }
        link.quality = quality
    }
    let streams = channel.flatMap { channel in
        widthMHz.flatMap { SpatialStreams.shared.get(iface: iface, channel: channel.channelNumber, widthMHz: $0) }
    }
    link.maxRate = maxRate(standard: standard, widthMHz: widthMHz, streams: streams)
    return link
}

/// 有線はifconfigのmedia行(例: "autoselect (1000baseT <full-duplex>)")から読み取る。
/// 有線はネゴシエーションで決まったリンク速度がそのまま上限になる
private func ethernetSpeed(_ iface: String) -> Double? {
    guard let output = Command.runText("/sbin/ifconfig", [iface]),
          let match = output.firstMatch(of: /media:.*\((\d+(?:\.\d+)?)(G?)base/.ignoresCase()),
          let speed = Double(match.1) else { return nil }
    return speed * (match.2.uppercased() == "G" ? 1000 : 1)
}

// MARK: - 速度の計測

/// OSがインターネット向け通信に使うインターフェース(デフォルト経路)
private func defaultInterface() -> String? {
    guard let store = SCDynamicStoreCreate(nil, "MenubarMonitor" as CFString, nil, nil) else { return nil }
    for key in ["State:/Network/Global/IPv4", "State:/Network/Global/IPv6"] {
        if let value = SCDynamicStoreCopyValue(store, key as CFString) as? [String: Any],
           let iface = value["PrimaryInterface"] as? String {
            return iface
        }
    }
    return nil
}

/// インターフェースの累計カウンタ。getifaddrsのif_dataは32bitで4GBごとに一周するので、64bit版を読む
private func interfaceCounters(_ name: String) -> NetworkTotals? {
    var mib: [Int32] = [CTL_NET, PF_ROUTE, 0, 0, NET_RT_IFLIST2, 0]
    var length = 0
    guard sysctl(&mib, 6, nil, &length, nil, 0) == 0 else { return nil }
    var buffer = [UInt8](repeating: 0, count: length)
    guard sysctl(&mib, 6, &buffer, &length, nil, 0) == 0 else { return nil }
    return buffer.withUnsafeBytes { raw -> NetworkTotals? in
        var offset = 0
        var nameBuffer = [CChar](repeating: 0, count: Int(IF_NAMESIZE))
        while offset + MemoryLayout<if_msghdr>.size <= length {
            let header = raw.loadUnaligned(fromByteOffset: offset, as: if_msghdr.self)
            defer { offset += Int(header.ifm_msglen) }
            guard header.ifm_msglen > 0 else { return nil }
            guard Int32(header.ifm_type) == RTM_IFINFO2,
                  if_indextoname(UInt32(header.ifm_index), &nameBuffer) != nil,
                  String(decoding: nameBuffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self) == name else { continue }
            let data = raw.loadUnaligned(fromByteOffset: offset, as: if_msghdr2.self).ifm_data
            return NetworkTotals(bytesReceived: data.ifi_ibytes, bytesSent: data.ifi_obytes,
                                 errorsIn: data.ifi_ierrors, errorsOut: data.ifi_oerrors, dropsIn: data.ifi_iqdrops)
        }
        return nil
    }
}

/// 呼び出しごとに前回からの差分で速度(Mbps)を求め、起動からの合計・最高速度を集計する
final class NetworkSampler {
    private static let ifaceCheckInterval: TimeInterval = 5

    private var iface: String?
    private var previous: (time: TimeInterval, counters: NetworkTotals)?
    private var totals = NetworkTotals()
    private var download = 0.0, upload = 0.0
    private var peakDownload = 0.0, peakUpload = 0.0
    private var lastIfaceCheck: TimeInterval?

    init() {
        // system_profilerはWi-Fi以外の環境でも無害なので常に起動しておく
        SpatialStreams.shared.start()
    }

    func sample() -> NetworkStatus {
        let now = monotonicNow()
        // 有線接続やVPNでデフォルト経路が切り替わることがあるので定期的に追従する
        if lastIfaceCheck.map({ now - $0 >= Self.ifaceCheckInterval }) ?? true {
            lastIfaceCheck = now
            let newIface = defaultInterface()
            if newIface != iface {
                iface = newIface
                previous = nil
                download = 0
                upload = 0
            }
        }

        if let iface, let counters = interfaceCounters(iface) {
            if let previous, now > previous.time {
                let before = previous.counters
                // カウンタのリセット(インターフェース再接続など)で減った場合は捨てる
                if counters.bytesReceived >= before.bytesReceived, counters.bytesSent >= before.bytesSent,
                   counters.errorsIn >= before.errorsIn, counters.errorsOut >= before.errorsOut,
                   counters.dropsIn >= before.dropsIn {
                    let elapsed = now - previous.time
                    let received = counters.bytesReceived - before.bytesReceived
                    let sent = counters.bytesSent - before.bytesSent
                    totals.bytesReceived += received
                    totals.bytesSent += sent
                    totals.errorsIn += counters.errorsIn - before.errorsIn
                    totals.errorsOut += counters.errorsOut - before.errorsOut
                    totals.dropsIn += counters.dropsIn - before.dropsIn
                    download = Double(received) * 8 / elapsed / 1_000_000
                    upload = Double(sent) * 8 / elapsed / 1_000_000
                    peakDownload = max(peakDownload, download)
                    peakUpload = max(peakUpload, upload)
                }
            }
            previous = (now, counters)
        }

        var status = NetworkStatus(iface: iface, kind: "Unknown", download: download, upload: upload,
                                   peakDownload: peakDownload, peakUpload: peakUpload, totals: totals)
        guard let iface else { return status }
        // Wi-FiはCoreWLANで現在の送信レート・規格・チャネルを取得する(刻々と変化する)
        if CWWiFiClient.shared().interfaceNames()?.contains(iface) == true, let wifi = CWWiFiClient.shared().interface(withName: iface) {
            let link = wifiLink(wifi, iface: iface)
            status.kind = "Wi-Fi"
            status.linkSpeed = link.linkSpeed
            status.maxRate = link.maxRate
            status.connection = link.connection
            status.quality = link.quality
        } else if let speed = ethernetSpeed(iface) {
            status.kind = "Ethernet"
            status.linkSpeed = speed
        }
        return status
    }
}
