"""调度核心：事件汇入、占用评估、限流决策与解释。

每条限流决策都必须说清：所用规则（rule）、受影响路径（affected_paths）、
触发依据（basis）与解除依据（release_basis）。置信度不足时自动改用更保守
的阈值，并把动作降级为需要人工确认，而不是假装精确地自动执行。
"""

import time

from .controls import ControlRegistry
from .events import Event
from .occupancy import OccupancyEstimator
from .routing import evacuation_plan, recommend_route
from .util import fmt_time, parse_time

DEFAULT_CONFIG = {
    "confidence_high": 0.7,  # 达到该置信度才允许自动限流
    "confidence_low": 0.4,   # 低于该值按最保守阈值
    "ratio_normal": 0.9,
    "ratio_cautious": 0.75,
    "ratio_conservative": 0.6,
    "danger_ratio": 0.95,    # 占用达到容量该比例即视为危险区段
    "heartbeat_ttl": 180.0,
    "late_grace": 120.0,
    "weather_closure_severity": "orange",  # 达到该等级的告警触发封区
}

SEVERITY_ORDER = {"blue": 1, "yellow": 2, "orange": 3, "red": 4}


def _control_view(control, now):
    return {
        "id": control.id,
        "initiator": control.initiator,
        "reason": control.reason,
        "elements": list(control.elements),
        "created_at": fmt_time(control.created_at),
        "expires_at": fmt_time(control.expires_at),
        "approvals": list(control.approvals),
        "released_at": fmt_time(control.released_at),
        "active": control.active(now),
    }


