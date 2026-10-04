"""ネットワーク情報の取得。

取得ロジックはsimple_monitor/network_monitor.pyから移植したもの
(network_monitor.pyはimport時に監視ループが走るため、直接importできない)。
"""
import json
import math
import re
import subprocess
import threading
import time

import CoreWLAN
import psutil
from CoreWLAN import CWWiFiClient

from wifi_congestion import occupied_channels

IFACE_CHECK_INTERVAL = 5
STREAM_CHECK_INTERVAL = 30
# 起動からの合計を集計するカウンタ(psutilのsnetioのフィールド名)
TOTAL_FIELDS = ("bytes_recv", "bytes_sent", "errin", "errout", "dropin", "dropout")

# 大文字始まりのkCWPHYMode11A等は旧定数で値がずれているため、小文字版を使う
PHY_MODE_NAMES = {
    CoreWLAN.kCWPHYMode11a: "802.11a",
    CoreWLAN.kCWPHYMode11b: "802.11b",
    CoreWLAN.kCWPHYMode11g: "802.11g",
    CoreWLAN.kCWPHYMode11n: "802.11n",
    CoreWLAN.kCWPHYMode11ac: "802.11ac",
    CoreWLAN.kCWPHYMode11ax: "802.11ax",
    CoreWLAN.kCWPHYMode11be: "802.11be",
}
WIFI_GENERATIONS = {
    "802.11n": "Wi-Fi 4",
    "802.11ac": "Wi-Fi 5",
    "802.11ax": "Wi-Fi 6",
    "802.11be": "Wi-Fi 7",
}
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

# 規格・チャネル幅ごとの1ストリームあたり理論最大速度(Mbps)と、その時の最大MCS。
# 最短ガードインターバル(11n/ac: 400ns, 11ax/be: 0.8us)での値。
MAX_RATE_PER_STREAM = {
    "802.11n": {20: (72.2, 7), 40: (150.0, 7)},
    "802.11ac": {20: (86.7, 8), 40: (200.0, 9), 80: (433.3, 9), 160: (866.7, 9)},
    "802.11ax": {20: (143.4, 11), 40: (286.8, 11), 80: (600.5, 11), 160: (1201.0, 11)},
    "802.11be": {20: (172.1, 13), 40: (344.1, 13), 80: (720.6, 13), 160: (1441.2, 13)},
}
# 空間ストリームを持たない旧規格の最大速度(Mbps)
LEGACY_MAX_RATE = {"802.11a": 54.0, "802.11b": 11.0, "802.11g": 54.0}
# MCS 0〜13の「変調ビット数×符号化率」。最大MCSとの比から任意MCSの速度を求める
MCS_EFFICIENCY = (0.5, 1, 1.5, 2, 3, 4, 4.5, 5, 6, 20 / 3, 7.5, 25 / 3, 9, 10)

# 電波品質の評価(下限値, 表示, 評価レベル)。規格で決まった値はなく、機器メーカーの設計ガイド等で一般的な目安。
# 評価レベルは表示側で色に変換する
SIGNAL_RATINGS = ((-50, "非常に良い", "excellent"), (-60, "良い", "good"),
                  (-67, "普通", "fair"), (-80, "弱い", "weak"))
SNR_RATINGS = ((40, "非常に良い", "excellent"), (25, "良い", "good"),
               (15, "普通", "fair"), (10, "弱い", "weak"))
POOR_RATING = ("悪い", "poor")

# CoreWLANの公開APIでは空間ストリーム数が取れないため、system_profilerの
# MCSと送信レートから逆算する。system_profilerは数秒かかるのでバックグラウンドで更新する。
# MCSとレートは同じ瞬間の値とは限らず1本と誤判定されることがあるため、
# 同じ接続(インターフェース・チャネル・幅)で観測した最大値を採用する。
spatial_streams = {}  # (インターフェース名, チャネル番号, 幅MHz) -> ストリーム数

def get_default_interface():
    # OSがインターネット向け通信に使うインターフェース(デフォルト経路)を取得する
    output = subprocess.run(
        ["route", "-n", "get", "default"], capture_output=True, text=True,
    ).stdout
    match = re.search(r"interface:\s*(\S+)", output)
    return match.group(1) if match else None

def estimate_spatial_streams(standard, width_mhz, mcs, rate):
    """送信レートを「そのMCSでの1ストリームあたり速度」で割ってストリーム数を求める。"""
    if standard == "802.11n" and mcs >= 8:
        # 11nのMCS番号はストリーム数を含む(0-7: 1本, 8-15: 2本, ...)
        return mcs // 8 + 1
    max_rate, max_mcs = MAX_RATE_PER_STREAM.get(standard, {}).get(width_mhz, (None, None))
    if max_rate is None or not 0 <= mcs <= max_mcs:
        return None
    per_stream = max_rate * MCS_EFFICIENCY[mcs] / MCS_EFFICIENCY[max_mcs]
    # 長いガードインターバルでは速度が最大15%程度下がるが、四捨五入で吸収できる
    return max(1, round(rate / per_stream))

