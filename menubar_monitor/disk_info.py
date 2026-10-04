"""ディスク全体の読み書き速度とビジー率の取得(ネットワークのNetworkSamplerと同じ作り)。

ビジー率は、読み書きの処理にかかった累計時間(psutilのread_time/write_time、ms)の増分を経過時間で割ったもの。
SSDは多数の読み書きを並行して処理し、その時間が重なって数えられるため100%を超えることがある。
WindowsのタスクマネージャーやActivity Monitorの考え方に合わせ、100%で打ち切って「混み具合」の目安にする。
"""
import time

import psutil

class DiskIOSampler:
    """sample()を呼ぶたびに、前回からの読み書き速度(MB/s)・ビジー率(%)と、起動からのピーク・合計を返す"""

    def __init__(self):
        self.prev = None
        self.peaks = {"read": 0.0, "write": 0.0}
        self.totals = {"read": 0, "write": 0}

    def sample(self):
        now, counters = time.monotonic(), psutil.disk_io_counters()
        if counters is None:
            raise OSError("disk_io_counters unavailable")
        rates = {"read": 0.0, "write": 0.0}
        busy = 0.0
        if self.prev is not None:
            prev_time, prev = self.prev
            elapsed = now - prev_time
            deltas = {"read": counters.read_bytes - prev.read_bytes,
                      "write": counters.write_bytes - prev.write_bytes}
            # カウンタのリセット(ディスクの付け外しなど)で負になった場合は捨てる
            if elapsed > 0 and all(delta >= 0 for delta in deltas.values()):
                for key, delta in deltas.items():
                    self.totals[key] += delta
                    rates[key] = delta / elapsed / 1_000_000
                    self.peaks[key] = max(self.peaks[key], rates[key])
                busy_ms = (counters.read_time - prev.read_time) + (counters.write_time - prev.write_time)
                busy = min(max(busy_ms / (elapsed * 1000) * 100, 0.0), 100.0)
        self.prev = (now, counters)
        return {**rates, "busy": busy, "peaks": dict(self.peaks), "totals": dict(self.totals)}
