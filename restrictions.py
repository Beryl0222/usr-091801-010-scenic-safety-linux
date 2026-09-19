"""运营临时管制。

每条管制必须有：发起人、原因、到期时间、至少一个受影响边/节点。
到期自动失效（只可延期，不可“忘记解除”）；
未到期解除必须双人确认（解除人 + 复核人，且两人不同、均非发起人本人）。

封锁方向区分：
- hold（截留/限流）：允许出、不允许进，用于把游客挡在危险区段外；
- block（全封闭）：两个方向都禁行，用于结构性危险。
"""

import secrets
from dataclasses import dataclass, field

ACTION_HOLD = "hold"  # 只出不进（截留）
ACTION_BLOCK = "block"  # 双向封闭
ACTIONS = {ACTION_HOLD, ACTION_BLOCK}

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_RELEASED = "released"


@dataclass
class Restriction:
    restriction_id: str
    action: str
    edge_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    reason: str
    issued_by: str
    issued_at: int
    expires_at: int
    confirm_by: str = ""  # 下发时的第二人确认（可选）
    rule_id: str = ""  # 触发本次限流的规则（人工下发也登记所依据的告警/规则）
    measure_id: str = ""  # 关联的系统措施编号
    release_basis: str = ""  # 解除判据：满足什么条件才可解除
    releases: list = field(default_factory=list)  # [{"by","witness","at","event_id"}]

    def is_active(self, now: int) -> bool:
        if self.releases:
            return False
        return now < self.expires_at

    def status(self, now: int) -> str:
        if self.releases:
            return STATUS_RELEASED
        return STATUS_ACTIVE if now < self.expires_at else STATUS_EXPIRED

    def blocks_edge(self, edge_id: str, now: int) -> bool:
        return self.is_active(now) and edge_id in self.edge_ids

    def to_dict(self, now: int) -> dict:
        if self.releases:
            lift = f"已由 {self.releases[-1]['by']} 解除、{self.releases[-1]['witness']} 复核"
        elif now >= self.expires_at:
            lift = "已到期自动失效"
        else:
            lift = (
                f"到期（{self.expires_at}）自动失效；未到期解除须双人确认"
                + (f"，且应满足解除判据：{self.release_basis}" if self.release_basis else "")
            )
        return {
            "restriction_id": self.restriction_id,
            "action": self.action,
            "edges": list(self.edge_ids),
            "nodes": list(self.node_ids),
            "reason": self.reason,
            "issued_by": self.issued_by,
            "confirm_by": self.confirm_by,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "rule_id": self.rule_id,
            "measure_id": self.measure_id,
            "release_basis": self.release_basis,
            "release_state": lift,
            "status": self.status(now),
            "releases": list(self.releases),
        }


class RestrictionError(ValueError):
    pass


class RestrictionBoard:
    def __init__(self):
        self._items: dict[str, Restriction] = {}

    def issue(self, event) -> Restriction:
        p = event.payload
        rid = p.get("restriction_id") or f"R-{secrets.token_hex(4)}"
        if rid in self._items:
            raise RestrictionError(f"管制编号重复: {rid}")
        action = p.get("action", ACTION_HOLD)
        if action not in ACTIONS:
            raise RestrictionError(f"未知管制动作: {action}")
        edges = tuple(p.get("edges", []))
        nodes = tuple(p.get("nodes", []))
        if not edges and not nodes:
            raise RestrictionError("管制必须影响至少一条边或一个节点")
        issued_by = p.get("issued_by", "")
        if not issued_by:
            raise RestrictionError("管制必须记录发起人")
        expires_at = p.get("expires_at")
        if expires_at is None:
            raise RestrictionError("临时管制必须设置到期时间")
        expires_at = int(expires_at)
        if expires_at <= event.occurred_at:
            raise RestrictionError("到期时间必须晚于发起时间")
        item = Restriction(
            restriction_id=rid,
            action=action,
            edge_ids=edges,
            node_ids=nodes,
            reason=p.get("reason", ""),
            issued_by=issued_by,
            issued_at=event.occurred_at,
            expires_at=expires_at,
            confirm_by=p.get("confirm_by", ""),
            rule_id=p.get("rule_id", ""),
            measure_id=p.get("measure_id", ""),
            release_basis=p.get("release_basis", ""),
        )
        self._items[rid] = item
        return item

    def release(self, event) -> Restriction:
        """解除管制。

        - 人工提前解除：解除人 + 复核人，两人不同，且都不能是发起人；
        - 系统按规则自动解除：released_by=system:auto、witness=rulebook:<版本>，
          依据事件日志中的规则判据，重放时结果一致；
        - 已到期：系统自动失效，无需解除，直接报错提示。
        """
        p = event.payload
        rid = p.get("restriction_id")
        item = self._items.get(rid)
        if item is None:
            raise RestrictionError(f"管制不存在: {rid}")
        if item.releases:
            raise RestrictionError(f"管制已解除: {rid}")
        by = p.get("released_by", "")
        witness = p.get("witness", "")
        if by == "system:auto":
            if not witness.startswith("rulebook:"):
                raise RestrictionError("系统解除的复核方必须是规则手册版本")
            if event.occurred_at >= item.expires_at:
                raise RestrictionError("管制已到期自动失效，无需再解除")
        else:
            if event.occurred_at >= item.expires_at:
                raise RestrictionError("管制已到期，无需双人解除（系统自动失效）")
            if not by or not witness:
                raise RestrictionError("提前解除必须记录解除人与复核人")
            if by == witness:
                raise RestrictionError("解除人与复核人不能为同一人")
            if by == item.issued_by or witness == item.issued_by:
                raise RestrictionError("发起人不能参与解除自己下发的管制")
        item.releases.append(
            {
                "by": by,
                "witness": witness,
                "at": event.occurred_at,
                "event_id": event.event_id,
                "basis": p.get("basis", ""),
            }
        )
        return item

    def active(self, now: int) -> list[Restriction]:
        return [r for r in self._items.values() if r.is_active(now)]

    def blocked_edges(self, now: int) -> set[str]:
        """block（全封闭）边：两个方向都禁行。hold 边不在此列（允许外撤）。"""
        return {
            e
            for r in self.active(now)
            if r.action == ACTION_BLOCK
            for e in r.edge_ids
        }

    def held_edges(self, now: int) -> set[str]:
        """hold 类管制的边：可顺危险方向外撤、不可进入。"""
        return {
            e
            for r in self.active(now)
            if r.action == ACTION_HOLD
            for e in r.edge_ids
        }

    def blocked_nodes(self, now: int) -> set[str]:
        """全封闭节点：疏散时也不得进入（危险点）。"""
        return {
            n
            for r in self.active(now)
            if r.action == ACTION_BLOCK
            for n in r.node_ids
        }

    def held_nodes(self, now: int) -> set[str]:
        """截留节点（hold）：禁止新游客进入，疏散时可穿过向外撤离。

        block 节点同时也不可新进入，因此取全部生效管制的节点；
        是否连疏散都禁入由 blocked_nodes 区分。
        """
        return {n for r in self.active(now) for n in r.node_ids}

    def all(self) -> list[Restriction]:
        return list(self._items.values())

    def get(self, rid: str) -> Restriction | None:
        return self._items.get(rid)
