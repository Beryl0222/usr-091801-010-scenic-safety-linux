"""限流规则引擎。

每次限流（系统建议或自动措施）必须说清三件事：
1. rule_id / 版本：用的是哪条规则；
2. 证据与受影响路径：占用区间上界、置信度、命中的告警、涉及的边；
3. 解除依据：什么读数/告警状态满足后才允许解除。

核心原则——“不假装精确”：
- 阈值永远对占用区间的 high（保守上界）比较；
- 证据 uncertain（迟到/失联/乱序）时，阈值降档使用（更保守），
  且动作降为 require_confirmation，必须值班员人工确认后才下发；
- 设施停运/气象告警命中的边直接按对应规则处置。
"""

from dataclasses import dataclass

from weather import LEVEL_LABEL

RULEBOOK_VERSION = "scenic-rules-2026.1"

LEVEL_NORMAL = "normal"
LEVEL_WATCH = "watch"  # 关注
_LEVEL_HOLD = "hold"  # 截留（只出不进）
LEVEL_CLOSE = "close"  # 封闭
LEVEL_EVACUATE = "evacuate"  # 疏散

ACTION_NONE = "none"
ACTION_MONITOR = "monitor"
ACTION_CONFIRM = "require_confirmation"
ACTION_HOLD_ENTRY = "hold_entry"
ACTION_BLOCK = "block_edge"
ACTION_EVACUATE = "evacuate"

# 对占用上界 high 的阈值比例；不确定时整体乘 UNCERTAIN_DISCOUNT（提前触发）。
RULES = [
    {
        "rule_id": "OCC-EVAC-01",
        "name": "危险区段超员疏散",
        "level": LEVEL_EVACUATE,
        "action": ACTION_EVACUATE,
        "ratio": 0.95,
        "requires": "edge",
        "release": "占用上界连续10分钟低于0.70，且无 severe 气象告警覆盖该区段",
        "release_after": 10,
        "release_ratio": 0.70,
    },
    {
        "rule_id": "OCC-CLOSE-02",
        "name": "区段接近容量封闭",
        "level": LEVEL_CLOSE,
        "action": ACTION_BLOCK,
        "ratio": 0.85,
        "requires": "edge",
        "release": "占用上界连续10分钟低于0.65",
        "release_after": 10,
        "release_ratio": 0.65,
    },
    {
        "rule_id": "OCC-HOLD-03",
        "name": "换乘点/路段截留",
        "level": _LEVEL_HOLD,
        "action": ACTION_HOLD_ENTRY,
        "ratio": 0.70,
        "requires": "any",
        "release": "占用上界连续8分钟低于0.55",
        "release_after": 8,
        "release_ratio": 0.55,
    },
    {
        "rule_id": "OCC-WATCH-04",
        "name": "局部客流关注",
        "level": LEVEL_WATCH,
        "action": ACTION_MONITOR,
        "ratio": 0.50,
        "requires": "any",
        "release": "占用上界回落至0.40以下",
        "release_after": 0,
        "release_ratio": 0.40,
    },
]

# 迟到/失联证据的阈值折减：0.9 表示 70% 阈值按 63% 执行（更早触发）。
UNCERTAIN_DISCOUNT = {"late": 0.90, "stale": 0.80, "ghost": 0.80}
# 低于该置信度时，任何自动动作都必须转人工确认。
CONFIDENCE_CONFIRM_BELOW = 0.8
# 气象级别对应的边处置。
WEATHER_ACTIONS = {
    "warning": (LEVEL_CLOSE, ACTION_BLOCK, "FAC-WX-WARN", "气象预警覆盖，封闭路段"),
    "severe": (LEVEL_EVACUATE, ACTION_EVACUATE, "FAC-WX-SEVERE", "严重气象，疏散区段"),
}


@dataclass
class RuleHit:
    rule_id: str
    rule_version: str
    rule_name: str
    level: str
    action: str
    ref: str
    threshold_ratio: float
    observed_high_ratio: float
    confidence: float
    freshness: str
    uncertain: bool
    conservative: bool  # 是否因不确定而提前/降档
    requires_confirmation: bool
    affected_edges: list
    reason: str
    release_basis: str
    release_after: int
    evidence: dict

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "rule_name": self.rule_name,
            "level": self.level,
            "action": self.action,
            "ref": self.ref,
            "threshold_ratio": round(self.threshold_ratio, 3),
            "observed_high_ratio": round(self.observed_high_ratio, 3),
            "confidence": self.confidence,
            "freshness": self.freshness,
            "uncertain": self.uncertain,
            "conservative_adjustment": self.conservative,
            "requires_confirmation": self.requires_confirmation,
            "affected_edges": self.affected_edges,
            "reason": self.reason,
            "release_basis": self.release_basis,
            "release_after_min": self.release_after,
            "evidence": self.evidence,
        }


RULES_BY_ID = {rule["rule_id"]: rule for rule in RULES}

# 设施停运：硬事实（不依赖占用读数的不确定性），停运即禁止新乘客进入，
# 在段人员由运营按应急程序处置；设施恢复后解除判据即满足。
FACILITY_CLOSED_RULE = "FAC-STOP-05"