class DispatchService:
    def __init__(self, topology, config=None):
        self.topology = topology
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.estimator = OccupancyEstimator(
            topology, self.config["heartbeat_ttl"], self.config["late_grace"]
        )
        self.controls = ControlRegistry()
        self.facility_status = {fid: "up" for fid in topology.facilities}
        self.weather_alerts = []
        self.decisions = []
        self.evacuation = []
        self.event_count = 0
        self.now = None

    # --- 时间 ---
    def _resolve_now(self, now=None):
        if now is not None:
            return float(now)
        if self.now is not None:
            return self.now
        return time.time()

    # --- 事件汇入 ---
    def ingest(self, raw):
        event = raw if isinstance(raw, Event) else Event.from_dict(raw)
        self.event_count += 1
        self.now = max(self.now or 0.0, event.received_at)
        notes = []
        payload = event.payload
        if event.type == "facility_status":
            self.facility_status[payload["facility"]] = payload["status"]
            notes.append(f"设施 {payload['facility']} 状态变为 {payload['status']}")
        elif event.type == "weather_alert":
            self.weather_alerts.append(
                {
                    "alert_id": payload["alert_id"],
                    "zones": list(payload["zones"]),
                    "severity": payload["severity"],
                    "start": parse_time(payload["start"]),
                    "end": parse_time(payload["end"]),
                }
            )
            notes.append(f"气象告警 {payload['alert_id']}（{payload['severity']}）已登记")
        elif event.type == "control_impose":
            control = self.controls.impose(
                payload["control_id"],
                payload["initiator"],
                payload.get("reason", ""),
                list(payload["elements"]),
                event.received_at,
                parse_time(payload["expires_at"]),
            )
            notes.append(f"临时管制 {control.id} 生效，{fmt_time(control.expires_at)} 到期")
        elif event.type == "control_release":
            control = self.controls.release(
                payload["control_id"], payload["operator"], event.received_at
            )
            state = "已解除" if control.released_at is not None else "待第二人确认"
            notes.append(f"管制 {control.id} 收到 {payload['operator']} 解除确认：{state}")
        else:
            notes.extend(self.estimator.ingest(event))
        self.evaluate()
        return {"notes": notes, "decisions": self.decisions}

    # --- 规则评估 ---
    def _threshold_for(self, confidence):
        cfg = self.config
        if confidence >= cfg["confidence_high"]:
            return cfg["ratio_normal"], True, "常规阈值"
        if confidence >= cfg["confidence_low"]:
            return cfg["ratio_cautious"], False, "置信度不足，改用保守阈值"
        return cfg["ratio_conservative"], False, "置信度低，改用最保守阈值"

    def _active_alerts(self, now):
        floor = SEVERITY_ORDER[self.config["weather_closure_severity"]]
        return [
            alert
            for alert in self.weather_alerts
            if alert["start"] <= now < alert["end"]
            and SEVERITY_ORDER.get(alert["severity"], 0) >= floor
        ]

    def evaluate(self, now=None):
        now = self._resolve_now(now)
        cfg = self.config
        estimates = self.estimator.all_estimates(now)
        decisions = []

        # 规则一：局部占用超限（阈值随置信度保守化）
        for est in estimates.values():
            if est.capacity <= 0 or est.value <= 0:
                continue
            threshold, automatic, label = self._threshold_for(est.confidence)
            if est.ratio >= threshold:
                decisions.append(
                    {
                        "id": f"segment-occupancy:{est.element}",
                        "rule": "segment-occupancy",
                        "level": "rate_limit" if automatic else "manual_confirm",
                        "element": est.element,
                        "requires_confirmation": not automatic,
                        "occupancy": round(est.value, 1),
                        "capacity": est.capacity,
                        "confidence": est.confidence,
                        "threshold_ratio": threshold,
                        "affected_paths": [est.element],
                        "basis": (
                            f"占用率 {est.ratio:.0%} ≥ 阈值 {threshold:.0%}"
                            f"（{label}，置信度 {est.confidence:.2f}）"
                        ),
                        "release_basis": (
                            f"占用率回落至 {threshold * 0.8:.0%} 以下"
                            + ("" if automatic else "，且置信度恢复后由值班员确认")
                        ),
                        "notes": list(est.notes),
                        "at": fmt_time(now),
                    }
                )

        # 规则二：气象告警封区
        for alert in self._active_alerts(now):
            paths = self.topology.segments_in_zones(alert["zones"])
            decisions.append(
                {
                    "id": f"weather-zone-closure:{alert['alert_id']}",
                    "rule": "weather-zone-closure",
                    "level": "closure",
                    "requires_confirmation": False,
                    "affected_paths": paths,
                    "basis": (
                        f"{alert['severity']} 级告警 {alert['alert_id']} "
                        f"覆盖分区 {','.join(alert['zones'])}"
                    ),
                    "release_basis": f"告警解除或窗口结束于 {fmt_time(alert['end'])}",
                    "at": fmt_time(now),
                }
            )

        # 规则三：设施停运
        for fid, status in sorted(self.facility_status.items()):
            if status == "up":
                continue
            paths = list(self.topology.facilities[fid].segments)
            decisions.append(
                {
                    "id": f"facility-outage:{fid}",
                    "rule": "facility-outage",
                    "level": "closure",
                    "requires_confirmation": False,
                    "affected_paths": paths,
                    "basis": f"设施 {fid} 运行状态为 {status}",
                    "release_basis": "设施恢复运行（status=up）",
                    "at": fmt_time(now),
                }
            )

        # 规则四：运营临时管制
        for control in self.controls.active_controls(now):
            decisions.append(
                {
                    "id": f"operator-control:{control.id}",
                    "rule": "operator-control",
                    "level": "closure",
                    "requires_confirmation": False,
                    "control_id": control.id,
                    "affected_paths": list(control.elements),
                    "basis": f"{control.initiator} 发起：{control.reason}",
                    "release_basis": (
                        f"到期 {fmt_time(control.expires_at)} 自动失效，"
                        "或由两名独立操作员确认解除"
                    ),
                    "at": fmt_time(now),
                }
            )

        # 规则五：危险区段疏散优先
        danger = {
            est.element
            for est in estimates.values()
            if est.capacity > 0 and est.value > 0 and est.ratio >= cfg["danger_ratio"]
        }
        for alert in self._active_alerts(now):
            if SEVERITY_ORDER.get(alert["severity"], 0) >= SEVERITY_ORDER["red"]:
                danger.update(self.topology.segments_in_zones(alert["zones"]))
        if danger:
            closed_hard, _ = self.closed_segments(now, hard_only=True)
            self.evacuation = evacuation_plan(
                self.topology, estimates, closed_hard, sorted(danger), now
            )
        else:
            self.evacuation = []
        if self.evacuation:
            affected = sorted(
                {sid for plan in self.evacuation for sid in plan["route"]} | set(danger)
            )
            decisions.append(
                {
                    "id": "evacuation-priority",
                    "rule": "evacuation-priority",
                    "level": "evacuation",
                    "requires_confirmation": False,
                    "affected_paths": affected,
                    "basis": f"危险区段 {sorted(danger)} 内仍有游客，按占用降序优先生成疏散路径",
                    "release_basis": "危险区段占用清零且相关告警解除",
                    "plan": self.evacuation,
                    "at": fmt_time(now),
                }
            )

        self.decisions = decisions
        return decisions

    # --- 关闭集合（供路线推荐） ---
    def closed_segments(self, now=None, hard_only=False):
        now = self._resolve_now(now)
        reasons = {}
        for seg in self.topology.segments.values():
            if seg.closed_at(now):
                reasons[seg.id] = "closure_window"
        for fid, status in self.facility_status.items():
            if status != "up":
                for sid in self.topology.facilities[fid].segments:
                    reasons[sid] = f"facility:{fid}"
        if hard_only:
            return set(reasons), reasons
        for alert in self._active_alerts(now):
            for sid in self.topology.segments_in_zones(alert["zones"]):
                reasons[sid] = f"weather:{alert['alert_id']}"
        for control in self.controls.active_controls(now):
            for sid in control.elements:
                reasons[sid] = f"control:{control.id}"
        return set(reasons), reasons

    # --- 路线与状态查询 ---
    def recommend(self, start, goal, group, now=None):
        now = self._resolve_now(now)
        closed, reasons = self.closed_segments(now)
        estimates = self.estimator.all_estimates(now)
        route = recommend_route(self.topology, estimates, closed, start, goal, group, now)
        route["closed_segments"] = {sid: reasons[sid] for sid in sorted(closed)}
        return route

    def state(self, now=None):
        now = self._resolve_now(now)
        self.evaluate(now)
        touched = sorted(self.estimator.states)
        return {
            "now": fmt_time(now),
            "decisions": self.decisions,
            "evacuation": self.evacuation,
            "controls": [
                _control_view(c, now) for c in self.controls.controls.values()
            ],
            "estimates": [self.estimator.estimate(eid, now).to_dict() for eid in touched],
            "facility_status": dict(self.facility_status),
        }

    # --- 运营操作（HTTP 层调用） ---
    def impose_control(self, initiator, reason, elements, expires_at, control_id=None, now=None):
        now = self._resolve_now(now)
        control_id = control_id or f"C-{int(now)}-{len(self.controls.controls) + 1}"
        control = self.controls.impose(
            control_id, initiator, reason, list(elements), now, parse_time(expires_at)
        )
        self.evaluate(now)
        return _control_view(control, now)

    def release_control(self, control_id, operator, now=None):
        now = self._resolve_now(now)
        control = self.controls.release(control_id, operator, now)
        self.evaluate(now)
        return _control_view(control, now)

    # --- 快照与恢复（服务重启模拟） ---
    def snapshot(self):
        return {
            "now": self.now,
            "event_count": self.event_count,
            "estimator": self.estimator.to_dict(),
            "facility_status": dict(self.facility_status),
            "weather_alerts": [dict(a) for a in self.weather_alerts],
            "controls": self.controls.to_dict(),
        }

    @classmethod
    def restore(cls, topology, snapshot, config=None):
        service = cls(topology, config)
        service.now = snapshot["now"]
        service.event_count = snapshot["event_count"]
        service.estimator.load_dict(snapshot["estimator"])
        service.facility_status.update(snapshot["facility_status"])
        service.weather_alerts = [dict(a) for a in snapshot["weather_alerts"]]
        service.controls = ControlRegistry.from_dict(snapshot["controls"])
        service.evaluate()
        return service
