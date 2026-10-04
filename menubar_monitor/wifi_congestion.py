"""周辺APのスキャン結果から、Wi-Fiチャネルごとの混雑度を推定する。

推定ロジックはsimple_monitor/wifi_channel_monitor.pyから移植したもの
(wifi_channel_monitor.pyはimport時に監視ループが走るため、直接importできない)。
スキャン中は無線が他チャネルを聞きに行き通信が一瞬遅れるため、
常駐アプリではメニューを開いている間だけスキャンする。
"""
import threading
import time
from collections import deque

import CoreWLAN
from CoreWLAN import CWWiFiClient

SCAN_INTERVAL = 5.0
# 直近何回分のスキャン結果を平均するか。1回のスキャンでは弱いAPが見えたり消えたりするため
SMOOTHING_SCANS = 3
# 1チャネルの重みの合計がこの値で「混雑度100%」とみなす(強いAP 3台相当)
SATURATION_SCORE = 3.0
# 重みを0〜1に割り当てるRSSI範囲(dBm)。これより弱い電波は干渉としてほぼ無視できる
RSSI_FLOOR, RSSI_CEIL = -95, -45
# 2.4GHz帯は5MHz間隔に約22MHz幅のチャネルが並ぶため、5ch離れるまで隣と重なる
OVERLAP_SPAN_24GHZ = 5

CHANNEL_BAND_NAMES = {
    CoreWLAN.kCWChannelBand2GHz: "2.4GHz",
    CoreWLAN.kCWChannelBand5GHz: "5GHz",
    CoreWLAN.kCWChannelBand6GHz: "6GHz",
}
CHANNEL_WIDTHS_MHZ = {
    CoreWLAN.kCWChannelWidth20MHz: 20,
    CoreWLAN.kCWChannelWidth40MHz: 40,
    CoreWLAN.kCWChannelWidth80MHz: 80,
    CoreWLAN.kCWChannelWidth160MHz: 160,
}
# 5GHz帯でチャネルボンディングの区切りとなる先頭チャネル(W52/W53, W56, W58)
BONDING_BASES_5GHZ = (149, 100, 36)

# 混雑度の評価(上限%, 表示, 評価レベル)。評価レベルは表示側で色に変換する
CONGESTION_RATINGS = ((30, "空き", "free"), (60, "やや混雑", "moderate"), (85, "混雑", "busy"))
HEAVY_RATING = ("非常に混雑", "heavy")

def rssi_weight(rssi):
    """電波が強いAPほど干渉・電波の取り合いが大きいとみなして0〜1の重みにする。"""
    return min(1.0, max(0.0, (rssi - RSSI_FLOOR) / (RSSI_CEIL - RSSI_FLOOR)))

def occupied_channels(band, primary, width_mhz):
    """APが実際に電波を出している20MHzチャネルと、その重なり具合(0〜1)を返す。"""
    if band == "2.4GHz":
        # CoreWLANでは40MHz時のセカンダリ方向が取れないため、一般的な割り当てで推定する
        centers = [primary]
        if width_mhz == 40:
            centers.append(primary + 4 if primary <= 7 else primary - 4)
        overlap = {}
        for center in centers:
            for offset in range(-OVERLAP_SPAN_24GHZ + 1, OVERLAP_SPAN_24GHZ):
                factor = 1 - abs(offset) / OVERLAP_SPAN_24GHZ
                channel = center + offset
                overlap[channel] = max(overlap.get(channel, 0), factor)
        return overlap

    # 5GHz/6GHzは帯域幅ごとに決まった区切りでまとめて使う(例: 80MHzなら36〜48)
    span = width_mhz // 20 * 4
    if band == "5GHz":
        base = next((b for b in BONDING_BASES_5GHZ if primary >= b), primary)
    else:
        base = 1  # 6GHzは1, 5, 9, ...と4ch間隔
    start = base + (primary - base) // span * span
    return {channel: 1.0 for channel in range(start, start + span, 4)}

def get_supported_channels(wifi):
    """このMacが使える20MHzチャネル一覧(国ごとの電波法の制限を反映済み)を帯域別に返す。"""
    channels = {}
    for channel in wifi.supportedWLANChannels() or []:
        band = CHANNEL_BAND_NAMES.get(channel.channelBand())
        if band:
            channels.setdefault(band, set()).add(channel.channelNumber())
    return {band: sorted(numbers) for band, numbers in channels.items()}

def scan_networks(wifi):
    """周辺のAPを(帯域, プライマリチャネル, 帯域幅MHz, RSSI)で返す。
    位置情報の許可が無いとSSID/BSSIDはNoneになるが、チャネルとRSSIは取得できる。"""
    networks, error = wifi.scanForNetworksWithName_includeHidden_error_(None, True, None)
    if error is not None or networks is None:
        return None
    results = []
    for network in networks:
        channel = network.wlanChannel()
        band = CHANNEL_BAND_NAMES.get(channel.channelBand()) if channel else None
        width_mhz = CHANNEL_WIDTHS_MHZ.get(channel.channelWidth()) if channel else None
        if band and width_mhz:
            results.append((band, channel.channelNumber(), width_mhz, network.rssiValue()))
    return results

