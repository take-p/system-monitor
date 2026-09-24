#!/usr/bin/env python3
import argparse
import json
import math
import re
import subprocess
import threading
import time
from collections import deque

import psutil
import CoreWLAN
from CoreWLAN import CWWiFiClient
from rich.console import Console
from rich.live import Live
from rich.text import Text

console = Console()

max_history = 60
GRAPH_HEIGHT = 10
MIN_SCALE_MBPS = 1.0
IFACE_CHECK_INTERVAL = 5
STREAM_CHECK_INTERVAL = 30
# スクリプト起動からの合計を集計するカウンタ(psutilのsnetioのフィールド名)
TOTAL_FIELDS = ("bytes_recv", "bytes_sent", "errin", "errout", "dropin", "dropout")

DL_STYLE = "green"
UL_STYLE = "magenta"
LINK_STYLE = "yellow"

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

# 電波品質の評価(下限値, 表示, 色)。規格で決まった値はなく、機器メーカーの設計ガイド等で一般的な目安
SIGNAL_RATINGS = ((-50, "非常に良い", "bright_green"), (-60, "良い", "green"),
                  (-67, "普通", "yellow"), (-80, "弱い", "dark_orange"))
SNR_RATINGS = ((40, "非常に良い", "bright_green"), (25, "良い", "green"),
               (15, "普通", "yellow"), (10, "弱い", "dark_orange"))
