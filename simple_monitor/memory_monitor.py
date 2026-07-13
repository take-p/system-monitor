#!/usr/bin/env python3
import psutil
import time
from collections import deque
from rich.console import Console
from rich.live import Live
from rich.text import Text

console = Console()

max_history = 30
mem_history = deque(maxlen=max_history)

def create_bar_graph(data, width=30, height=10):
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

def value_style(value):
    if value >= 80:
        return "red"
    elif value >= 50:
        return "yellow"
    else:
        return "green"

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            mem_percent = psutil.virtual_memory().percent
            mem_history.append(mem_percent)

            mem_graph = create_bar_graph(mem_history, width=30, height=10)

            output = Text()
            output.append("Memory Monitor\n", style="bold magenta")

            output.append("Memory: ")
            output.append(f"{mem_percent:5.1f}%\n", style=f"bold {value_style(mem_percent)}")

            output.append("\n")
            output.append(mem_graph, style="magenta")

            live.update(output)
            time.sleep(1)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
