"""プロセスごと(アプリ単位にグループ化)のメモリ使用量と、メモリ全体の内訳の取得。

取得ロジックはadvanced_monitor/mac_memory_monitor_grouped.pyから移植したもの
(mac_memory_monitor_grouped.pyはimport時に監視ループが走るため、直接importできない)。
"""
import ctypes
import ctypes.util
import re
import subprocess

import psutil

MIN_FOOTPRINT_MB = 100

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

_libproc = ctypes.CDLL(ctypes.util.find_library("libproc"))
_libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libproc.proc_pid_rusage.restype = ctypes.c_int

# --- responsibility_get_pid_responsible_for_pid() でXPCサービスの「責任元(親)アプリ」を取得する ---
# com.apple.WebKit.WebContent等のXPCサービスはlaunchd(pid 1)の子として起動されるため、
# ppidを辿るだけでは起動元アプリ(Safari/Mail/Xcode等)に紐付けられない。
# このAPIはActivity Monitorが「対象アプリ」列を表示する際に使っているのと同じ仕組み。
_libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libsystem.responsibility_get_pid_responsible_for_pid.argtypes = [ctypes.c_int]
_libsystem.responsibility_get_pid_responsible_for_pid.restype = ctypes.c_int

def get_responsible_pid(pid):
    rpid = _libsystem.responsibility_get_pid_responsible_for_pid(pid)
    return rpid if rpid > 0 else None

# XPCヘルパープロセス(WebKit/Virtualization/写真変換サービス等)は、責任元アプリ
# (実際にそのヘルパーを起動させたアプリ)が判明する限り、常にそのアプリのグループへ統合する。
# システムデーモン等、責任元が自分自身または不明なプロセスはそのまま個別ルートとして扱う。
def remap_responsible_parents(procs):
    for pid, info in procs.items():
        rpid = get_responsible_pid(pid)
        if rpid is None or rpid == pid or rpid not in procs:
            continue
        info["ppid"] = rpid

# rusage_infoのCPU時間はナノ秒ではなくMachの時間単位(Apple Siliconでは1=約41.7ns)なので、
# mach_timebase_infoの比でナノ秒に直す
class _mach_timebase_info(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

_timebase = _mach_timebase_info()
_libsystem.mach_timebase_info(ctypes.byref(_timebase))
MACH_TIME_TO_NS = _timebase.numer / _timebase.denom

def get_rusage(pid):
    """(footprint_bytes, CPU時間ns, ディスク読み書き量bytes)を返す。他ユーザー所有プロセス等は取得できずNone"""
    info = _rusage_info_v4()
    if _libproc.proc_pid_rusage(pid, RUSAGE_INFO_V4, ctypes.byref(info)) != 0:
        return None
    return (info.ri_phys_footprint, (info.ri_user_time + info.ri_system_time) * MACH_TIME_TO_NS,
            info.ri_diskio_bytesread + info.ri_diskio_byteswritten)

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

def parse_ps_time(text):
    """psのTIME列(「mmm:ss.ss」、長いと「[dd-]hh:mm:ss」)を秒にする"""
    days, _, clock = text.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + int(days or 0) * 86400

def get_ps_cpu_times():
    """{pid: 累計CPU時間ns}。psは特権付きで動くので、他ユーザー所有プロセスの分も取れる"""
    output = subprocess.run(["ps", "-A", "-o", "pid=,time="], capture_output=True, text=True, check=True).stdout
    times = {}
    for line in output.splitlines():
        pid, _, cpu_time = line.strip().partition(" ")
        try:
            times[int(pid)] = parse_ps_time(cpu_time.strip()) * 1e9
        except ValueError:
            continue
    return times

def snapshot_processes():
    """全プロセスの{pid: {ppid, name, footprint, cpu_time, disk_io}}。メモリ・CPU・ディスクのランキングで共用する"""
    procs = {}
    for p in psutil.process_iter(["pid", "ppid", "name"]):
        usage = get_rusage(p.info["pid"])
        # 他ユーザー所有プロセス(rootのloginなど)はEPERM等で取得できないが、
        # ppid/nameはprocess_iterで取得済みなので0としてツリーに残し、親子関係の連結を保つ。
        # cpu_time/disk_ioはNoneにして、速度の計算から外す(cpu_timeは下でpsの値で補う)
        footprint, cpu_time, disk_io = usage if usage is not None else (0, None, None)
        procs[p.info["pid"]] = {
            "ppid": p.info["ppid"],
            "name": p.info["name"],
            "footprint": footprint,
            "cpu_time": cpu_time,
            "disk_io": disk_io,
        }
    # WindowServer等の他ユーザー所有プロセスはCPU使用率の上位に来やすいので、psの値(10ms精度)で補う
    if any(info["cpu_time"] is None for info in procs.values()):
        try:
            ps_times = get_ps_cpu_times()
        except (OSError, subprocess.CalledProcessError):
            ps_times = {}
        for pid, info in procs.items():
            if info["cpu_time"] is None:
                info["cpu_time"] = ps_times.get(pid)
    return procs

def snapshot_grouped_processes():
    """XPCヘルパーを責任元アプリにつなぎ直した後のスナップショットと、{pid: グループの根のpid}を返す"""
    procs = snapshot_processes()
    remap_responsible_parents(procs)
    root_cache = {}
    roots = {pid: find_root(pid, procs, root_cache) for pid in procs}
    return procs, roots

def group_name(root, procs):
    return procs.get(root, {}).get("name") or f"pid:{root}"

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

def collect_grouped(mem_total, snapshot=None):
    """snapshot: snapshot_grouped_processes()の結果。省略時はその場で取得する"""
    procs, roots = snapshot or snapshot_grouped_processes()
    groups = {}

    for pid, info in procs.items():
        root = roots[pid]
        group = groups.setdefault(root, {"footprint": 0, "count": 0, "name": None})
        group["footprint"] += info["footprint"]
        group["count"] += 1

    for root, group in groups.items():
        group["name"] = group_name(root, procs)

    results = [
        (root, g["name"], g["count"], g["footprint"] / (1024 * 1024), g["footprint"] / mem_total * 100)
        for root, g in groups.items()
    ]
    results.sort(key=lambda r: r[3], reverse=True)
    return results
