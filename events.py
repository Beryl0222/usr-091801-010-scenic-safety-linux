"""乱序事件模型。

所有观测都带两个时间：
- occurred_at：事件在现场发生的时间（传感器时间戳）；
- observed_at：事件进入调度服务的时间（入库时间）。

迟到 = observed_at - occurred_at 超过 feed.late_after；
乱序 = 同一 feed 的 occurred_at 不随入库顺序单调。
估计器信任 occurred_at 的读数，但用“当前入库时钟 - 该 feed 最新读数时间”
判断新鲜度，因此迟到数据到达后可以恢复一个 feed 的置信度。
"""

from dataclasses import dataclass, field

# 闸机计数：entries/exits 为本读数覆盖时段内的进出增量（人）。
TYPE_GATE_COUNT = "gate_count"
# 匿名位置区段计数：ref 为 edge:<id> 或 node:<id>，count 为当前在段/在场人数。
TYPE_ZONE_COUNT = "zone_count"
# 设施运行状态：open/degraded/closed，degraded 时给 throughput_factor。
TYPE_FACILITY_STATUS = "facility_status"
# 接驳容量：available 为未来一个发班间隔可承载的座位数。
TYPE_SHUTTLE_CAPACITY = "shuttle_capacity"
# 气象告警 / 告警解除。
TYPE_WEATHER_ALERT = "weather_alert"
TYPE_WEATHER_CLEAR = "weather_clear"
# 运营临时管制的下发与双人解除。
TYPE_RESTRICTION = "restriction"
TYPE_RESTRICTION_RELEASE = "restriction_release"
# 服务重启标记：重启后状态由事件日志重建，不依赖内存残留。
TYPE_SERVICE_RESTART = "service_restart"

VALID_TYPES = {
    TYPE_GATE_COUNT,
    TYPE_ZONE_COUNT,
    TYPE_FACILITY_STATUS,
    TYPE_SHUTTLE_CAPACITY,
    TYPE_WEATHER_ALERT,
    TYPE_WEATHER_CLEAR,
    TYPE_RESTRICTION,
    TYPE_RESTRICTION_RELEASE,
    TYPE_SERVICE_RESTART,
}


@dataclass
class Event:
    event_id: str
    type: str
    occurred_at: int
    observed_at: int
    payload: dict = field(default_factory=dict)
    source: str = ""

    @property
    def delay(self) -> int:
        return self.observed_at - self.occurred_at

    def is_late(self, late_after: int) -> bool:
        return self.delay > late_after


@dataclass(frozen=True)
class Feed:
    """一个传感器/数据馈源的新鲜度约定。"""

    feed_id: str
    type: str  # gate_count/zone_count/facility_status/shuttle_capacity
    ref: str  # gate 节点 id，或 edge:<id> / node:<id>
    late_after: int = 5  # 迟到阈值（分钟）
    stale_after: int = 12  # 失联阈值：超过该时长无更新则置为 stale

    @staticmethod
    def from_raw(raw: dict) -> "Feed":
        return Feed(
            feed_id=raw["id"],
            type=raw["type"],
            ref=raw["ref"],
            late_after=int(raw.get("late_after", 5)),
            stale_after=int(raw.get("stale_after", 12)),
        )


def parse_event(raw: dict, line_no: int = 0) -> Event:
    missing = [k for k in ("id", "type", "occurred_at", "observed_at") if k not in raw]
    if missing:
        raise ValueError(f"第 {line_no} 行事件缺少字段 {missing}")
    if raw["type"] not in VALID_TYPES:
        raise ValueError(f"第 {line_no} 行事件类型未知: {raw['type']}")
    if int(raw["occurred_at"]) > int(raw["observed_at"]):
        raise ValueError(f"第 {line_no} 行事件发生时间晚于入库时间: {raw['id']}")
    return Event(
        event_id=raw["id"],
        type=raw["type"],
        occurred_at=int(raw["occurred_at"]),
        observed_at=int(raw["observed_at"]),
        payload=raw.get("payload", {}),
        source=raw.get("source", ""),
    )
