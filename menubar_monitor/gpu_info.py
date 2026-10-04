"""アプリ単位のGPU使用率の取得。

ioregのAGXDeviceUserClient(GPUを使うプロセスごとのクライアント)には、
起動からの累計GPU時間(accumulatedGPUTime、ナノ秒)がsudo不要で載っている。
前回との差分を経過時間で割り、Activity Monitorの「% GPU」と同じ考え方で使用率にする。
"""
import plistlib
import re
import subprocess
import time

import psutil

from memory_info import get_responsible_pid

CREATOR_PATTERN = re.compile(r"pid (\d+), (.*)")

def snapshot_gpu_times():
    """{pid: (名前, 累計GPU時間ns)} を返す。名前はIOUserClientCreator由来で16文字に切り詰められている"""
    output = subprocess.run(
        ["ioreg", "-r", "-a", "-d", "1", "-c", "AGXDeviceUserClient"],
        capture_output=True, check=True,
    ).stdout
    times = {}
    for node in plistlib.loads(output) if output.strip() else []:
        match = CREATOR_PATTERN.match(node.get("IOUserClientCreator", ""))
        if match is None:
            continue
        pid = int(match.group(1))
        gpu_time = sum(usage.get("accumulatedGPUTime", 0) for usage in node.get("AppUsage", []))
        # 1プロセスが複数のクライアントを持つことがあるので合算する
        name, total = times.get(pid, (match.group(2), 0))
        times[pid] = (name, total + gpu_time)
    return times

def process_name(pid, fallback):
    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return fallback

class GpuProcessSampler:
    """sample()を呼ぶたびに、前回からのアプリ単位のGPU使用率を返す"""

    def __init__(self):
        self.prev = None

    def reset(self):
        # 間隔が空いた後の差分は長時間の平均になってしまうので、基準を取り直させる
        self.prev = None

    def sample(self):
        """[(アプリ名, 使用率%)] を使用率の高い順に返す。初回(基準の取得のみ)はNone"""
        now, times = time.monotonic(), snapshot_gpu_times()
        prev, self.prev = self.prev, (now, times)
        if prev is None or now <= prev[0]:
            return None
        elapsed_ns = (now - prev[0]) * 1e9
        groups = {}
        for pid, (name, total) in times.items():
            # 前回いなかったプロセスは今回分を測れないので、次回から数える
            if pid not in prev[1]:
                continue
            delta = total - prev[1][pid][1]
            if delta <= 0:
                continue
            # XPCヘルパー(WebKitのGPUプロセス等)は責任元アプリにまとめる。メモリ欄のグループ化と同じ考え方
            root = get_responsible_pid(pid) or pid
            group = groups.setdefault(root, [None, 0.0])
            if root == pid:
                group[0] = name
            group[1] += delta / elapsed_ns * 100
        results = [
            (process_name(root, name or f"pid:{root}"), percent)
            for root, (name, percent) in groups.items()
        ]
        results.sort(key=lambda r: r[1], reverse=True)
        return results
