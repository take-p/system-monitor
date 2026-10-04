"""ディスク全体の読み書き速度の取得(ネットワークのNetworkSamplerと同じ作り)。"""
import time

import psutil

class DiskIOSampler:
    """sample()を呼ぶたびに、前回からの読み書き速度(MB/s)と、起動からのピーク・合計を返す"""

    def __init__(self):
        self.prev = None
        self.peaks = {"read": 0.0, "write": 0.0}
        self.totals = {"read": 0, "write": 0}

    def sample(self):
        now, counters = time.monotonic(), psutil.disk_io_counters()
        if counters is None:
            raise OSError("disk_io_counters unavailable")
        rates = {"read": 0.0, "write": 0.0}
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
        self.prev = (now, counters)
        return {**rates, "peaks": dict(self.peaks), "totals": dict(self.totals)}
