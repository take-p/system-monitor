#!/usr/bin/env python3
import argparse
import time
from collections import deque

import CoreWLAN
from CoreWLAN import CWWiFiClient
from rich.console import Console
from rich.live import Live
from rich.text import Text

console = Console()

max_history = 60
GRAPH_HEIGHT = 8
# グラフの縦軸範囲。ノイズは-90dBm台が平常で、電子レンジ等の干渉があると上がる
NOISE_RANGE_DBM = (-100, -60)
SNR_RANGE_DB = (0, 60)

NOISE_STYLE = "magenta"
SNR_STYLE = "green"

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

# network_monitor.pyと同じ評価の目安(下限値, 表示, 色)
SIGNAL_RATINGS = ((-50, "非常に良い", "bright_green"), (-60, "良い", "green"),
                  (-67, "普通", "yellow"), (-80, "弱い", "dark_orange"))
SNR_RATINGS = ((40, "非常に良い", "bright_green"), (25, "良い", "green"),
               (15, "普通", "yellow"), (10, "弱い", "dark_orange"))
POOR_RATING = ("悪い", "red")

def rate_quality(value, ratings):
    for threshold, label, style in ratings:
        if value >= threshold:
            return label, style
    return POOR_RATING

def get_channel_label(wifi):
    channel = wifi.wlanChannel()
    band = CHANNEL_BAND_NAMES.get(channel.channelBand()) if channel else None
    width_mhz = CHANNEL_WIDTHS_MHZ.get(channel.channelWidth()) if channel else None
    if not band or not width_mhz:
        return None
    return f"{band} ch {channel.channelNumber()} ({width_mhz}MHz)"

def create_range_graph(data, low, high, unit, bar_style, width=max_history, height=GRAPH_HEIGHT):
    """固定の縦軸範囲(low〜high)で█の棒グラフを描く。範囲外の値は端に張り付かせる。"""
    graph = Text()
    display_data = list(data)[-width:]
    # 履歴が満杯になるまでは左側を空白で埋める
    padding = width - len(display_data)
    for y_level in range(height, 0, -1):
        threshold = low + (high - low) * y_level / height
        graph.append(f"{threshold:5.0f} |")
        graph.append(" " * padding)
        for value in display_data:
            bar_height = (min(max(value, low), high) - low) / (high - low) * height
            # 切り捨てだと最大値近くでも最上段が塗られないため、四捨五入相当にする
            if bar_height >= y_level - 0.5:
                graph.append("█", style=bar_style)
            else:
                graph.append("‾", style="grey23")
        graph.append("\n")
    graph.append(f"{unit:>5} +" + "─" * width + "\n")
    return graph

def build_display(iface, channel_label, rssi, noise_history, snr_history):
    output = Text()
    output.append("Wi-Fi Signal Monitor\n", style="bold cyan")
    output.append("Interface: ", style="bold")
    output.append(f"{iface}   ")
    output.append("Channel: ", style="bold")
    output.append(channel_label or "未接続", style="cyan")
    output.append("\n")

    if not noise_history:
        output.append("\n未接続のためノイズ/SNRを取得できません\n", style="grey50")
        return output

    noise, snr = noise_history[-1], snr_history[-1]
    for i, (label, value, rating) in enumerate((
        ("Signal", f"{rssi} dBm", rate_quality(rssi, SIGNAL_RATINGS)),
        # ノイズ単体には一般的な評価基準が無いので、値のみ表示する
        ("Noise", f"{noise} dBm", None),
        ("SNR", f"{snr} dB", rate_quality(snr, SNR_RATINGS)),
    )):
        if i:
            output.append("   ")
        output.append(f"{label}: ", style="bold")
        output.append(value)
        if rating:
            rating_label, rating_style = rating
            output.append(f" ({rating_label})", style=rating_style)
    output.append("\n")
    output.append("ノイズは接続中チャネルでのみ測定できます。チャネルが変わると履歴をリセットします\n",
                  style="grey50")

    # ノイズは突発的な上昇(電子レンジ等)を、SNRは落ち込みを見たいので、それぞれの最悪値を添える
    for title, history, (low, high), unit, style, worst in (
        ("Noise", noise_history, NOISE_RANGE_DBM, "dBm", NOISE_STYLE, f"最大 {max(noise_history)} dBm"),
        ("SNR", snr_history, SNR_RANGE_DB, "dB", SNR_STYLE, f"最小 {min(snr_history)} dB"),
    ):
        output.append("\n")
        output.append(f"{title}: ", style="bold")
        output.append(f"{history[-1]} {unit}", style=f"bold {style}")
        output.append(f"   (直近{max_history}秒の{worst})\n", style="grey50")
        output.append(create_range_graph(history, low, high, unit, style))
    return output

parser = argparse.ArgumentParser(description="Wi-Fi noise / SNR monitor")
parser.add_argument("--iface", help="監視するWi-Fiインターフェース(省略時は既定のWi-Fi)")
args = parser.parse_args()

client = CWWiFiClient.sharedWiFiClient()
wifi = client.interfaceWithName_(args.iface) if args.iface else client.interface()
if wifi is None:
    console.print("[red]Wi-Fiインターフェースが見つかりません[/red]")
    raise SystemExit(1)

noise_history = deque(maxlen=max_history)
snr_history = deque(maxlen=max_history)

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        prev_label = None
        while True:
            channel_label = get_channel_label(wifi)
            # ノイズは接続中チャネルでしか測れないので、チャネルが変わったら履歴を捨てる
            if channel_label != prev_label:
                noise_history.clear()
                snr_history.clear()
                prev_label = channel_label
            # 未接続時はRSSI・ノイズが0になるので記録しない
            rssi, noise = wifi.rssiValue(), wifi.noiseMeasurement()
            if rssi < 0 and noise < 0:
                noise_history.append(noise)
                snr_history.append(rssi - noise)
            live.update(build_display(wifi.interfaceName(), channel_label, rssi, noise_history, snr_history))
            time.sleep(1)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