POOR_RATING = ("悪い", "red")

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
    for threshold, label, style in ratings:
        if value >= threshold:
            return label, style
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
    """リンク速度(Mbps)・理論最大速度の表記・種別・詳細情報の行[[(ラベル, 値, 評価)]]を返す。
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
        if standard:
            # 6GHz帯を使う802.11axはWi-Fi 6Eと呼ばれる
            generation = "Wi-Fi 6E" if standard == "802.11ax" and band == "6GHz" else WIFI_GENERATIONS.get(standard)
            spec_row.append(("Standard", f"{standard} ({generation})" if generation else standard, None))
        if channel:
            width = f"{width_mhz}MHz" if width_mhz else "?"
            spec_row.append(("Band", f"{band or '?'} / {width} (ch {channel.channelNumber()})", None))
        # 未接続時はRSSIが0になるので表示しない
        rssi, noise = wifi.rssiValue(), wifi.noiseMeasurement()
        if rssi < 0:
            quality_row.append(("Signal", f"{rssi} dBm", rate_quality(rssi, SIGNAL_RATINGS)))
            if noise < 0:
                # ノイズ単体には一般的な評価基準が無いので、値のみ表示する
                quality_row.append(("Noise", f"{noise} dBm", None))
                snr = rssi - noise
                quality_row.append(("SNR", f"{snr} dB", rate_quality(snr, SNR_RATINGS)))
        streams = spatial_streams.get((iface, channel.channelNumber(), width_mhz)) if channel else None
        max_rate = get_max_rate(standard, width_mhz, streams)
        if max_rate and standard not in LEGACY_MAX_RATE:
            max_text = (f"{max_rate:.0f} Mbps", f"(Max, {streams} stream{'s' if streams > 1 else ''})")
        elif max_rate:
            max_text = (f"{max_rate:.0f} Mbps", "(Max)")
        elif standard in MAX_RATE_PER_STREAM and streams is None:
            max_text = ("...", "(Max, checking)")
        else:
            max_text = None
        return (rate if rate > 0 else None), max_text, "Wi-Fi", [spec_row, quality_row]

    # 有線はifconfigのmedia行(例: "autoselect (1000baseT <full-duplex>)")から読み取る
    output = subprocess.run(["ifconfig", iface], capture_output=True, text=True).stdout
    match = re.search(r"media:.*\((\d+(?:\.\d+)?)(G?)base", output, re.IGNORECASE)
    if match:
        speed = float(match.group(1)) * (1000 if match.group(2).upper() == "G" else 1)
        # 有線はネゴシエーションで決まったリンク速度がそのまま上限になる
        return speed, None, "Ethernet", []
    return None, None, "Unknown", []

def nice_ceil(value):
    """Y軸の上限を1/2/5×10^nの切りの良い値に切り上げる。"""
    value = max(value, MIN_SCALE_MBPS)
    exponent = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        if value <= step * exponent:
            return step * exponent

def format_mbps(value):
    return f"{value:6.1f}" if value < 1000 else f"{value / 1000:5.2f}G"

def format_bytes(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"

def create_bar_graph(data, bar_style, width=max_history, height=GRAPH_HEIGHT):
    """Create bar graph using █ character."""
    graph = Text()
    if not data:
        graph.append("No data")
        return graph

    display_data = list(data)[-width:]

    # 表示中の最大値に合わせてY軸を自動伸縮する
    max_val = nice_ceil(max(display_data))

    # 履歴が満杯になるまでは左側を空白で埋める
    padding = width - len(display_data)

    for y_level in range(height, 0, -1):
        threshold = (y_level / height) * max_val
        graph.append(f"{format_mbps(threshold)} |")
        graph.append(" " * padding)

        for value in display_data:
            bar_height = (value / max_val) * height
            # 切り捨てだと最大値近くでも最上段が塗られないため、四捨五入相当にする
            if bar_height >= y_level - 0.5:
                graph.append("█", style=bar_style)
            else:
                graph.append("‾", style="grey23")
        graph.append("\n")

    graph.append("  Mbps +" + "─" * width + "\n")
    return graph

def build_display(iface, kind, details, link_speed, max_text, dl, ul, totals, peaks):
    output = Text()
    output.append("Network Monitor\n", style="bold cyan")

    output.append("Interface: ", style="bold")
    output.append(f"{iface or 'N/A'} ({kind})   ")
    output.append("Link: ", style="bold")
    link_text = f"{link_speed:.0f} Mbps" if link_speed else "N/A"
    output.append(link_text, style=f"bold {LINK_STYLE}")
    # Wi-Fiは「現在のリンク速度 / 理論最大速度」の分数のように並べる
    if max_text:
        max_value, max_note = max_text
        output.append(f" / {max_value} ", style=LINK_STYLE)
        output.append(max_note, style="dim")
    output.append("\n")

    # 規格・帯域などインターフェース種別ごとの詳細情報(無ければ行ごと省略)
    for row in details:
        if not row:
            continue
        for i, (label, value, rating) in enumerate(row):
            if i:
                output.append("   ")
            output.append(f"{label}: ", style="bold")
            output.append(value)
            if rating:
                rating_label, rating_style = rating
                output.append(f" ({rating_label})", style=rating_style)
        output.append("\n")

    output.append("Total since start: ", style="bold")
    output.append(f"↓ {format_bytes(totals['bytes_recv'])}", style=DL_STYLE)
    output.append("  ")
    output.append(f"↑ {format_bytes(totals['bytes_sent'])}", style=UL_STYLE)

    # 接続品質の目安。1件でもあれば赤で目立たせる
    for label, in_field, out_field in (("Errors", "errin", "errout"), ("Drops", "dropin", "dropout")):
        output.append(f"   {label}:", style="bold")
        for arrow, field in (("↓", in_field), ("↑", out_field)):
            count = totals[field]
            output.append(f" {arrow} {count}", style="bold red" if count else "grey50")
    output.append("\n")

    for label, value, peak, history, style in (
        ("Download", dl, peaks["dl"], dl_history, DL_STYLE),
        ("Upload", ul, peaks["ul"], ul_history, UL_STYLE),
    ):
        output.append("\n")
        output.append(f"{label}: ", style="bold")
        output.append(f"{value:.2f} Mbps", style=f"bold {style}")
        # 起動からの最高速度(グラフの60秒より前の値も含む)
        output.append("   Peak: ", style="bold")
        output.append(f"{peak:.2f} Mbps\n", style=style)
        output.append(create_bar_graph(history, style))
    return output

parser = argparse.ArgumentParser(description="Network throughput monitor")
parser.add_argument("--iface", help="監視するインターフェース(省略時はデフォルト経路を自動検出)")
args = parser.parse_args()

# system_profilerはWi-Fi以外の環境でも無害なので常に起動しておく
threading.Thread(target=update_spatial_streams, daemon=True).start()

dl_history = deque(maxlen=max_history)
ul_history = deque(maxlen=max_history)

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        iface = None
        prev = None
        totals = dict.fromkeys(TOTAL_FIELDS, 0)
        peaks = {"dl": 0.0, "ul": 0.0}
        last_iface_check = 0.0

        while True:
            now = time.monotonic()
            # 有線接続やVPNでデフォルト経路が切り替わることがあるので定期的に追従する
            if args.iface is None and now - last_iface_check >= IFACE_CHECK_INTERVAL:
                last_iface_check = now
                new_iface = get_default_interface()
                if new_iface != iface:
                    iface, prev = new_iface, None
            elif args.iface is not None:
                iface = args.iface

            counters = psutil.net_io_counters(pernic=True).get(iface)
            link_speed, max_text, kind, details = get_link_info(iface) if iface else (None, None, "Unknown", [])

            if counters is not None:
                if prev is not None:
                    prev_time, prev_counters = prev
                    elapsed = now - prev_time
                    deltas = {
                        field: getattr(counters, field) - getattr(prev_counters, field)
                        for field in TOTAL_FIELDS
                    }
                    # カウンタのリセット(インターフェース再接続など)で負になった場合は捨てる
                    if all(delta >= 0 for delta in deltas.values()) and elapsed > 0:
                        for field, delta in deltas.items():
                            totals[field] += delta
                        dl_history.append(deltas["bytes_recv"] * 8 / elapsed / 1_000_000)
                        ul_history.append(deltas["bytes_sent"] * 8 / elapsed / 1_000_000)
                prev = (now, counters)

            dl = dl_history[-1] if dl_history else 0.0
            ul = ul_history[-1] if ul_history else 0.0
            peaks["dl"] = max(peaks["dl"], dl)
            peaks["ul"] = max(peaks["ul"], ul)
            live.update(build_display(iface, kind, details, link_speed, max_text, dl, ul, totals, peaks))
            time.sleep(1)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
