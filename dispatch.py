"""调度核心：事件摄入、规则评估、措施生命周期、重启重建。

措施（measure）分两类生命周期：
- confident 命中：系统自动下发管制（issued_by=system:auto，带 TTL 与解除依据）；
- uncertain 命中（迟到/失联/低置信度）：只生成 pending_confirmation，
  值班员确认后才下发管制——系统不替人对不确定数据做硬决定。

所有改变管制的操作（运营人工下发、确认、解除）都写回事件日志，
service_restart 后状态完全由日志重建，不依赖内存残留。
"""

from dataclasses import dataclass, field

from clock import Clock
from estimator import OccupancyEstimator
from events import (
    Event,
    TYPE_FACILITY_STATUS,
    TYPE_GATE_COUNT,
    TYPE_RESTRICTION,
    TYPE_RESTRICTION_RELEASE,
    TYPE_SERVICE_RESTART,
    TYPE_SHUTTLE_CAPACITY,
    TYPE_WEATHER_ALERT,
    TYPE_WEATHER_CLEAR,
    TYPE_ZONE_COUNT,
    Feed,
)
from evacuation import build_evacuation, collect_dangers
from restrictions import (
    ACTION_BLOCK,
    ACTION_HOLD,
    RestrictionBoard,
    RestrictionError,
)
from rules import (
    ACTION_BLOCK as R_ACTION_BLOCK,
    ACTION_CONFIRM,
    ACTION_EVACUATE,
    ACTION_HOLD_ENTRY,
    ACTION_MONITOR,
    FACILITY_CLOSED_RULE,
    RULEBOOK_VERSION,
    weather_hit,
    evaluate_facility,
    evaluate_zone,
)
from weather import LEVEL_ORDER, WeatherBoard

# 自动措施的默认有效期（分钟），到期自动失效，可由值班员提前双人解除。
AUTO_TTL = {
    ACTION_HOLD_ENTRY: 30,
    R_ACTION_BLOCK: 20,
    ACTION_EVACUATE: 15,
}
# 硬事实（设施停运/气象）不靠短时 TTL 收口——封控持续到事实解除并满足
# 释放判据为止；TTL 只作为兜底上限，防止异常情况下永久悬挂。
RULE_TTL = {
    "FAC-STOP-05": 120,
    "FAC-WX-WARN": 90,
    "FAC-WX-SEVERE": 60,
}

STATUS_PENDING = "pending_confirmation"
STATUS_ACTIVE = "active"
STATUS_MONITOR = "monitoring"  # 仅监控提示，不产生管制
STATUS_CLEARED = "condition_cleared"
STATUS_DENIED = "denied"


@dataclass
class Measure:
    measure_id: str
    created_at: int
    status: str
    rule_hit: dict
    ref: str
    action: str
    restriction_id: str = ""
    decided_by: str = ""
    history: list = field(default_factory=list)
    release_ready: bool = False
    release_recommendation: str = ""

    def decide(self, status: str, actor: str, detail: str, at: int, restriction_id: str = ""):
        self.status = status
        self.decided_by = actor
        if restriction_id:
            self.restriction_id = restriction_id
        self.history.append({"at": at, "actor": actor, "status": status, "detail": detail})

    def to_dict(self, now: int) -> dict:
        return {
            "measure_id": self.measure_id,
            "created_at": self.created_at,
            "status": self.status,
            "ref": self.ref,
            "action": self.action,
            "restriction_id": self.restriction_id,
            "decided_by": self.decided_by,
            "rule": self.rule_hit,
            "history": self.history,
            "release_ready": self.release_ready,
            "release_recommendation": self.release_recommendation,
        }


class DispatchError(ValueError):
    pass


