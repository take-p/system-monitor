#!/usr/bin/env python3
import psutil
import time
from collections import deque
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

console = Console()

TOP_N = 10
HISTORY_LEN = 20
UPDATE_INTERVAL = 5
SPARK_BLOCKS = "▁▂▃▄▅▆▇█"

history = {}

def usage_style(percent):
    if percent >= 80:
        return "red"
    elif percent >= 50:
        return "yellow"
    else:
        return "green"

def sparkline(values):
    if not values:
        return ""
    v_min, v_max = min(values), max(values)
    if v_max == v_min:
        # 変化がない期間は中間の高さで横一線にする（最大値でスケーリングして全部満タンに見えるのを防ぐ）
        return SPARK_BLOCKS[len(SPARK_BLOCKS) // 2] * len(values)
    return "".join(
        SPARK_BLOCKS[int((v - v_min) / (v_max - v_min) * (len(SPARK_BLOCKS) - 1))]
        for v in values
    )

def collect_top_processes():
    results = []
    current_pids = set()

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pid = proc.info["pid"]
            current_pids.add(pid)
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            percent = proc.memory_percent()
            results.append((pid, proc.info["name"], rss_mb, percent))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    for pid in list(history):
        if pid not in current_pids:
            del history[pid]

    results.sort(key=lambda r: r[2], reverse=True)
    top = results[:TOP_N]

    for pid, _, rss_mb, _ in top:
        history.setdefault(pid, deque(maxlen=HISTORY_LEN)).append(rss_mb)

    return top

def build_table(top_processes, mem):
    table = Table(title="Process Memory Monitor", expand=False)
    table.add_column("PID", justify="right", width=7)
    table.add_column("Process", style="bold", width=30, no_wrap=True, overflow="ellipsis")
    table.add_column("Mem%", justify="right", width=8)
    table.add_column("RSS(MB)", justify="right", width=10)
    table.add_column(f"History ({HISTORY_LEN * UPDATE_INTERVAL}s)", width=HISTORY_LEN)

    for pid, name, rss_mb, percent in top_processes:
        style = usage_style(percent)
        proc_history = history.get(pid, [])
        table.add_row(
            str(pid),
            name[:30],
            Text(f"{percent:5.1f}%", style=style),
            Text(f"{rss_mb:8.1f}", style=style),
            Text(sparkline(proc_history), style=style),
        )

    used_gb = mem.used / (1024 ** 3)
    total_gb = mem.total / (1024 ** 3)
    table.caption = f"Overall Memory: {mem.percent:.1f}% ({used_gb:.1f} GB / {total_gb:.1f} GB)"
    return table

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            mem = psutil.virtual_memory()
            top_processes = collect_top_processes()

            live.update(build_table(top_processes, mem))
            time.sleep(UPDATE_INTERVAL)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
