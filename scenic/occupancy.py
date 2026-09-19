"""多源占用估计。

闸机计数、匿名位置区段、接驳容量与传感器心跳汇成带置信度的占用值。
数据迟到、传感器失联或来源互相矛盾时降低置信度——系统宁可标记不确定，
也不假装精确。占用值取各来源的保守上界（宁可高估，不可低估）。
"""

from dataclasses import asdict, dataclass, field

# 观测年龄 → 新鲜度系数
_FRESHNESS_STEPS = ((60.0, 1.0), (300.0, 0.85), (900.0, 0.6), (float("inf"), 0.35))

# 闸机推算与位置区段观测相差超过容量该比例即视为来源分歧
_DISAGREE_RATIO = 0.25


def _freshness(age):
    if age is None:
        return None
    age = max(0.0, age)
    for limit, factor in _FRESHNESS_STEPS:
        if age <= limit:
            return factor
    return _FRESHNESS_STEPS[-1][1]


@dataclass
class ElementState:
    in_count: float = 0.0
    out_count: float = 0.0
    location_count: float | None = None
    location_at: float | None = None
    turnstile_at: float | None = None
    heartbeat_at: float | None = None
    sensor_online: bool = True
    last_late_at: float | None = None
    capacity_override: float | None = None


@dataclass
class Estimate:
    element: str
    value: float
    capacity: float
    confidence: float
    notes: list = field(default_factory=list)

    @property
    def ratio(self):
        return self.value / self.capacity if self.capacity > 0 else 0.0

    @property
    def uncertain(self):
        return self.confidence < 0.7

    def to_dict(self):
        return {
            "element": self.element,
            "value": round(self.value, 1),
            "capacity": self.capacity,
            "ratio": round(self.ratio, 3),
            "confidence": self.confidence,
            "uncertain": self.uncertain,
            "notes": list(self.notes),
        }


class OccupancyEstimator:
    def __init__(self, topology, heartbeat_ttl=180.0, late_grace=120.0):
        self.topology = topology
        self.heartbeat_ttl = heartbeat_ttl
        self.late_grace = late_grace
        self.states = {}

    def state_of(self, element):
        return self.states.setdefault(element, ElementState())

    def ingest(self, event):
        """消费一个观测类事件，返回需要值班人员留意的说明。"""
        notes = []
        payload = event.payload
        element = payload.get("element")
        if element is None:
            return notes
        state = self.state_of(element)
        if event.is_late(self.late_grace):
            state.last_late_at = event.received_at
            delay = int(event.received_at - event.observed_at)
            notes.append(f"{element} 数据迟到 {delay}s，按观测时间回填并下调置信度")
        if event.type == "turnstile_count":
            state.in_count += float(payload.get("in", 0))
            state.out_count += float(payload.get("out", 0))
            state.turnstile_at = max(state.turnstile_at or 0.0, event.observed_at)
        elif event.type == "location_segment":
            if state.location_at is None or event.observed_at >= state.location_at:
                state.location_count = float(payload["count"])
                state.location_at = event.observed_at
        elif event.type == "shuttle_capacity":
            state.capacity_override = float(payload["capacity"])
        elif event.type == "sensor_heartbeat":
            state.heartbeat_at = max(state.heartbeat_at or 0.0, event.observed_at)
        elif event.type == "sensor_status":
            state.sensor_online = bool(payload.get("online", True))
        return notes

    def estimate(self, element, now):
        state = self.states.get(element)
        if state is None:
            state = ElementState()
        capacity = (
            state.capacity_override
            if state.capacity_override is not None
            else self.topology.capacity_of(element)
        )
        derived = max(0.0, state.in_count - state.out_count)
        notes = []

        value = derived
        disagreement = False
        if state.location_count is not None:
            value = max(derived, state.location_count)
            if capacity > 0 and abs(derived - state.location_count) > _DISAGREE_RATIO * capacity:
                disagreement = True
                notes.append("闸机推算与匿名区段观测分歧，取保守上界")

        turnstile_fresh = _freshness(None if state.turnstile_at is None else now - state.turnstile_at)
        location_fresh = _freshness(None if state.location_at is None else now - state.location_at)
        if turnstile_fresh is None and location_fresh is None:
            confidence = 0.2
            notes.append("无任何观测数据")
        else:
            confidence = 1.0
            if turnstile_fresh is None:
                confidence *= 0.7
                notes.append("缺少闸机计数")
            else:
                confidence *= turnstile_fresh
            if location_fresh is None:
                confidence *= 0.7
                notes.append("缺少位置区段观测")
            else:
                confidence *= location_fresh

        if not state.sensor_online:
            confidence *= 0.5
            notes.append("传感器失联")
        elif state.heartbeat_at is not None and now - state.heartbeat_at > self.heartbeat_ttl:
            confidence *= 0.5
            notes.append("传感器心跳超时")
        if disagreement:
            confidence *= 0.6
        if state.last_late_at is not None and now - state.last_late_at < 600:
            confidence *= 0.9
            notes.append("近期存在迟到数据")

        return Estimate(element, value, capacity, round(confidence, 3), notes)

    def all_estimates(self, now):
        return {eid: self.estimate(eid, now) for eid in self.topology.element_ids()}

    # --- 快照（供服务重启恢复） ---
    def to_dict(self):
        return {eid: asdict(state) for eid, state in self.states.items()}

    def load_dict(self, data):
        self.states = {eid: ElementState(**state) for eid, state in data.items()}