class Dispatch:
    def __init__(self, topology, feeds: list[Feed]):
        self.topo = topology
        self.feeds_def = list(feeds)
        self.clock = Clock()
        self.event_log: list[Event] = []
        self.restarts: list[int] = []
        self._rebuild()
        self.measures: dict[str, Measure] = {}
        self._measure_seq = 0
        # (ref, rule_id) -> measure_id，便于命中去重与状态延续。
        self._measure_index: dict[tuple, str] = {}
        # measure_id -> 解除判据首次持续满足的分钟时刻。
        self._clear_since: dict[str, int] = {}
        # 确定性 nonce，使人工下发/解除产生的编号在回放中可复现。
        self._nonce = 0

    def _next_nonce(self) -> str:
        self._nonce += 1
        return f"{self._nonce:04d}"

    # ------------------------------------------------------------ 状态与重建

    def _rebuild(self):
        """从事件日志重建全部运行状态（初始化或服务重启时调用）。"""
        self.clock = Clock()
        self.estimator = OccupancyEstimator(self.topo, self.feeds_def)
        self.weather = WeatherBoard()
        self.restrictions = RestrictionBoard()
        for event in self.event_log:
            self.clock.advance_to(event.observed_at)
            self._apply_domain_event(event)

    def _apply_domain_event(self, event: Event):
        if event.type == TYPE_WEATHER_ALERT:
            self.weather.raise_alert(event)
        elif event.type == TYPE_WEATHER_CLEAR:
            self.weather.clear(event)
        elif event.type == TYPE_RESTRICTION:
            self.restrictions.issue(event)
        elif event.type == TYPE_RESTRICTION_RELEASE:
            self.restrictions.release(event)
        else:
            self.estimator.apply(event)

    def ingest(self, event: Event) -> dict:
        """摄入一个外部观测事件。"""
        if event.occurred_at > event.observed_at:
            raise DispatchError("事件发生时间晚于入库时间")
        self.clock.advance_to(event.observed_at)
        if event.type == TYPE_SERVICE_RESTART:
            # 重启：日志保留，运行态全部重建；措施记录保留（审计），
            # 但运行中的管制以重建后的 RestrictionBoard 为准。
            active_before = {r.restriction_id for r in self.restrictions.active(self.clock.now())}
            self.event_log.append(event)
            self.restarts.append(event.occurred_at)
            self._rebuild()
            # 措施是运行派生产物：重启后清空，由后续 evaluate 依据
            # 日志中恢复的管制（R-AUTO-* 自带 measure_id）重新挂接。
            self.measures = {}
            self._measure_seq = 0
            for r in self.restrictions.all():
                if r.measure_id.startswith("M-") and r.measure_id[2:].isdigit():
                    self._measure_seq = max(self._measure_seq, int(r.measure_id[2:]))
            self._measure_index = {}
            self._clear_since = {}
            self._nonce = 0
            active_after = {r.restriction_id for r in self.restrictions.active(self.clock.now())}
            return {
                "rebuilt": True,
                "at": event.occurred_at,
                "active_restrictions_before": sorted(active_before),
                "active_restrictions_after": sorted(active_after),
                "events_replayed": len(self.event_log) - 1,
            }
        self.event_log.append(event)
        self._apply_domain_event(event)
        return {"rebuilt": False, "type": event.type, "id": event.event_id}

    # ------------------------------------------------------------ 评估与措施

    def _collect_hits(self, now: int) -> list:
        hits = []
        observed = set()
        # 占用规则只作用于有直接观测链的区段（含迟到/失联），
        # 从不给“没有传感器的边”伪造一个高占用上界去触发限流。
        for est in self.estimator.observed_zones(now):
            observed.add(est.ref)
            hit = evaluate_zone(self.topo, est, self.weather, now)
            if hit:
                hits.append(hit)
        # 气象告警是硬事实：即使某边没有任何传感器，告警覆盖也要处置。
        for alert in self.weather.active():
            refs = []
            if alert.scope == "edge":
                refs = [f"edge:{r}" for r in alert.refs if r in self.topo.edges]
            elif alert.scope == "node":
                refs = [f"node:{r}" for r in alert.refs if r in self.topo.nodes]
            elif alert.scope == "park":
                refs = [f"edge:{e.edge_id}" for e in self.topo.edge_objects()]
            for ref in refs:
                if ref in observed:
                    continue
                est = self.estimator.estimate_zone(ref, now)
                hit = weather_hit(self.topo, ref, alert, est)
                if hit:
                    hits.append(hit)
        # 设施停运是硬事实，独立于占用读数触发封控。
        for facility_id, facility in self.estimator.facilities.items():
            if facility.status == "closed" and facility_id in self.topo.edges:
                est = self.estimator.estimate_zone(f"edge:{facility_id}", now)
                hit = evaluate_facility(self.topo, facility_id, facility, est, now)
                if hit:
                    hits.append(hit)
        return hits

    def evaluate(self, now: int | None = None) -> dict:
        now = self.clock.now() if now is None else now
        hits = self._collect_hits(now)

        seen_keys = set()
        for hit in hits:
            key = (hit.ref, hit.rule_id)
            seen_keys.add(key)
            existing_id = self._measure_index.get(key)
            existing = self.measures.get(existing_id) if existing_id else None
            if existing is not None and existing.status == STATUS_ACTIVE:
                # 管制一旦生效，只有解除判据/到期能收口；证据变差（迟到/失联）
                # 绝不放松已下发的管制，只刷新证据让释放判断更保守。
                existing.rule_hit = hit.to_dict()
                continue
            if hit.action == ACTION_MONITOR:
                self._advisory(hit, now, existing)
            elif hit.requires_confirmation or hit.action == ACTION_CONFIRM:
                if existing is None or existing.status != STATUS_DENIED:
                    self._upsert_pending(hit, now, existing)
                else:
                    existing.rule_hit = hit.to_dict()  # 驳回抑制期内仍刷新证据
            else:
                self._auto_issue(hit, now, existing)

        # 命中消失：挂起/监控/被驳回措施随条件消除关闭；
        # 仍持有生效管制的措施保持 active，由解除判据或到期收口，
        # 不能在管制实际解除前假装“措施已结束”。
        for key, mid in list(self._measure_index.items()):
            measure = self.measures[mid]
            if measure.status not in (STATUS_PENDING, STATUS_MONITOR, STATUS_DENIED):
                continue
            if key in seen_keys:
                continue
            measure.decide(STATUS_CLEARED, "system", "触发条件已消除", now)

        self._reconcile_measures(hits, now)

        return {
            "evaluated_at": now,
            "rulebook_version": RULEBOOK_VERSION,
            "in_park": self.estimator.in_park(),
            "hits": [h.to_dict() for h in hits],
            "measures": [m.to_dict(now) for m in self.measures.values()],
        }

    def _new_measure_id(self) -> str:
        self._measure_seq += 1
        return f"M-{self._measure_seq:04d}"

    def _advisory(self, hit, now: int, existing: "Measure | None") -> Measure:
        """WATCH 命中：只更新监控提示，绝不下发管制。"""
        if existing is None:
            existing = Measure(
                measure_id=self._new_measure_id(),
                created_at=now,
                status=STATUS_MONITOR,
                rule_hit=hit.to_dict(),
                ref=hit.ref,
                action=ACTION_MONITOR,
            )
            existing.history.append(
                {"at": now, "actor": "system", "status": STATUS_MONITOR,
                 "detail": "客流关注：仅提示值班席，不采取封控"}
            )
            self.measures[existing.measure_id] = existing
            self._measure_index[(hit.ref, hit.rule_id)] = existing.measure_id
        else:
            existing.rule_hit = hit.to_dict()
            if existing.status != STATUS_MONITOR:
                # 从更强状态回落为监控（如人工驳回后仍超 WATCH 线）。
                existing.decide(STATUS_MONITOR, "system",
                                "证据回落至关注级，仅保留监控", now)
        return existing

    def _upsert_pending(self, hit, now: int, existing: "Measure | None") -> Measure:
        key = (hit.ref, hit.rule_id)
        if existing is not None:
            existing.rule_hit = hit.to_dict()
            if existing.status == STATUS_PENDING:
                return existing
            # 同一区段同一规则再次达到需要确认的程度（前次已解除/驳回/到期）：
            # 复用措施并重新挂起，保持一个可追溯的生命周期。
            existing.decide(
                STATUS_PENDING, "system",
                "证据不确定，重新等待人工确认（未自动下发管制）", now,
            )
            existing.restriction_id = ""
            return existing
        measure = Measure(
            measure_id=self._new_measure_id(),
            created_at=now,
            status=STATUS_PENDING,
            rule_hit=hit.to_dict(),
            ref=hit.ref,
            action=_underlying_action(hit) if hit.action == ACTION_CONFIRM else hit.action,
        )
        measure.history.append(
            {
                "at": now,
                "actor": "system",
                "status": STATUS_PENDING,
                "detail": "证据不确定，等待人工确认（未自动下发管制）",
            }
        )
        self.measures[measure.measure_id] = measure
        self._measure_index[key] = measure.measure_id
        return measure

    def _auto_issue(self, hit, now: int, existing: "Measure | None") -> Measure:
        key = (hit.ref, hit.rule_id)
        if existing is not None and existing.status == STATUS_ACTIVE:
            existing.rule_hit = hit.to_dict()
            return existing
        # 已有针对同一规则+区段的生效管制（可能是人工下发），不重复下发。
        for r in self.restrictions.active(now):
            if hit.affected_edges and any(e in r.edge_ids for e in hit.affected_edges):
                if r.rule_id == hit.rule_id:
                    return self._attach_existing(key, r, hit, now, existing)
        if existing is None:
            measure = Measure(
                measure_id=self._new_measure_id(),
                created_at=now,
                status=STATUS_ACTIVE,
                rule_hit=hit.to_dict(),
                ref=hit.ref,
                action=hit.action,
            )
            self.measures[measure.measure_id] = measure
            self._measure_index[key] = measure.measure_id
        else:
            # 复用同一措施（可能此前挂起后证据转充分，或前次管制已解除后复发）。
            measure = existing
            measure.created_at = now
            measure.rule_hit = hit.to_dict()
            measure.status = STATUS_ACTIVE
            measure.restriction_id = ""
        ttl = RULE_TTL.get(hit.rule_id) or AUTO_TTL.get(hit.action, 20)
        edge_ids, node_ids = _hit_targets(hit)
        attempt = 1 + sum(1 for h in measure.history if h["status"] == STATUS_ACTIVE)
        suffix = f"-{attempt}" if attempt > 1 else ""
        event = Event(
            event_id=f"evt-auto-{measure.measure_id}{suffix}",
            type=TYPE_RESTRICTION,
            occurred_at=now,
            observed_at=now,
            payload={
                "restriction_id": f"R-AUTO-{measure.measure_id}{suffix}",
                "action": _restriction_action(hit.action),
                "edges": edge_ids,
                "nodes": node_ids,
                "reason": hit.reason,
                "issued_by": "system:auto",
                "expires_at": now + ttl,
                "rule_id": hit.rule_id,
                "measure_id": measure.measure_id,
                "release_basis": hit.release_basis,
            },
        )
        restriction = self.restrictions.issue(event)
        self.event_log.append(event)
        measure.restriction_id = restriction.restriction_id
        measure.history.append(
            {
                "at": now,
                "actor": "system:auto",
                "status": STATUS_ACTIVE,
                "detail": f"证据充分（置信度 {hit.confidence:.2f}），自动下发，{ttl} 分钟后到期；"
                f"解除依据：{hit.release_basis}",
            }
        )
        return measure

    def _attach_existing(self, key, restriction, hit, now, existing=None) -> Measure:
        if existing is None:
            measure = Measure(
                measure_id=self._new_measure_id(),
                created_at=now,
                status=STATUS_ACTIVE,
                rule_hit=hit.to_dict(),
                ref=hit.ref,
                action=hit.action,
                restriction_id=restriction.restriction_id,
            )
            self.measures[measure.measure_id] = measure
            self._measure_index[key] = measure.measure_id
        else:
            measure = existing
            measure.rule_hit = hit.to_dict()
            measure.status = STATUS_ACTIVE
            measure.restriction_id = restriction.restriction_id
        measure.history.append(
            {"at": now, "actor": "system", "status": STATUS_ACTIVE,
             "detail": f"命中已生效管制 {restriction.restriction_id}，不重复下发"}
        )
        return measure

    def _reconcile_measures(self, hits: list, now: int):
        """按“解除依据”收口生效措施：

        - 系统自动下发（system:auto）且判据持续满足 release_after 分钟的，
          系统按规则自动解除（复核方记为规则手册版本，进入事件日志）；
        - 人工下发/人工确认的管制，系统只把“已满足解除判据”标记出来，
          仍由值班员双人解除；
        - 已到期的管制系统早已自动失效，同步关闭措施。
        """
        from rules import RULES_BY_ID

        live_hit_refs = {(h.ref, h.rule_id): h for h in hits}
        for measure in list(self.measures.values()):
            if measure.status != STATUS_ACTIVE or not measure.restriction_id:
                continue
            restriction = self.restrictions.get(measure.restriction_id)
            if restriction is None:
                continue
            if not restriction.is_active(now):
                measure.decide(
                    "expired" if now >= restriction.expires_at else "released",
                    "system", f"管制 {restriction.restriction_id} 已"
                    + ("到期自动失效" if now >= restriction.expires_at else "被解除"),
                    now,
                )
                self._clear_since.pop(measure.measure_id, None)
                continue

            rule = RULES_BY_ID.get(measure.rule_hit.get("rule_id", ""))
            criterion_met = False
            basis_detail = ""
            if measure.rule_hit.get("rule_id", "").startswith("FAC-WX-"):
                edge_id = restriction.edge_ids[0] if restriction.edge_ids else ""
                need_level = 3 if "SEVERE" in measure.rule_hit["rule_id"] else 2
                alert = self.weather.worst_for(edge_id)
                criterion_met = alert is None or LEVEL_ORDER[alert.level] < need_level
                basis_detail = "覆盖该区段的气象告警已解除"
            elif measure.rule_hit.get("rule_id") == FACILITY_CLOSED_RULE:
                edge_id = restriction.edge_ids[0] if restriction.edge_ids else ""
                facility = self.estimator.facilities.get(edge_id)
                criterion_met = facility is not None and facility.status == "open"
                basis_detail = "设施已恢复 open 状态"
            elif rule:
                est = self.estimator.estimate_zone(measure.ref, now)
                capacity = rule and self._capacity_of(measure.ref)
                ratio = est.high / capacity if capacity else 0.0
                criterion_met = ratio < rule["release_ratio"]
                basis_detail = (
                    f"占用上界比例 {ratio:.0%} 低于解除阈值 {rule['release_ratio']:.0%}"
                )

            measure.release_ready = criterion_met
            if not criterion_met:
                self._clear_since.pop(measure.measure_id, None)
                continue
            since = self._clear_since.setdefault(measure.measure_id, now)
            sustained = now - since
            release_after = rule["release_after"] if rule else 0
            if restriction.issued_by != "system:auto":
                # 人工管制：只提示、不代解除。
                measure.release_recommendation = (
                    f"已满足解除判据（{basis_detail}，持续 {sustained} 分钟）；"
                    "仍须双人解除或等待到期"
                )
                continue
            if sustained < release_after:
                continue
            event = Event(
                event_id=f"evt-autorelease-{self._next_nonce()}",
                type=TYPE_RESTRICTION_RELEASE,
                occurred_at=now,
                observed_at=now,
                payload={
                    "restriction_id": restriction.restriction_id,
                    "released_by": "system:auto",
                    "witness": f"rulebook:{RULEBOOK_VERSION}",
                    "basis": f"{basis_detail}，持续 {release_after} 分钟：{restriction.release_basis}",
                },
            )
            self.restrictions.release(event)
            self.event_log.append(event)
            measure.decide(
                "auto_released", "system:auto",
                event.payload["basis"], now,
            )
            self._clear_since.pop(measure.measure_id, None)

    def _capacity_of(self, ref: str) -> int:
        if ref.startswith("edge:"):
            edge = self.topo.edges.get(ref[5:])
            return edge.capacity if edge else 0
        node = self.topo.nodes.get(ref[5:])
        return node.capacity if node else 0

    def confirm_measure(self, measure_id: str, actor: str, ttl: int | None = None) -> Measure:
        """值班员对不确定证据触发的挂起措施做人工确认，随后下发管制。"""
        measure = self.measures.get(measure_id)
        if measure is None:
            raise DispatchError(f"措施不存在: {measure_id}")
        if measure.status != STATUS_PENDING:
            raise DispatchError(f"措施当前状态 {measure.status}，不可确认")
        now = self.clock.now()
        hit_dict = measure.rule_hit
        ttl = ttl or AUTO_TTL.get(measure.action, 20)
        restriction_id = f"R-MAN-{self._next_nonce()}"
        edge_ids, node_ids = _hit_targets(hit_dict)
        event = Event(
            event_id=f"evt-confirm-{self._next_nonce()}",
            type=TYPE_RESTRICTION,
            occurred_at=now,
            observed_at=now,
            payload={
                "restriction_id": restriction_id,
                "action": _restriction_action(measure.action),
                "edges": edge_ids,
                "nodes": node_ids,
                "reason": f"人工确认下发：{hit_dict['reason']}",
                "issued_by": actor,
                "expires_at": now + ttl,
                "rule_id": hit_dict["rule_id"],
                "measure_id": measure.measure_id,
                "release_basis": hit_dict["release_basis"],
            },
        )
        restriction = self.restrictions.issue(event)
        self.event_log.append(event)
        measure.decide(
            STATUS_ACTIVE, actor,
            f"人工确认不确定证据后下发；{ttl} 分钟后到期；解除依据：{hit_dict['release_basis']}",
            now, restriction_id=restriction.restriction_id,
        )
        return measure

    def deny_measure(self, measure_id: str, actor: str, note: str = "") -> Measure:
        measure = self.measures.get(measure_id)
        if measure is None or measure.status != STATUS_PENDING:
            raise DispatchError("只能驳回挂起中的措施")
        measure.decide(STATUS_DENIED, actor, f"值班员驳回：{note}", self.clock.now())
        return measure

    def release_restriction(self, restriction_id: str, released_by: str, witness: str) -> dict:
        """未到期解除走双人确认；到期管制系统已自动失效，无需解除。"""
        now = self.clock.now()
        event = Event(
            event_id=f"evt-release-{self._next_nonce()}",
            type=TYPE_RESTRICTION_RELEASE,
            occurred_at=now,
            observed_at=now,
            payload={
                "restriction_id": restriction_id,
                "released_by": released_by,
                "witness": witness,
            },
        )
        try:
            restriction = self.restrictions.release(event)
        except RestrictionError as exc:
            raise DispatchError(str(exc)) from exc
        self.event_log.append(event)
        for measure in self.measures.values():
            if measure.restriction_id == restriction_id and measure.status == STATUS_ACTIVE:
                measure.decide(
                    "released", released_by,
                    f"双人解除（复核 {witness}）；解除依据：{restriction.release_basis or '值班判定'}",
                    now,
                )
        return restriction.to_dict(now)

    # ------------------------------------------------------------ 视图

    def zone_views(self, now: int | None = None) -> list[dict]:
        now = self.clock.now() if now is None else now
        views = []
        for est in self.estimator.all_zones(now):
            capacity = 0
            if est.ref.startswith("edge:"):
                edge = self.topo.edges.get(est.ref[5:])
                capacity = edge.capacity if edge else 0
            else:
                node = self.topo.nodes.get(est.ref[5:])
                capacity = node.capacity if node else 0
            row = est.to_dict()
            row["capacity"] = capacity
            row["high_ratio"] = round(est.high / capacity, 3) if capacity else None
            views.append(row)
        return sorted(
            views,
            key=lambda r: (r["high_ratio"] is None, -(r["high_ratio"] or 0)),
        )

    def facility_views(self, now: int | None = None) -> list[dict]:
        now = self.clock.now() if now is None else now
        rows = []
        for ref, fac in self.estimator.facilities.items():
            edge = self.topo.edges.get(ref)
            rows.append(
                {
                    "facility_id": ref,
                    "names": (
                        [
                            {"lang": lang, "text": self.topo.localize(edge.name, [lang])["text"]}
                            for lang in ("zh", "en")
                        ]
                        if edge
                        else [{"lang": "zh", "text": ref}]
                    ),
                    "status": fac.status,
                    "throughput_factor": fac.effective_throughput(),
                    "changed_at": fac.changed_at,
                    "available_next_headway": fac.available,
                    "last_observed_at": fac.last_observed,
                    "observation_age_min": now - fac.last_observed if fac.last_observed else None,
                }
            )
        return sorted(rows, key=lambda r: r["facility_id"])

    def routing_context_kwargs(self) -> dict:
        now = self.clock.now()
        occupancy = {}
        for est in self.estimator.all_zones(now):
            if est.ref.startswith("edge:"):
                occupancy[est.ref[5:]] = est
        return {
            "restrictions": self.restrictions,
            "facilities": dict(self.estimator.facilities),
            "occupancy": occupancy,
        }

    def evacuation_plan(self, langs, now: int | None = None) -> dict:
        from routing import RoutingContext

        now = self.clock.now() if now is None else now
        hits = self._collect_hits(now)
        dangers, candidates = collect_dangers(
            self.topo, self.estimator, self.weather, self.restrictions, hits, now
        )
        template = RoutingContext(self.topo, now, langs=langs, **self.routing_context_kwargs())
        return build_evacuation(self.topo, dangers, candidates, template, langs, now)


def _underlying_action(hit) -> str:
    """require_confirmation 包装下的真实意图，用于选择 TTL/管制方向。"""
    rule_action = {
        "OCC-EVAC-01": ACTION_EVACUATE,
        "FAC-WX-SEVERE": ACTION_EVACUATE,
        "OCC-CLOSE-02": R_ACTION_BLOCK,
        "FAC-WX-WARN": R_ACTION_BLOCK,
        "OCC-HOLD-03": ACTION_HOLD_ENTRY,
    }
    return rule_action.get(hit.rule_id, ACTION_HOLD_ENTRY)


def _hit_targets(hit) -> tuple[list[str], list[str]]:
    """规则命中可能作用于边或节点；节点命中时管制其入场。"""
    if isinstance(hit, dict):
        ref = hit["ref"]
        edges = list(hit.get("affected_edges", []))
    else:
        ref = hit.ref
        edges = list(hit.affected_edges)
    nodes = [ref[5:]] if ref.startswith("node:") else []
    return edges, nodes


def _restriction_action(measure_action: str) -> str:
    if measure_action == R_ACTION_BLOCK or measure_action == ACTION_EVACUATE:
        return ACTION_BLOCK
    return ACTION_HOLD
