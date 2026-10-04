"""アプリ単位のCPU使用率・ディスク読み書き速度の取得。

memory_info.snapshot_grouped_processes()のスナップショット(プロセスごとの累計CPU時間・累計読み書き量)を
前回と比べ、メモリのランキングと同じグループ単位で1秒あたりの量にする。
"""
import time

import psutil

from memory_info import group_name

class GroupRateSampler:
    """sample()を呼ぶたびに、スナップショットのfieldの前回からの増分を、グループ単位の1秒あたりの量で返す。
    divisorで割った値を返す(CPUなら「全コアで1秒」を100%にする値で割って使用率にする)"""

    def __init__(self, field, divisor=1.0):
        self.field = field
        self.divisor = divisor
        self.prev = None

    def reset(self):
        # 間隔が空いた後の差分は長時間の平均になってしまうので、基準を取り直させる
        self.prev = None

    def sample(self, snapshot):
        """snapshot: snapshot_grouped_processes()の結果。
        [(アプリ名, 1秒あたりの量)] を多い順に返す。初回(基準の取得のみ)はNone"""
        procs, roots = snapshot
        now = time.monotonic()
        totals = {pid: info[self.field] for pid, info in procs.items() if info[self.field] is not None}
        prev, self.prev = self.prev, (now, totals)
        if prev is None or now <= prev[0]:
            return None
        elapsed = now - prev[0]
        groups = {}
        for pid, total in totals.items():
            # 前回いなかったプロセス(pidの再利用を含む)は今回分を測れないので、次回から数える
            delta = total - prev[1].get(pid, total)
            if delta > 0:
                groups[roots[pid]] = groups.get(roots[pid], 0.0) + delta / elapsed / self.divisor
        results = [(group_name(root, procs), rate) for root, rate in groups.items()]
        results.sort(key=lambda r: r[1], reverse=True)
        return results

def cpu_sampler():
    # CPU全体(全コア)を100%とする。GPUのランキングやメニューの見出しと同じ基準で、
    # 合計が見出しの使用率とおおむね一致する(Activity Monitorの「% CPU」は1コア=100%)
    cores = psutil.cpu_count(logical=True) or 1
    return GroupRateSampler("cpu_time", divisor=1e9 * cores / 100)

def disk_sampler():
    # 読み込みと書き込みの合計(バイト/秒)
    return GroupRateSampler("disk_io")
