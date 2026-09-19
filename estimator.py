"""带置信度的占用估计。

三类证据汇成每个区段（边/节点）的占用区间 [low, high]：
1. 闸机计数：入口累计进 - 出口累计出 = 在园总人数（先验）；
2. 区段匿名计数：直接读数，给出窄区间；
3. 在段计数缺失时，用流量在途模型从历史闸机流推断（宽区间）。

新鲜度决定置信度，而不是丢弃读数：
- fresh（新鲜）：读数在 feed.late_after 内；
- late（迟到）：读数本身迟到，或自上次读数已超过 late_after；
- stale（失联）：超过 feed.stale_after 无任何读数，
  在园人数只增不减的不确定性按流失速率放大占用上界。

任何区段只要其证据链上有 late/stale，估计就标 uncertain，
阈值判定一律对 high 做比较（保守阈值），并要求人工确认。
"""

from dataclasses import dataclass

from events import (
    Feed,
    TYPE_FACILITY_STATUS,
    TYPE_GATE_COUNT,
    TYPE_SHUTTLE_CAPACITY,
    TYPE_ZONE_COUNT,
)

FRESH = "fresh"
LATE = "late"
STALE = "stale"
GHOST = "ghost"  # 从未收到过读数的馈源

# 失联后每分钟每人的占用漂移比例（游客继续流动，在段人数不确定地变化）。
STALE_DRIFT_PER_MIN = 0.06
# 迟到读数自身的相对误差放宽（迟到期间可能已有变化）。
LATE_REL_ERROR = 0.10
STALE_REL_ERROR = 0.30
FRESH_REL_ERROR = 0.03


@dataclass
class Reading:
    value: int
    occurred_at: int
    observed_at: int
    late: bool


@dataclass
class FeedState:
    feed: Feed
    last: Reading | None = None
    out_of_order: bool = False
    seen: bool = False

    def freshness(self, now: int) -> str:
        if self.last is None:
            return GHOST
        age = now - self.last.observed_at
        if age > self.feed.stale_after:
            return STALE
        if age > self.feed.late_after or self.last.late:
            return LATE
        return FRESH


@dataclass
class ZoneEstimate:
    ref: str  # edge:<id> / node:<id>
    low: int
    point: int
    high: int
    confidence: float  # 0..1
    fresh: str  # fresh/late/stale/ghost/mixed
    uncertain: bool
    sources: list
    basis: str

    def ratio_against(self, capacity: int) -> float | None:
        if capacity <= 0:
            return None
        return self.high / capacity

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "low": self.low,
            "point": self.point,
            "high": self.high,
            "confidence": round(self.confidence, 3),
            "freshness": self.fresh,
            "uncertain": self.uncertain,
            "sources": self.sources,
            "basis": self.basis,
        }


@dataclass
class FacilityState:
    ref: str
    status: str = "open"  # open/degraded/closed
    throughput_factor: float = 1.0
    changed_at: int = 0
    observed_at: int = 0
    available: int | None = None  # 接驳下一班可售座位
    available_at: int = 0
    last_observed: int = 0

    def effective_throughput(self) -> float:
        if self.status == "closed":
            return 0.0
        if self.status == "degraded":
            return max(0.05, self.throughput_factor)
        return 1.0