def evaluate_facility(topology, facility_id: str, facility, estimate, now: int) -> RuleHit | None:
    edge = topology.edges.get(facility_id)
    if edge is None or facility.status != "closed":
        return None
    capacity = edge.capacity or 0
    ratio = round(estimate.high / capacity, 3) if capacity else 0.0
    return RuleHit(
        rule_id=FACILITY_CLOSED_RULE,
        rule_version=RULEBOOK_VERSION,
        rule_name="设施停运封控",
        level=LEVEL_CLOSE,
        action=ACTION_BLOCK,
        ref=f"edge:{facility_id}",
        threshold_ratio=0.0,
        observed_high_ratio=ratio,
        confidence=estimate.confidence,
        freshness=estimate.fresh,
        uncertain=False,  # 停运状态本身确定；占用只用于提示在段人数
        conservative=False,
        requires_confirmation=False,
        affected_edges=[facility_id],
        reason=(
            f"设施于 {facility.changed_at} 停运，禁止新乘客进入；"
            f"当前在段估计 {estimate.low}-{estimate.high} 人，由运营按应急程序处置"
        ),
        release_basis="设施恢复 open 状态，并完成空载试车确认",
        release_after=0,
        evidence={
            "facility_id": facility_id,
            "facility_status": "closed",
            "changed_at": facility.changed_at,
            "estimate": estimate.to_dict(),
        },
    )


def _capacity_for(topology, ref: str) -> int:
    if ref.startswith("edge:"):
        edge = topology.edges.get(ref[5:])
        return edge.capacity if edge else 0
    node = topology.nodes.get(ref[5:])
    return node.capacity if node else 0


def weather_hit(topology, ref: str, alert, estimate) -> RuleHit | None:
    """气象告警对单个 ref（edge:/node:）生成处置命中。"""
    if not alert.applies_to(ref[5:], ref[5:] if ref.startswith("node:") else ""):
        return None
    if alert.level not in WEATHER_ACTIONS:
        return None
    edge_id = ref[5:] if ref.startswith("edge:") else ""
    capacity = _capacity_for(topology, ref)
    ratio = estimate.ratio_against(capacity) or 0.0
    level, action, rule_id, why = WEATHER_ACTIONS[alert.level]
    return RuleHit(
        rule_id=rule_id,
        rule_version=RULEBOOK_VERSION,
        rule_name="气象告警处置",
        level=level,
        action=action,
        ref=ref,
        threshold_ratio=0.0,
        observed_high_ratio=round(ratio, 3),
        confidence=estimate.confidence,
        freshness=estimate.fresh,
        uncertain=estimate.uncertain,
        conservative=False,
        requires_confirmation=estimate.confidence < CONFIDENCE_CONFIRM_BELOW,
        affected_edges=[edge_id] if edge_id else [],
        reason=f"{why}：{LEVEL_LABEL.get(alert.level, alert.level)} {alert.message}".rstrip("："),
        release_basis=f"{alert.hazard} 告警解除，且现场复核无次生危险",
        release_after=0,
        evidence={
            "alert_id": alert.alert_id,
            "alert_level": alert.level,
            "estimate": estimate.to_dict(),
        },
    )


def evaluate_zone(topology, estimate, weather_board, now: int) -> RuleHit | None:
    """对单个有直接观测的区段做规则判定；返回最高优先级的一个命中。"""
    ref = estimate.ref
    capacity = _capacity_for(topology, ref)
    edge_id = ref[5:] if ref.startswith("edge:") else ""

    # 气象优先：命中 warning/severe 直接给出对应处置。
    alert = weather_board.worst_for(edge_id, ref[5:] if ref.startswith("node:") else "")
    if alert:
        hit = weather_hit(topology, ref, alert, estimate)
        if hit:
            return hit

    if capacity <= 0:
        return None
    ratio = estimate.high / capacity
    discount = (
        UNCERTAIN_DISCOUNT.get(estimate.fresh, 1.0) if estimate.uncertain else 1.0
    )

    for rule in RULES:
        if rule["requires"] == "edge" and not edge_id:
            continue
        effective_threshold = rule["ratio"] * discount
        if ratio >= effective_threshold:
            conservative = discount < 1.0
            needs_confirm = (
                estimate.uncertain or estimate.confidence < CONFIDENCE_CONFIRM_BELOW
            )
            return RuleHit(
                rule_id=rule["rule_id"],
                rule_version=RULEBOOK_VERSION,
                rule_name=rule["name"],
                level=rule["level"],
                action=(
                    ACTION_CONFIRM
                    if needs_confirm and rule["action"] != ACTION_MONITOR
                    else rule["action"]
                ),
                ref=ref,
                threshold_ratio=effective_threshold,
                observed_high_ratio=ratio,
                confidence=estimate.confidence,
                freshness=estimate.fresh,
                uncertain=estimate.uncertain,
                conservative=conservative,
                requires_confirmation=needs_confirm,
                affected_edges=[edge_id] if edge_id else [],
                reason=(
                    f"占用上界 {estimate.high} 人 / 容量 {capacity} 人 "
                    f"= {ratio:.0%}，达到{('折减后' if conservative else '')}阈值 "
                    f"{effective_threshold:.0%}"
                    + ("；证据不确定，按保守阈值处理并请求人工确认" if needs_confirm else "")
                ),
                release_basis=rule["release"],
                release_after=rule["release_after"],
                evidence={
                    "low": estimate.low,
                    "point": estimate.point,
                    "high": estimate.high,
                    "capacity": capacity,
                    "freshness": estimate.fresh,
                    "sources": estimate.sources,
                },
            )
    return None
