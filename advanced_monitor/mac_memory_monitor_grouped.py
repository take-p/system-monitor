#!/usr/bin/env python3
import ctypes
import ctypes.util
import psutil
import re
import subprocess
import time
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

console = Console()

MIN_FOOTPRINT_MB = 100
UPDATE_INTERVAL = 5
GAUGE_WIDTH = 40

CATEGORY_STYLES = {
    "app": "bright_blue",
    "wired": "red",
    "compressed": "yellow",
    "cached": "cyan",
    "free": "grey37",
}
CATEGORY_LABELS = {
    "app": "App (approx)",
    "wired": "Wired",
    "compressed": "Compressed",
    "cached": "Cached Files",
    "free": "Free/Other",
}

# --- libproc.proc_pid_rusage() 経由でActivity Monitorと同じ「メモリフットプリント」を取得する ---
# psutilはこの値(phys_footprint)を公開していないため、ctypesでlibprocを直接呼び出す。
# 構造体レイアウトはxnuのbsd/sys/resource.h `rusage_info_v4` に準拠。
RUSAGE_INFO_V4 = 4

class _rusage_info_v4(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time", ctypes.c_uint64),
        ("ri_serviced_system_time", ctypes.c_uint64),
        ("ri_logical_writes", ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint", ctypes.c_uint64),
        ("ri_instructions", ctypes.c_uint64),
        ("ri_cycles", ctypes.c_uint64),
        ("ri_billed_energy", ctypes.c_uint64),
        ("ri_serviced_energy", ctypes.c_uint64),
        ("ri_interval_max_phys_footprint", ctypes.c_uint64),
        ("ri_runnable_time", ctypes.c_uint64),
        ("ri_flags", ctypes.c_uint64),
        ("ri_user_ptime", ctypes.c_uint64),
        ("ri_system_ptime", ctypes.c_uint64),
        ("ri_pinstructions", ctypes.c_uint64),
        ("ri_pcycles", ctypes.c_uint64),
        ("ri_energy_nj", ctypes.c_uint64),
        ("ri_penergy_nj", ctypes.c_uint64),
        ("ri_reserved", ctypes.c_uint64 * 14),
    ]

_libproc = ctypes.CDLL(ctypes.util.find_library("libproc"), use_errno=True)
_libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libproc.proc_pid_rusage.restype = ctypes.c_int

def get_phys_footprint(pid):
    # 戻り値は(footprint_bytes, errno)。他ユーザー所有プロセス等はerrno!=0でfootprint_bytes=Noneになる
    info = _rusage_info_v4()
    ret = _libproc.proc_pid_rusage(pid, RUSAGE_INFO_V4, ctypes.byref(info))
    if ret != 0:
        return None, ctypes.get_errno()
    return info.ri_phys_footprint, 0

def get_vm_stat_counts():
    # psutilはfree_count/speculative_count/compressor_page_countを公開していないため、
    # vm_statを直接パースする
    output = subprocess.run(["vm_stat"], capture_output=True, text=True, check=True).stdout
    page_size = int(re.search(r"page size of (\d+) bytes", output).group(1))

    def pages(label):
        return int(re.search(rf"{label}:\s+(\d+)", output).group(1))

    return {
        "page_size": page_size,
        "free": pages("Pages free"),
        "speculative": pages("Pages speculative"),
        "external": pages("File-backed pages"),
        "anonymous": pages("Anonymous pages"),
        "purgeable": pages("Pages purgeable"),
        "compressor": pages(r"Pages occupied by compressor"),
    }

def compute_breakdown(mem, vm_counts):
    total = mem.total
    page_size = vm_counts["page_size"]
    compressed_bytes = vm_counts["compressor"] * page_size
    # File-backed pages(ファイルにマップされた再利用可能ページ)はActivity Monitorが
    # 「キャッシュされたファイル」として使用済みメモリ/アプリメモリから分離して表示する分類。
    # これをapp_bytesに含めたままだと実測でApp側が実際より約5GB(ファイルキャッシュ分)過大に出る
    cached_bytes = vm_counts["external"] * page_size

    # Activity Monitorの「アプリメモリ」の定義に相当する直接式:
    #   app = (anonymous_pages(internal) - purgeable_pages) * page_size
    # 以前のused(top式)からの引き算による間接計算より、AM実測に対する誤差が小さい
    app_bytes = max(vm_counts["anonymous"] - vm_counts["purgeable"], 0) * page_size
    free_bytes = max(total - (app_bytes + mem.wired + compressed_bytes + cached_bytes), 0)
    return {
        "app": app_bytes,
        "wired": mem.wired,
        "compressed": compressed_bytes,
        "cached": cached_bytes,
        "free": free_bytes,
        # Activity Monitorの「使用済みメモリ」に相当(キャッシュされたファイルは含まない)
        "used": app_bytes + mem.wired + compressed_bytes,
    }

def build_capacity_gauge(mem, breakdown):
    total = mem.total
    segments = [
        ("app", breakdown["app"]),
        ("wired", breakdown["wired"]),
        ("compressed", breakdown["compressed"]),
        ("cached", breakdown["cached"]),
        ("free", breakdown["free"]),
    ]

    text = Text()
    text.append("Memory Capacity Breakdown\n", style="bold cyan")
    text.append("[")
    filled_so_far = 0
    for i, (key, nbytes) in enumerate(segments):
        if i == len(segments) - 1:
            # 丸め誤差を最後のセグメントに寄せて、合計をGAUGE_WIDTHに厳密に一致させる
            width = GAUGE_WIDTH - filled_so_far
        else:
            ratio = nbytes / total if total else 0
            width = min(int(round(ratio * GAUGE_WIDTH)), GAUGE_WIDTH - filled_so_far)
        filled_so_far += width
        text.append("█" * width, style=CATEGORY_STYLES[key])
    text.append("]\n")

    for key, nbytes in segments:
        gb = nbytes / (1024 ** 3)
        pct = nbytes / total * 100 if total else 0
        text.append("■ ", style=CATEGORY_STYLES[key])
        text.append(f"{CATEGORY_LABELS[key]} {gb:5.1f} GB ({pct:4.1f}%)   ")
    text.append("\n")
    return text

def usage_style(footprint_mb):
    if footprint_mb >= 10 * 1024:
        return "red"
    elif footprint_mb >= 1024:
        return "yellow"
    else:
        return "green"

def snapshot_processes():
    procs = {}
    for p in psutil.process_iter(["pid", "ppid", "name"]):
        footprint, errno = get_phys_footprint(p.info["pid"])
        if footprint is None:
            # 他ユーザー所有プロセス(rootのloginなど)はEPERM等で取得できないが、
            # ppid/nameはprocess_iterで取得済みなのでfootprint=0としてツリーに残し、親子関係の連結を保つ
            footprint = 0
        procs[p.info["pid"]] = {
            "ppid": p.info["ppid"],
            "name": p.info["name"],
            "footprint": footprint,
        }
    return procs

def find_root(pid, procs, cache):
    if pid in cache:
        return cache[pid]
    info = procs.get(pid)
    ppid = info["ppid"] if info else 0
    # ppid<=1(launchd)や親プロセスが既に存在しない場合、自分自身をツリーの根とする
    if ppid <= 1 or ppid not in procs:
        cache[pid] = pid
        return pid
    root = find_root(ppid, procs, cache)
    cache[pid] = root
    return root

def collect_grouped(mem_total):
    procs = snapshot_processes()
    root_cache = {}
    groups = {}

    for pid, info in procs.items():
        root = find_root(pid, procs, root_cache)
        group = groups.setdefault(root, {"footprint": 0, "count": 0, "name": None})
        group["footprint"] += info["footprint"]
        group["count"] += 1
        if root == pid:
            group["name"] = info["name"]

    for root, group in groups.items():
        if group["name"] is None:
            group["name"] = procs.get(root, {}).get("name", f"pid:{root}")

    results = [
        (root, g["name"], g["count"], g["footprint"] / (1024 * 1024), g["footprint"] / mem_total * 100)
        for root, g in groups.items()
    ]
    results.sort(key=lambda r: r[3], reverse=True)
    return results

def build_table(all_groups, mem, breakdown):
    table = Table(
        title=f"Process Memory Monitor (grouped by process tree, phys_footprint, >= {MIN_FOOTPRINT_MB}MB)",
        expand=False,
    )
    table.add_column("PID", justify="right", width=9)
    table.add_column("Process", style="bold", width=28, no_wrap=True, overflow="ellipsis")
    table.add_column("Procs", justify="right", width=8)
    table.add_column("Mem%", justify="right", width=9)
    table.add_column("Footprint(MB)", justify="right", width=14)

    shown = [g for g in all_groups if g[3] >= MIN_FOOTPRINT_MB]
    hidden = [g for g in all_groups if g[3] < MIN_FOOTPRINT_MB]

    for root, name, count, footprint_mb, percent in shown:
        style = usage_style(footprint_mb)
        table.add_row(
            str(root),
            name[:28],
            str(count),
            Text(f"{percent:5.1f}%", style=style),
            Text(f"{footprint_mb:10.1f}", style=style),
        )

    if hidden:
        hidden_count = sum(g[2] for g in hidden)
        hidden_mb = sum(g[3] for g in hidden)
        hidden_percent = sum(g[4] for g in hidden)
        table.add_row(
            "",
            Text(f"その他 {len(hidden)}グループ", style="dim italic"),
            Text(str(hidden_count), style="dim"),
            Text(f"{hidden_percent:5.1f}%", style="dim"),
            Text(f"{hidden_mb:10.1f}", style="dim"),
        )

    used_gb = breakdown["used"] / (1024 ** 3)
    total_gb = mem.total / (1024 ** 3)
    percent = breakdown["used"] / mem.total * 100 if mem.total else 0
    table.caption = f"Overall Memory (App+Wired+Compressed, Activity Monitor式): {percent:.1f}% ({used_gb:.1f} GB / {total_gb:.1f} GB)"
    return table

try:
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            mem = psutil.virtual_memory()
            top_groups = collect_grouped(mem.total)
            vm_counts = get_vm_stat_counts()
            breakdown = compute_breakdown(mem, vm_counts)

            table = build_table(top_groups, mem, breakdown)
            gauge = build_capacity_gauge(mem, breakdown)
            live.update(Group(gauge, table))
            time.sleep(UPDATE_INTERVAL)

except KeyboardInterrupt:
    console.print("\n[yellow]Stopped[/yellow]")