class OccupancyEstimator:
    def __init__(self, topology, feeds: list[Feed]):
        self.topo = topology
        self.feeds: dict[str, FeedState] = {f.feed_id: FeedState(f) for f in feeds}
        # 按 ref 索引馈源（一个 ref 可有多个冗余馈源）。
        self.refs: dict[str, list[FeedState]] = {}
        for state in self.feeds.values():
            self.refs.setdefault(state.feed.ref, []).append(state)
        # 闸机累计量。
        self.gates: dict[str, dict] = {}
        self.facilities: dict[str, FacilityState] = {}
        self.total_entries = 0
        self.total_exits = 0
        self.applied_events: list[str] = []

    # ------------------------------------------------------------------ 应用

    def apply(self, event) -> dict | None:
        """按事件类型更新读数；返回该事件的处理摘要（供审计）。"""
        p = event.payload
        if event.type == TYPE_GATE_COUNT:
            gate_id = p.get("gate_id") or event.source
            entry = self.gates.setdefault(
                gate_id, {"entries": 0, "exits": 0, "last_at": -1, "late": False}
            )
            entry["entries"] += int(p.get("entries", 0))
            entry["exits"] += int(p.get("exits", 0))
            if entry["last_at"] >= 0 and event.occurred_at < entry["last_at"]:
                # 增量计数乱序：累计会重复计数，必须标记，由区间吸收误差。
                entry["out_of_order"] = True
            entry["last_at"] = max(entry["last_at"], event.occurred_at)
            entry["late"] = event.is_late(self._late_after(event.source))
            entry["observed_at"] = event.observed_at
            self.total_entries += int(p.get("entries", 0))
            self.total_exits += int(p.get("exits", 0))
            self.applied_events.append(event.event_id)
            return {"gate": gate_id, "entries_delta": p.get("entries", 0)}

        if event.type == TYPE_ZONE_COUNT:
            state = self._state_for_feed(event.source)
            ref = state.feed.ref if state else p.get("ref", "")
            if state:
                late = event.is_late(state.feed.late_after)
                if state.seen and event.occurred_at < state.last.occurred_at:
                    state.out_of_order = True
                state.last = Reading(
                    value=max(0, int(p.get("count", 0))),
                    occurred_at=event.occurred_at,
                    observed_at=event.observed_at,
                    late=late,
                )
                state.seen = True
            self.applied_events.append(event.event_id)
            return {"zone": ref, "count": p.get("count", 0)}

        if event.type == TYPE_FACILITY_STATUS:
            ref = p.get("facility_id") or event.source
            fac = self.facilities.setdefault(ref, FacilityState(ref=ref))
            # 迟到的旧状态不能覆盖更新的状态。
            if event.occurred_at >= fac.changed_at:
                fac.status = p.get("status", fac.status)
                fac.throughput_factor = float(p.get("throughput_factor", fac.throughput_factor))
                fac.changed_at = event.occurred_at
            fac.observed_at = event.observed_at
            fac.last_observed = event.observed_at
            self.applied_events.append(event.event_id)
            return {"facility": ref, "status": fac.status}

        if event.type == TYPE_SHUTTLE_CAPACITY:
            ref = p.get("facility_id") or event.source
            fac = self.facilities.setdefault(ref, FacilityState(ref=ref))
            if event.occurred_at >= fac.available_at:
                fac.available = int(p.get("available", 0))
                fac.available_at = event.occurred_at
            fac.last_observed = event.observed_at
            self.applied_events.append(event.event_id)
            return {"facility": ref, "available": fac.available}

        return None

    def _state_for_feed(self, source: str) -> FeedState | None:
        return self.feeds.get(source)

    def _late_after(self, source: str) -> int:
        state = self.feeds.get(source)
        return state.feed.late_after if state else 5

    # ------------------------------------------------------------------ 推断

    def in_park(self) -> int:
        return max(0, self.total_entries - self.total_exits)

    def _direct_estimate(self, ref: str, now: int) -> ZoneEstimate | None:
        states = [s for s in self.refs.get(ref, []) if s.seen]
        if not states:
            return None
        # 多馈源取最新读数；新鲜度取最差（任一馈源失联即整体不确定）。
        states.sort(key=lambda s: s.last.occurred_at, reverse=True)
        chosen = states[0]
        freshness_order = {FRESH: 0, LATE: 1, STALE: 2, GHOST: 3}
        # 任一冗余馈源失联/迟到，整体证据就按最差新鲜度处理。
        worst = max((s.freshness(now) for s in states), key=lambda f: freshness_order[f])
        age = now - chosen.last.observed_at
        value = chosen.last.value
        lost_gap = self._stale_gap(ref) + 20
        if age > lost_gap:
            # 读数失联过久：旧计数已无意义，不假装知道在段人数。
            # 占用区间给物理上界 [0, capacity]（无容量信息时用粗上界），
            # 极低置信度，规则侧必然走人工确认。
            cap = self._capacity_hint(ref)
            point = value
            return ZoneEstimate(
                ref=ref,
                low=0,
                point=point,
                high=cap if cap else max(value * 3, 10),
                confidence=0.12,
                fresh=STALE,
                uncertain=True,
                sources=[s.feed.feed_id for s in states] + ["lost_reading"],
                basis="reading_lost_unknown_occupancy",
            )
        if worst == STALE or chosen.freshness(now) == STALE:
            rel = STALE_REL_ERROR + STALE_DRIFT_PER_MIN * max(0, age - self._stale_gap(ref))
        elif worst == LATE:
            rel = LATE_REL_ERROR
        else:
            rel = FRESH_REL_ERROR
        if chosen.out_of_order or any(s.out_of_order for s in states):
            rel += 0.1
        rel = min(rel, 1.2)
        margin = max(2, round(value * rel))
        confidence = {FRESH: 0.95, LATE: 0.7, STALE: 0.4, GHOST: 0.2}[worst]
        return ZoneEstimate(
            ref=ref,
            low=max(0, value - margin),
            point=value,
            high=value + margin,
            confidence=confidence,
            fresh=worst,
            uncertain=worst != FRESH,
            sources=[s.feed.feed_id for s in states],
            basis="direct_zone_count",
        )

    def _stale_gap(self, ref: str) -> int:
        states = self.refs.get(ref, [])
        return min((s.feed.stale_after for s in states), default=12)

    def _capacity_hint(self, ref: str) -> int:
        if ref.startswith("edge:"):
            edge = self.topo.edges.get(ref[5:])
            return edge.capacity if edge else 0
        node = self.topo.nodes.get(ref[5:])
        return node.capacity if node else 0

    def _flow_estimate(self, ref: str, now: int) -> ZoneEstimate:
        """无直接区段计数时的在途推断：宽区间、低置信度。"""
        edge = self.topo.edges.get(ref.removeprefix("edge:")) if ref.startswith("edge:") else None
        if edge is None:
            return ZoneEstimate(ref, 0, 0, 0, 0.2, GHOST, True, [], "no_data")
        # 极朴素的在途模型：在园人数按边容量占全园区容量的比例分摊，
        # 再乘以行程时间占开放时长的比例。仅用于“没有传感器时也不假装为 0”。
        total_cap = sum(e.capacity for e in self.topo.edge_objects() if e.capacity) or 1
        share = edge.capacity / total_cap if edge.capacity else 1.0 / max(1, len(self.topo.edges))
        point = round(self.in_park() * share * min(1.0, edge.travel_min / 60.0))
        margin = max(5, round(point * 0.5))
        return ZoneEstimate(
            ref=ref,
            low=max(0, point - margin),
            point=point,
            high=point + margin,
            confidence=0.2,
            fresh="ghost",
            uncertain=True,
            sources=["flow_model:gate_totals"],
            basis="inferred_from_gate_totals",
        )

    def estimate_zone(self, ref: str, now: int) -> ZoneEstimate:
        direct = self._direct_estimate(ref, now)
        if direct:
            return direct
        return self._flow_estimate(ref, now)

    def observed_refs(self) -> set[str]:
        """至少收到过一次直接读数的区段（迟到/失联仍算“有观测链”）。"""
        return {
            state.feed.ref
            for state in self.feeds.values()
            if state.seen
        }

    def observed_zones(self, now: int) -> list[ZoneEstimate]:
        return [self.estimate_zone(ref, now) for ref in sorted(self.observed_refs())]

    def all_zones(self, now: int) -> list[ZoneEstimate]:
        refs = [f"edge:{e.edge_id}" for e in self.topo.edge_objects()]
        refs += [f"node:{n.node_id}" for n in self.topo.nodes.values()]
        # 节点也可能有直接计数馈源。
        result = []
        for ref in refs:
            has_direct = any(s.seen for s in self.refs.get(ref, []))
            if ref.startswith("edge:") or has_direct:
                result.append(self.estimate_zone(ref, now))
        return result

    def feed_health(self, now: int) -> list[dict]:
        rows = []
        for state in self.feeds.values():
            f = state.feed
            if f.type == TYPE_ZONE_COUNT:
                rows.append(
                    {
                        "feed_id": f.feed_id,
                        "type": f.type,
                        "ref": f.ref,
                        "freshness": state.freshness(now),
                        "last_observed_at": state.last.observed_at if state.last else None,
                        "last_occurred_at": state.last.occurred_at if state.last else None,
                        "out_of_order": state.out_of_order,
                        "late_after": f.late_after,
                        "stale_after": f.stale_after,
                    }
                )
            elif f.type == TYPE_GATE_COUNT:
                gate = self.gates.get(f.ref)
                if gate is None:
                    freshness = GHOST
                    observed = occurred = None
                    ooo = False
                else:
                    observed = gate.get("observed_at")
                    occurred = gate.get("last_at")
                    age = now - observed
                    freshness = (
                        STALE if age > f.stale_after
                        else LATE if age > f.late_after or gate.get("late")
                        else FRESH
                    )
                    ooo = gate.get("out_of_order", False)
                rows.append(
                    {
                        "feed_id": f.feed_id, "type": f.type, "ref": f.ref,
                        "freshness": freshness,
                        "last_observed_at": observed,
                        "last_occurred_at": occurred,
                        "out_of_order": ooo,
                        "late_after": f.late_after,
                        "stale_after": f.stale_after,
                    }
                )
            else:  # facility_status / shuttle_capacity
                fac_ref = f.ref.removeprefix("edge:")
                fac = self.facilities.get(fac_ref)
                if fac is None or not fac.last_observed:
                    freshness = GHOST
                    observed = occurred = None
                else:
                    observed = fac.last_observed
                    occurred = fac.changed_at if f.type == TYPE_FACILITY_STATUS else fac.available_at
                    age = now - observed
                    freshness = STALE if age > f.stale_after else (
                        LATE if age > f.late_after else FRESH
                    )
                rows.append(
                    {
                        "feed_id": f.feed_id, "type": f.type, "ref": f.ref,
                        "freshness": freshness,
                        "last_observed_at": observed,
                        "last_occurred_at": occurred,
                        "out_of_order": False,
                        "late_after": f.late_after,
                        "stale_after": f.stale_after,
                    }
                )
        return sorted(rows, key=lambda r: (r["freshness"], r["feed_id"]))
