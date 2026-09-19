"""运营临时管制：必须带到期时间，解除需两名独立操作员确认。"""

from dataclasses import asdict, dataclass, field


class ControlError(ValueError):
    pass


@dataclass
class Control:
    id: str
    initiator: str
    reason: str
    elements: list
    created_at: float
    expires_at: float
    approvals: list = field(default_factory=list)
    released_at: float | None = None

    def active(self, now):
        return self.released_at is None and now < self.expires_at

    def to_dict(self):
        data = asdict(self)
        data["active"] = None  # 由调用方按当前时间填充
        return data


class ControlRegistry:
    def __init__(self):
        self.controls = {}

    def impose(self, control_id, initiator, reason, elements, now, expires_at):
        if not initiator:
            raise ControlError("管制必须记录发起人")
        if not elements:
            raise ControlError("管制必须指定受影响路段")
        if expires_at is None:
            raise ControlError("临时管制必须设置到期时间")
        if expires_at <= now:
            raise ControlError("到期时间必须晚于当前时间")
        if control_id in self.controls and self.controls[control_id].active(now):
            raise ControlError(f"管制 {control_id} 已存在且未解除")
        control = Control(control_id, initiator, reason, list(elements), now, expires_at)
        self.controls[control_id] = control
        return control

    def release(self, control_id, operator, now):
        control = self.controls.get(control_id)
        if control is None:
            raise ControlError(f"管制 {control_id} 不存在")
        if control.released_at is not None:
            raise ControlError("管制已解除")
        if now >= control.expires_at:
            raise ControlError("管制已到期自动失效，无需解除")
        if operator == control.initiator:
            raise ControlError("发起人不能参与解除，需两名独立操作员")
        if operator in control.approvals:
            raise ControlError("同一操作员不能重复确认")
        control.approvals.append(operator)
        if len(control.approvals) >= 2:
            control.released_at = now
        return control

    def active_controls(self, now):
        return [c for c in self.controls.values() if c.active(now)]

    # --- 快照 ---
    def to_dict(self):
        return [asdict(c) for c in self.controls.values()]

    @classmethod
    def from_dict(cls, data):
        registry = cls()
        for raw in data:
            control = Control(**raw)
            registry.controls[control.id] = control
        return registry
