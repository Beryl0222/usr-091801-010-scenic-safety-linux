"""领域时钟。

全项目时间单位统一为“当日分钟”（0-1439 的整数），不引入时区与 epoch，
便于拓扑关闭窗口（如 21:00-21:40）与事件时间直接比较。
"""

from dataclasses import dataclass


@dataclass
class Clock:
    """只前进的仿真时钟；事件的发生时间可以早于当前时间（迟到数据）。"""

    now_minute: int = 0

    def now(self) -> int:
        return self.now_minute

    def advance_to(self, minute: int) -> int:
        """按观测（入库）时间推进时钟，返回推进的分钟数。"""
        delta = max(0, minute - self.now_minute)
        self.now_minute += delta
        return delta

    def is_expired(self, expires_at: int | None) -> bool:
        return expires_at is not None and self.now_minute >= expires_at


def format_minute(value: int | None) -> str | None:
    """600 -> '10:00'，供接口阅读；超过当日按 24 小时制回绕。"""
    if value is None:
        return None
    value = int(value) % 1440
    return f"{value // 60:02d}:{value % 60:02d}"
