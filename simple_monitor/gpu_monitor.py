#!/usr/bin/env python3
import re
import subprocess
import time
from collections import deque
from rich.console import Console
from rich.live import Live
from rich.text import Text

console = Console()

max_history = 30
gpu_history = deque(maxlen=max_history)

def get_gpu_utilization():
    # ioregのIOAccelerator配下にあるPerformanceStatisticsはsudo不要で取得できる
    output = subprocess.run(
        ["ioreg", "-r", "-c", "IOAccelerator", "-d", "1"],
        capture_output=True, text=True, check=True,
    ).stdout
    match = re.search(r'"Device Utilization %"\s*=\s*(\d+)', output)
    return float(match.group(1)) if match else 0.0

def create_bar_graph(data, width=30, height=10):
    """Create simple bar graph using █ character."""
    if not data:
        return "No data"

    max_val = 100
    display_data = list(data)[-width:]
    lines = []

    # Y軸ラベル付きでグラフを描画
    for y_level in range(height, 0, -1):
        threshold = (y_level / height) * max_val

        # Y軸ラベル（毎行表示）
        line = f"{threshold:3.0f}% |"

        # 各棒を描画
        for value in display_data:
            bar_height = (value / max_val) * height
            # 切り捨てだと100%近い値でも最上段が塗られないため、四捨五入相当にする
            if bar_height >= y_level - 0.5:
                line += "█"
            else:
                line += "‾"

        lines.append(line)

    # X軸
    x_axis = "     +" + "─" * width
    lines.append(x_axis)

    return "\n".join(lines)

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            gpu_percent = get_gpu_utilization()
            gpu_history.append(gpu_percent)

            # グラフ作成
            graph = create_bar_graph(gpu_history, width=30, height=10)

            # 現在値表示
            output = Text()
            output.append("GPU Usage Monitor\n", style="bold medium_purple")
            # 色分けスタイル(直前の行の長さに関係なく、数値部分だけに直接スタイルを適用する)
            if gpu_percent >= 80:
                value_style = "bold red"
            elif gpu_percent >= 50:
                value_style = "bold yellow"
            else:
                value_style = "bold green"
            output.append("Current: ", style="bold")
            output.append(f"{gpu_percent:.1f}%\n", style=value_style)

            output.append("\n")
            output.append(graph, style="medium_purple")

            live.update(output)
            time.sleep(1)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
