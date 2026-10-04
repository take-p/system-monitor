"""アプリ単位のネットワーク通信速度の取得。

macOS標準のnettopはsudo不要で、他ユーザー所有(rootなど)を含む全プロセスの送受信の累計バイト数を出せる。
前回との差分を経過時間で割って速度にし、memory_info.snapshot_grouped_processes()と同じグループにまとめる。
"""
import subprocess
import time

from memory_info import group_name

def snapshot_net_bytes():
    """{pid: (名前, 受信bytes, 送信bytes)}。nettopの名前は15文字で切れるので、表示にはなるべく使わない"""
    output = subprocess.run(
        ["nettop", "-P", "-L", "1", "-x", "-J", "bytes_in,bytes_out"],
        capture_output=True, text=True, check=True,
    ).stdout
    totals = {}
    for line in output.splitlines()[1:]:  # 1行目は列名
        # 「プロセス名.pid,受信,送信,」の形。プロセス名に「.」や「,」を含むことがあるので右から分ける
        name_pid, *values = line.rstrip(",").rsplit(",", 2)
        name, _, pid = name_pid.rpartition(".")
        try:
            totals[int(pid)] = (name, int(values[0]), int(values[1]))
        except (ValueError, IndexError):
            continue
    return totals

class NetProcessSampler:
    """sample()を呼ぶたびに、前回からのアプリ単位の上り/下りの速度(Mbps)を返す"""

    def __init__(self):
        self.prev = None

    def reset(self):
        # 間隔が空いた後の差分は長時間の平均になってしまうので、基準を取り直させる
        self.prev = None

    def sample(self, snapshot):
        """snapshot: snapshot_grouped_processes()の結果(グループ分けに使う)。
        [(アプリ名, 上り+下り, 上り, 下り)] を合計の多い順に返す。初回(基準の取得のみ)はNone"""
        procs, roots = snapshot
        now, totals = time.monotonic(), snapshot_net_bytes()
        prev, self.prev = self.prev, (now, totals)
        if prev is None or now <= prev[0]:
            return None
        elapsed = now - prev[0]
        groups = {}
        names = {}
        for pid, (name, received, sent) in totals.items():
            # 前回nettopに出ていなかったプロセスは、その間に通信を始めた(累計は0から)ものとして全量を数える
            _, prev_received, prev_sent = prev[1].get(pid, (name, 0, 0))
            down, up = received - prev_received, sent - prev_sent
            if down < 0 or up < 0 or down + up == 0:
                continue
            # スナップショットに無い(直後に終了した等)プロセスは単独のグループにし、nettopの名前を使う
            root = roots.get(pid, pid)
            if root not in procs:
                names[root] = name
            group = groups.setdefault(root, [0.0, 0.0])
            group[0] += up * 8 / elapsed / 1_000_000
            group[1] += down * 8 / elapsed / 1_000_000
        results = [(names.get(root) or group_name(root, procs), up + down, up, down)
                   for root, (up, down) in groups.items()]
        results.sort(key=lambda r: r[1], reverse=True)
        return results
