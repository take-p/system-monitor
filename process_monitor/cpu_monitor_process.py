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
UPDATE_INTERVAL = 10
SPARK_BLOCKS = "▁▂▃▄▅▆▇█"

proc_cache = {}
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
    return "".join(
        SPARK_BLOCKS[min(int(v / 100 * (len(SPARK_BLOCKS) - 1)), len(SPARK_BLOCKS) - 1)]
        for v in values
    )

def refresh_process_cache():
    current_pids = set()
    for p in psutil.process_iter(["pid"]):
        pid = p.info["pid"]
        current_pids.add(pid)
        if pid not in proc_cache:
            try:
                proc = psutil.Process(pid)
                proc.cpu_percent(interval=None)
                proc_cache[pid] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    for pid in list(proc_cache):
        if pid not in current_pids:
            del proc_cache[pid]
            history.pop(pid, None)

def collect_top_processes():
    results = []
    for pid, proc in proc_cache.items():
        try:
            cpu = proc.cpu_percent(interval=None)
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        results.append((pid, name, cpu))

    results.sort(key=lambda r: r[2], reverse=True)
    top = results[:TOP_N]

    for pid, _, cpu in top:
        history.setdefault(pid, deque(maxlen=HISTORY_LEN)).append(cpu)

    return top

def build_table(top_processes, overall_cpu):
    table = Table(title="Process CPU Monitor", expand=False)
    table.add_column("PID", justify="right", width=7)
    table.add_column("Process", style="bold", width=30, no_wrap=True, overflow="ellipsis")
    table.add_column("CPU%", justify="right", width=7)
    table.add_column(f"History ({HISTORY_LEN * UPDATE_INTERVAL}s)", width=HISTORY_LEN)

    for pid, name, cpu in top_processes:
        style = usage_style(cpu)
        table.add_row(
            str(pid),
            name[:30],
            Text(f"{cpu:5.1f}%", style=style),
            Text(sparkline(history.get(pid, [])), style=style),
        )

    caption = f"Overall CPU: {overall_cpu:.1f}%"
    table.caption = caption
    return table

# 初回はcpu_percent(interval=None)が0.0を返すため、事前に一度計測しておく
refresh_process_cache()
collect_top_processes()
time.sleep(1)

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            overall_cpu = psutil.cpu_percent(interval=0.1)
            refresh_process_cache()
            top_processes = collect_top_processes()

            live.update(build_table(top_processes, overall_cpu))
            time.sleep(UPDATE_INTERVAL)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
