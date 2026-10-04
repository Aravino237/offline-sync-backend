import time
from typing import List, Tuple

Timestamp = Tuple[int, int, str]


class HLCClock:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.last_physical = 0
        self.counter = 0

    def now(self) -> Timestamp:
        physical = int(time.time() * 1000)
        if physical > self.last_physical:
            self.last_physical = physical
            self.counter = 0
        else:
            self.counter += 1
        return (self.last_physical, self.counter, self.device_id)


def ts_key(ts) -> Tuple[int, int, str]:
    return (int(ts[0]), int(ts[1]), str(ts[2]))


def ts_gt(a, b) -> bool:
    return ts_key(a) > ts_key(b)


def ts_eq(a, b) -> bool:
    return ts_key(a) == ts_key(b)


def ts_ge(a, b) -> bool:
    return ts_key(a) >= ts_key(b)


def max_ts(timestamps: List) -> Tuple[int, int, str]:
    return max((ts_key(t) for t in timestamps), default=(0, 0, ""))