def compute_congestion(networks):
    """チャネルごとの混雑スコア(重みの合計)と、そのチャネルに電波が掛かっているAP数を返す。"""
    scores, counts = {}, {}
    for band, primary, width_mhz, rssi in networks:
        weight = rssi_weight(rssi)
        for channel, overlap in occupied_channels(band, primary, width_mhz).items():
            key = (band, channel)
            scores[key] = scores.get(key, 0.0) + weight * overlap
            counts[key] = counts.get(key, 0) + 1
    return scores, counts

def congestion_rating(percent):
    for limit, label, level in CONGESTION_RATINGS:
        if percent < limit:
            return label, level
    return HEAVY_RATING

class CongestionScanner:
    """メニューを開いている間(activate〜deactivate)だけ、バックグラウンドで一定間隔にスキャンする。
    スキャンは数秒かかるので、表示側は直近の結果(snapshot)を使う。"""

    def __init__(self):
        self.wifi = CWWiFiClient.sharedWiFiClient().interface()
        self.supported = get_supported_channels(self.wifi) if self.wifi else {}
        self.history = deque(maxlen=SMOOTHING_SCANS)
        self.last_scan = None
        self.scanning = False
        self.lock = threading.Lock()
        self.active = threading.Event()
        if self.wifi is not None:
            threading.Thread(target=self._run, daemon=True).start()

    def activate(self):
        self.active.set()

    def deactivate(self):
        self.active.clear()

    def _run(self):
        while True:
            self.active.wait()
            self.scanning = True
            networks = scan_networks(self.wifi)
            self.scanning = False
            if networks is not None:
                with self.lock:
                    self.history.append(networks)
                    self.last_scan = time.time()
            time.sleep(SCAN_INTERVAL)

    def snapshot(self):
        """全帯域のチャネル別混雑度と、接続中のチャネルの情報をまとめて返す(Wi-Fiが無ければNone)。"""
        if self.wifi is None or not self.supported:
            return None
        channel = self.wifi.wlanChannel()
        band = CHANNEL_BAND_NAMES.get(channel.channelBand()) if channel else None
        width_mhz = CHANNEL_WIDTHS_MHZ.get(channel.channelWidth()) if channel else None
        current, primary = set(), None
        if band and width_mhz:
            primary = channel.channelNumber()
            # 2.4GHzは重なりの裾まで含めず、実際の中心チャネルだけを接続中とする
            current = {c for c, overlap in occupied_channels(band, primary, width_mhz).items() if overlap == 1.0}

        with self.lock:
            history = list(self.history)
            last_scan = self.last_scan
        result = {
            "current_band": band,
            "primary": primary,
            "width_mhz": width_mhz,
            "current": current,
            "bands": None,
            "scanning": self.scanning,
            "age": time.time() - last_scan if last_scan else None,
        }
        if not history:
            return result

        scores = {}
        for networks in history:
            for key, value in compute_congestion(networks)[0].items():
                scores[key] = scores.get(key, 0.0) + value / len(history)
        bands = []
        for name in ("2.4GHz", "5GHz", "6GHz"):
            if name not in self.supported:
                continue
            channels = [
                (number, min(100.0, scores.get((name, number), 0.0) / SATURATION_SCORE * 100))
                for number in self.supported[name]
            ]
            # 同じ混雑度なら番号の小さいチャネルを優先して提示する
            best = min(channels, key=lambda item: (item[1], item[0]))
            # チャネルごとの電波が掛かっているAP数(直近のスキャン。2.4GHzは部分的な重なりも含む)
            ap_counts = {
                number: sum(1 for ap_band, ap_primary, ap_width, _rssi in history[-1]
                            if ap_band == name and number in occupied_channels(ap_band, ap_primary, ap_width))
                for number in self.supported[name]
            }
            bands.append({"band": name, "channels": channels, "best": best, "ap_counts": ap_counts})
        result["bands"] = bands
        result["ap_count"] = len(history[-1])
        if band:
            percents = dict(next(b["channels"] for b in bands if b["band"] == band))
            # 接続中のチャネル群のうち、最も混んでいるチャネルの値をその接続の混雑度とする
            result["current_percent"] = max((percents[c] for c in current if c in percents), default=0.0)
            # 接続中のチャネル群と電波が重なっているAPの数(直近のスキャン。接続先のAP自身も含む)
            result["current_ap_count"] = sum(
                1 for ap_band, ap_primary, ap_width, _rssi in history[-1]
                if ap_band == band and current & occupied_channels(ap_band, ap_primary, ap_width).keys()
            )
        return result
