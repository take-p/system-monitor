#!/usr/bin/env python3
import psutil
import time
from collections import deque
from rich.console import Console
from rich.live import Live
from rich.text import Text

console = Console()

HISTORY = 30
NUM_CORES = psutil.cpu_count(logical=True)
histories = [deque(maxlen=HISTORY) for _ in range(NUM_CORES)]

def usage_style(percent):
    if percent >= 80:
        return "red"
    elif percent >= 50:
        return "yellow"
    else:
        return "blue"

def build_display(average):
    text = Text()
    text.append("CPU Heatmap\n", style="bold cyan")
    text.append("Average: ", style="bold")
    text.append(f"{average:.1f}%\n\n", style=f"bold {usage_style(average)}")

    for i, history in enumerate(histories):
        # 履歴が満杯になるまでは左側を空白セルで埋める
        padding = HISTORY - len(history)
        label = f"Core {i:<2}"
        text.append(f"{label} |")
        text.append(" " * padding)
        for percent in history:
            text.append("█", style=usage_style(percent))
        text.append("|\n")

    return text

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            per_core = psutil.cpu_percent(interval=0.1, percpu=True)
            for i, percent in enumerate(per_core):
                histories[i].append(percent)
            average = sum(per_core) / len(per_core)

            live.update(build_display(average))
            time.sleep(1)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