def min_spatial_streams(standard, width_mhz, rate):
    """今の送信レートを出すのに最低限必要なストリーム数。1本の理論最大速度を超えていれば2本以上と分かる"""
    max_rate, _ = MAX_RATE_PER_STREAM.get(standard, {}).get(width_mhz, (None, None))
    if max_rate is None or rate <= 0:
        return None
    # 表の値は小数1桁に丸めてあるので、丸め誤差で1本多く数えないよう少し余裕を持たせる
    return max(1, math.ceil(rate / max_rate - 0.01))

def update_spatial_streams():
    while True:
        try:
            output = subprocess.run(
                ["system_profiler", "SPAirPortDataType", "-json"],
                capture_output=True, text=True, check=True,
            ).stdout
            for itf in json.loads(output)["SPAirPortDataType"][0]["spairport_airport_interfaces"]:
                info = itf.get("spairport_current_network_information") or {}
                # 例: "44 (5GHz, 20MHz)"
                channel = re.match(r"(\d+) \(.*?(\d+)MHz", info.get("spairport_network_channel", ""))
                if channel and "spairport_network_mcs" in info and "spairport_network_rate" in info:
                    width_mhz = int(channel.group(2))
                    streams = estimate_spatial_streams(
                        info.get("spairport_network_phymode"), width_mhz,
                        info["spairport_network_mcs"], info["spairport_network_rate"],
                    )
                    key = (itf["_name"], int(channel.group(1)), width_mhz)
                    if streams:
                        spatial_streams[key] = max(streams, spatial_streams.get(key, 0))
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError):
            pass
        time.sleep(STREAM_CHECK_INTERVAL)

def rate_quality(value, ratings):
    for threshold, label, level in ratings:
        if value >= threshold:
            return label, level
    return POOR_RATING

def get_max_rate(standard, width_mhz, streams):
    """現在の規格・チャネル幅・ストリーム数での理論最大速度(Mbps)。"""
    if standard in LEGACY_MAX_RATE:
        return LEGACY_MAX_RATE[standard]
    max_rate, _ = MAX_RATE_PER_STREAM.get(standard, {}).get(width_mhz, (None, None))
    if max_rate is None or streams is None:
        return None
    return max_rate * streams

