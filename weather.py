"""气象告警状态机。

告警有级别（advisory/warning/severe）和适用范围（节点/边/全园区）。
同级或更高级别可覆盖；clear 只解除不高于其已确认级别的告警，
避免旧的迟到 clear 事件把新的暴雨预警错误抹掉。
"""

from dataclasses import dataclass

LEVEL_ORDER = {"advisory": 1, "warning": 2, "severe": 3}
LEVEL_LABEL = {"advisory": "提示", "warning": "预警", "severe": "严重预警"}


@dataclass
class Alert:
    alert_id: str
    level: str
    hazard: str  # rainstorm/thunder/gale/heat/...
    scope: str  # park / edge / node
    refs: tuple[str, ...]
    raised_at: int
    observed_at: int
    message: str = ""

    def applies_to(self, edge_id: str, node_id: str = "") -> bool:
        if self.scope == "park":
            return True
        if self.scope == "edge":
            return edge_id in self.refs
        return edge_id in self.refs or node_id in self.refs

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "level": self.level,
            "hazard": self.hazard,
            "scope": self.scope,
            "refs": list(self.refs),
            "raised_at": self.raised_at,
            "observed_at": self.observed_at,
            "message": self.message,
            "label": LEVEL_LABEL.get(self.level, self.level),
        }


class WeatherBoard:
    def __init__(self):
        self._alerts: dict[str, Alert] = {}  # key = scope|ref|hazard

    @staticmethod
    def _key(scope: str, refs: tuple[str, ...], hazard: str) -> str:
        return f"{scope}|{','.join(sorted(refs))}|{hazard}"

    def raise_alert(self, event) -> Alert:
        p = event.payload
        scope = p.get("scope", "park")
        refs = tuple(sorted(p.get("refs", [])))
        level = p.get("level", "advisory")
        if level not in LEVEL_ORDER:
            raise ValueError(f"未知气象级别: {level}")
        alert = Alert(
            alert_id=event.event_id,
            level=level,
            hazard=p.get("hazard", "weather"),
            scope=scope,
            refs=refs,
            raised_at=event.occurred_at,
            observed_at=event.observed_at,
            message=p.get("message", ""),
        )
        key = self._key(scope, refs, alert.hazard)
        old = self._alerts.get(key)
        # 迟到的低级告警不能覆盖更新的高级告警。
        if old and (
            LEVEL_ORDER[old.level] > LEVEL_ORDER[level]
            or (LEVEL_ORDER[old.level] == LEVEL_ORDER[level] and old.raised_at > alert.raised_at)
        ):
            return old
        self._alerts[key] = alert
        return alert

    def clear(self, event) -> list[str]:
        """解除事件：可带 hazard/refs 精确定位；返回解除掉的 key。"""
        p = event.payload
        scope = p.get("scope", "park")
        refs = tuple(sorted(p.get("refs", [])))
        hazard = p.get("hazard")
        cleared = []
        for key, alert in list(self._alerts.items()):
            if alert.scope != scope:
                continue
            if hazard and alert.hazard != hazard:
                continue
            if refs and set(alert.refs) != set(refs):
                continue
            if event.occurred_at < alert.raised_at:
                # 迟到的旧解除不能作用于更晚产生的告警。
                continue
            del self._alerts[key]
            cleared.append(key)
        return cleared

    def active(self) -> list[Alert]:
        return sorted(self._alerts.values(), key=lambda a: (-LEVEL_ORDER[a.level], a.raised_at))

    def worst_for(self, edge_id: str, node_id: str = "") -> Alert | None:
        hits = [a for a in self._alerts.values() if a.applies_to(edge_id, node_id)]
        if not hits:
            return None
        return max(hits, key=lambda a: LEVEL_ORDER[a.level])

    def worst_level(self) -> int:
        if not self._alerts:
            return 0
        return max(LEVEL_ORDER[a.level] for a in self._alerts.values())
