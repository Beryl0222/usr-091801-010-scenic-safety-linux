"""带观测时间的运行事件；乱序与迟到是一等公民。"""

from dataclasses import dataclass

from .util import parse_time

# 载荷之外的信封字段
_ENVELOPE = {"type", "observed_at", "received_at", "seq"}


@dataclass
class Event:
    type: str
    observed_at: float  # 事件在现场发生的时间
    received_at: float  # 事件到达服务的时间
    payload: dict
    seq: int | None = None

    @classmethod
    def from_dict(cls, raw):
        observed = parse_time(raw.get("observed_at"))
        received = parse_time(raw.get("received_at"))
        if received is None:
            received = observed
        if observed is None:
            raise ValueError("事件缺少 observed_at")
        payload = {k: v for k, v in raw.items() if k not in _ENVELOPE}
        return cls(raw["type"], observed, received, payload, raw.get("seq"))

    def is_late(self, grace_seconds):
        """到达时间明显晚于观测时间即为迟到。"""
        return self.received_at - self.observed_at > grace_seconds