def get_link_info(iface):
    """リンク速度(Mbps)・理論最大速度の表記・種別・詳細情報の行[[(ラベル, 値, 評価)]]・
    理論最大速度(Mbps, 不明ならNone)を返す。
    macOSではpsutilのspeedが0になるため個別に取得する。"""
    # Wi-FiはCoreWLANで現在の送信レート・規格・チャネルを取得(刻々と変化する)
    wifi = CWWiFiClient.sharedWiFiClient().interfaceWithName_(iface)
    if wifi is not None:
        rate = wifi.transmitRate()
        spec_row, quality_row = [], []
        standard = PHY_MODE_NAMES.get(wifi.activePHYMode())
        channel = wifi.wlanChannel()
        band = CHANNEL_BAND_NAMES.get(channel.channelBand()) if channel else None
        width_mhz = CHANNEL_WIDTHS_MHZ.get(channel.channelWidth()) if channel else None
        # 6GHz帯を使う802.11axはWi-Fi 6Eと呼ばれる
        generation = "Wi-Fi 6E" if standard == "802.11ax" and band == "6GHz" else WIFI_GENERATIONS.get(standard)
        width_text = channels_text = None
        if channel:
            width_text = f"{width_mhz}MHz" if width_mhz else "?"
            number = channel.channelNumber()
            channels_text = f"ch {number}"
            if band and width_mhz and width_mhz > 20:
                # 複数チャネルを束ねて使っている場合(チャネルボンディング)は、実際に使っている範囲で示す。
                # 2.4GHzは重なりの裾まで含めず、中心チャネルだけを使用中とする
                used = sorted(c for c, overlap in occupied_channels(band, number, width_mhz).items()
                              if overlap == 1.0)
                if len(used) > 1:
                    channels_text = f"ch {used[0]}–{used[-1]}"
        if standard or channel:
            # 「Wi-Fi 5 (802.11ac, 5GHz) / 80MHz (ch 52–64)」の形にまとめる(インターフェース名はメニュー側で足す)
            detail = ", ".join(x for x in (standard, band) if x)
            text = (generation or "Wi-Fi") + (f" ({detail})" if detail else "")
            if width_text:
                text += f" / {width_text} ({channels_text})"
            spec_row.append(("Connection", text, None))
        # 未接続時はRSSIが0になるので表示しない
        rssi, noise = wifi.rssiValue(), wifi.noiseMeasurement()
        if rssi < 0:
            quality_row.append(("Signal", f"{rssi} dBm", rate_quality(rssi, SIGNAL_RATINGS)))
            if noise < 0:
                # ノイズ単体には一般的な評価基準が無いので、値のみ表示する
                quality_row.append(("Noise", f"{noise} dBm", None))
                snr = rssi - noise
                quality_row.append(("SNR", f"{snr} dB", rate_quality(snr, SNR_RATINGS)))
        streams = None
        if channel:
            key = (iface, channel.channelNumber(), width_mhz)
            # system_profilerからの推定はMCSとレートの時点がずれて少なく出ることがあるので、
            # 今の送信レートから分かる下限で補う(リンク速度がMaxを超える矛盾を防ぐ)。補った値も観測値として残す
            lower = min_spatial_streams(standard, width_mhz, rate)
            if lower and lower > spatial_streams.get(key, 0):
                spatial_streams[key] = lower
            streams = spatial_streams.get(key)
        max_rate = get_max_rate(standard, width_mhz, streams)
        if max_rate and standard not in LEGACY_MAX_RATE:
            max_text = (f"{max_rate:.0f} Mbps", f"(Max, {streams} stream{'s' if streams > 1 else ''})")
        elif max_rate:
            max_text = (f"{max_rate:.0f} Mbps", "(Max)")
        elif standard in MAX_RATE_PER_STREAM and streams is None:
            max_text = ("...", "(Max, checking)")
        else:
            max_text = None
        return (rate if rate > 0 else None), max_text, "Wi-Fi", [spec_row, quality_row], max_rate

    # 有線はifconfigのmedia行(例: "autoselect (1000baseT <full-duplex>)")から読み取る
    output = subprocess.run(["ifconfig", iface], capture_output=True, text=True).stdout
    match = re.search(r"media:.*\((\d+(?:\.\d+)?)(G?)base", output, re.IGNORECASE)
    if match:
        speed = float(match.group(1)) * (1000 if match.group(2).upper() == "G" else 1)
        # 有線はネゴシエーションで決まったリンク速度がそのまま上限になる
        return speed, None, "Ethernet", [], None
    return None, None, "Unknown", [], None

def format_bytes(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"

class NetworkSampler:
    """呼び出しごとに前回からの差分で速度(Mbps)を求め、起動からの合計・最高速度を集計する。"""

    def __init__(self):
        self.iface = None
        self.prev = None
        self.totals = dict.fromkeys(TOTAL_FIELDS, 0)
        self.peaks = {"dl": 0.0, "ul": 0.0}
        self.dl = 0.0
        self.ul = 0.0
        self.last_iface_check = 0.0
        # system_profilerはWi-Fi以外の環境でも無害なので常に起動しておく
        threading.Thread(target=update_spatial_streams, daemon=True).start()

    def sample(self):
        now = time.monotonic()
        # 有線接続やVPNでデフォルト経路が切り替わることがあるので定期的に追従する
        if now - self.last_iface_check >= IFACE_CHECK_INTERVAL:
            self.last_iface_check = now
            new_iface = get_default_interface()
            if new_iface != self.iface:
                self.iface, self.prev = new_iface, None
                self.dl = self.ul = 0.0

        counters = psutil.net_io_counters(pernic=True).get(self.iface) if self.iface else None
        if counters is not None:
            if self.prev is not None:
                prev_time, prev_counters = self.prev
                elapsed = now - prev_time
                deltas = {
                    field: getattr(counters, field) - getattr(prev_counters, field)
                    for field in TOTAL_FIELDS
                }
                # カウンタのリセット(インターフェース再接続など)で負になった場合は捨てる
                if all(delta >= 0 for delta in deltas.values()) and elapsed > 0:
                    for field, delta in deltas.items():
                        self.totals[field] += delta
                    self.dl = deltas["bytes_recv"] * 8 / elapsed / 1_000_000
                    self.ul = deltas["bytes_sent"] * 8 / elapsed / 1_000_000
                    self.peaks["dl"] = max(self.peaks["dl"], self.dl)
                    self.peaks["ul"] = max(self.peaks["ul"], self.ul)
            self.prev = (now, counters)

        if self.iface:
            link_speed, max_text, kind, details, max_rate = get_link_info(self.iface)
        else:
            link_speed, max_text, kind, details, max_rate = None, None, "Unknown", [], None
        return {
            "iface": self.iface,
            "kind": kind,
            "link_speed": link_speed,
            "max_text": max_text,
            "max_rate": max_rate,
            "details": details,
            "dl": self.dl,
            "ul": self.ul,
            "peaks": dict(self.peaks),
            "totals": dict(self.totals),
        }
