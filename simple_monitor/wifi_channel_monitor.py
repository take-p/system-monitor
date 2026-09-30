#!/usr/bin/env python3
import argparse
import threading
import time
from collections import deque

import CoreWLAN
from CoreWLAN import CWWiFiClient
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

console = Console()

BAR_WIDTH = 20
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

# 混雑度の評価(上限%, 表示, 色)
CONGESTION_RATINGS = ((30, "空き", "green"), (60, "やや混雑", "yellow"),
                      (85, "混雑", "dark_orange"))
HEAVY_RATING = ("非常に混雑", "red")

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

class Scanner:
    """スキャンは数秒かかるのでバックグラウンドで繰り返し、直近の結果を保持する。"""

    def __init__(self, wifi, interval):
        self.wifi = wifi
        self.interval = interval
        self.history = deque(maxlen=SMOOTHING_SCANS)
        self.last_scan = None
        self.scanning = False
        self.lock = threading.Lock()

    def run(self):
        while True:
            self.scanning = True
            networks = scan_networks(self.wifi)
            self.scanning = False
            if networks is not None:
                with self.lock:
                    self.history.append(networks)
                    self.last_scan = time.time()
            time.sleep(self.interval)

    def snapshot(self):
        """平滑化したスコアとAP数、直近スキャンのAP総数を返す。"""
        with self.lock:
            history = list(self.history)
        if not history:
            return None
        scores, counts = {}, {}
        for networks in history:
            s, c = compute_congestion(networks)
            for key, value in s.items():
                scores[key] = scores.get(key, 0.0) + value / len(history)
            for key, value in c.items():
                counts[key] = counts.get(key, 0) + value / len(history)
        return scores, counts, len(history[-1])

def congestion_rating(percent):
    for limit, label, style in CONGESTION_RATINGS:
        if percent < limit:
            return label, style
    return HEAVY_RATING

def get_current_channel(wifi):
    """接続中のチャネルが占有している(帯域, チャネル)の集合と表示用文字列。"""
    channel = wifi.wlanChannel()
    band = CHANNEL_BAND_NAMES.get(channel.channelBand()) if channel else None
    width_mhz = CHANNEL_WIDTHS_MHZ.get(channel.channelWidth()) if channel else None
    if not band or not width_mhz:
        return set(), None
    primary = channel.channelNumber()
    occupied = occupied_channels(band, primary, width_mhz)
    # 2.4GHzは重なりの裾まで含めず、実際の中心チャネルだけを接続中とする
    keys = {(band, c) for c, overlap in occupied.items() if overlap == 1.0}
    return keys, f"{band} ch {primary} ({width_mhz}MHz)"

def build_band_panel(band, channels, scores, counts, current_keys):
    panel = Text()
    panel.append(f"{band}\n", style="bold underline")
    percents = {}
    for channel in channels:
        key = (band, channel)
        percent = min(100.0, scores.get(key, 0.0) / SATURATION_SCORE * 100)
        percents[channel] = percent
        label, style = congestion_rating(percent)
        filled = round(percent / 100 * BAR_WIDTH)
        panel.append(f"ch{channel:>4} ", style="bold" if key in current_keys else None)
        panel.append("█" * filled, style=style)
        panel.append("░" * (BAR_WIDTH - filled), style="grey23")
        panel.append(f" {percent:3.0f}%", style=style)
        panel.append(f" {counts.get(key, 0):3.0f} AP", style="grey50")
        if key in current_keys:
            panel.append(" ◀", style="bold cyan")
        panel.append("\n")
    if percents:
        # 同じ混雑度なら番号の小さいチャネルを優先して提示する
        best = min(percents, key=lambda c: (percents[c], c))
        panel.append("最も空いている: ", style="bold")
        panel.append(f"ch {best}", style="bold green")
        panel.append(f" ({percents[best]:.0f}%)\n")
    return panel

def build_display(scanner, supported, current_keys, current_label):
    header = Text()
    header.append("Wi-Fi Channel Congestion Monitor\n", style="bold cyan")
    header.append("接続中: ", style="bold")
    header.append(current_label or "未接続", style="cyan")
    header.append("   ◀ = 接続中のAPが使っているチャネル\n", style="grey50")

    snapshot = scanner.snapshot()
    if snapshot is None:
        header.append("\nスキャン中...\n", style="yellow")
        return header
    scores, counts, total = snapshot
    age = time.time() - scanner.last_scan
    header.append("検出AP数: ", style="bold")
    header.append(f"{total}")
    header.append(f"   最終スキャン: {age:.0f}秒前", style="grey50")
    if scanner.scanning:
        header.append(" (スキャン中)", style="yellow")
    header.append("\n")
    header.append(
        "混雑度 = 周辺APの電波強度(RSSI)×チャネル重なりの合計による推定値"
        f"(直近{SMOOTHING_SCANS}回平均)。実際の通信量ではありません\n",
        style="grey50",
    )

    panels = [
        build_band_panel(band, supported[band], scores, counts, current_keys)
        for band in ("2.4GHz", "5GHz", "6GHz") if band in supported
    ]
    return Group(header, Columns(panels, padding=(0, 4)))

parser = argparse.ArgumentParser(description="Wi-Fi channel congestion monitor")
parser.add_argument("--iface", help="スキャンに使うWi-Fiインターフェース(省略時は既定のWi-Fi)")
parser.add_argument("--interval", type=float, default=5.0, help="スキャン間隔(秒)")
args = parser.parse_args()

client = CWWiFiClient.sharedWiFiClient()
wifi = client.interfaceWithName_(args.iface) if args.iface else client.interface()
if wifi is None:
    console.print("[red]Wi-Fiインターフェースが見つかりません[/red]")
    raise SystemExit(1)

supported = get_supported_channels(wifi)
scanner = Scanner(wifi, args.interval)
threading.Thread(target=scanner.run, daemon=True).start()

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            current_keys, current_label = get_current_channel(wifi)
            live.update(build_display(scanner, supported, current_keys, current_label))
            time.sleep(1)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
