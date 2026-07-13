#!/usr/bin/env python3
import psutil
import time
from rich.console import Console
from rich.live import Live
from rich.text import Text

console = Console()

BAR_WIDTH = 30

def usage_style(percent):
    if percent >= 80:
        return "red"
    elif percent >= 50:
        return "yellow"
    else:
        return "green"

def build_display(per_core, overall):
    text = Text()
    text.append("CPU Usage Monitor (Multi-Core)\n", style="bold cyan")
    text.append("─" * 40 + "\n", style="cyan")

    for i, percent in enumerate(per_core):
        filled = int((percent / 100) * BAR_WIDTH)
        empty = BAR_WIDTH - filled
        style = usage_style(percent)

        label = f"Core {i:<2}"
        text.append(f"{label} [")
        text.append("█" * filled, style=style)
        text.append("░" * empty, style="dim")
        text.append(f"] {percent:5.1f}%\n")

    text.append("─" * 40 + "\n", style="cyan")

    overall_filled = int((overall / 100) * BAR_WIDTH)
    overall_empty = BAR_WIDTH - overall_filled
    overall_style = usage_style(overall)

    text.append("Total  [")
    text.append("█" * overall_filled, style=f"bold {overall_style}")
    text.append("░" * overall_empty, style="dim")
    text.append(f"] {overall:5.1f}%\n", style="bold")

    return text

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            per_core = psutil.cpu_percent(interval=0.1, percpu=True)
            overall = sum(per_core) / len(per_core)

            live.update(build_display(per_core, overall))
            time.sleep(1)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
