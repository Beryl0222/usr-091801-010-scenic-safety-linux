"""疏散编排。

优先级铁律：已经进入危险区段的人先撤，上游接近人群只做截留、不与撤离抢道。

危险来源（同一套规则/管制/气象，不另立标准）：
- severe 气象覆盖的在段人员：最高优先；
- block（全封闭）边内在段人员：次高（结构性危险，禁止穿越该边撤离）；
- warning 气象 / OCC-EVAC-01：再次；
- hold（只出不进）边内人员：允许沿边外撤，优先于上游截留。

路径计算复用 routing 的疏散上下文：hold 边可顺向外撤，block 边仍禁行，
就近选择庇护点/出口。所有广播文案随游客群体语言列表输出。
"""

from dataclasses import dataclass

from restrictions import ACTION_BLOCK
from routing import RoutingContext, find_path

BROADCAST = {
    "severe": {
        "zh": "请立即停止前行，沿工作人员指引撤离至 {shelter}，勿在崖边与树下停留",
        "en": "Please stop and follow staff to {shelter}. Avoid cliff edges and trees",
    },
    "block": {
        "zh": "前方路段封闭，场内游客请沿单向外撤通道尽快离开，前往 {shelter}",
        "en": "Path closed ahead. Visitors inside please exit via the outward lane to {shelter}",
    },
    "evac": {
        "zh": "区段超员，请听从指引外撤至 {shelter}，不要逆向返回",
        "en": "Section over capacity. Please follow staff outward to {shelter}",
    },
    "hold": {
        "zh": "该路段只出不进，请场内游客继续向外移动至 {shelter}",
        "en": "Exit-only section. Visitors inside please continue outward to {shelter}",
    },
    "upstream": {
        "zh": "前方临时管制，请原地等待或改道，不要进入 {edge}",
        "en": "Temporary control ahead. Please wait or detour; do not enter {edge}",
    },
}

PRIORITY_RANK = {"severe": 4, "block": 3, "evac": 2, "hold": 1}


@dataclass
class Danger:
    edge_id: str
    category: str  # severe/block/evac/hold
    reason: str
    estimate: object
    rule_hit: object = None


def _broadcast(key: str, langs, **kwargs) -> list[dict]:
    template = BROADCAST[key]
    out = []
    for lang in langs:
        text = template.get(lang) or template.get("en") or template.get("zh", "")
        out.append({"lang": lang, "text": text.format(**kwargs)})
    return out


def collect_dangers(topo, estimator, weather_board, restriction_board, rule_hits, now):
    """返回 (dangers, unconfirmed)。

    dangers = 已可执行处置的危险（生效管制 / 气象硬告警 / 设施停运 /
              高置信度占用命中）；
    unconfirmed = 证据不确定、尚未经人工确认的候选，只能提示核实，
              不生成疏散指令（不把失联传感器的旧读数当成精确人数）。
    """
    dangers: dict[str, Danger] = {}
    candidates: dict[str, Danger] = {}
    # 管制边：内在段人员需要外撤。
    for r in restriction_board.active(now):
        category = "block" if r.action == ACTION_BLOCK else "hold"
        for edge_id in r.edge_ids:
            dangers[edge_id] = Danger(
                edge_id=edge_id,
                category=category,
                reason=f"管制 {r.restriction_id}：{r.reason}",
                estimate=estimator.estimate_zone(f"edge:{edge_id}", now),
            )
    # 规则命中（含气象处置）；占用直接向估计器重新取，避免依赖证据序列化。
    for hit in rule_hits:
        if not hit.ref.startswith("edge:"):
            continue
        edge_id = hit.affected_edges[0]
        # 未确认的不确定证据：只进入待核实候选，不执行疏散；
        # 同一区段若已有更高级别的硬危险（气象/设施/管制），不再重复列为候选。
        if hit.action == "require_confirmation" and not hit.rule_id.startswith("FAC-WX-"):
            if edge_id not in dangers:
                candidates[edge_id] = Danger(
                    edge_id=edge_id, category="unconfirmed", reason=hit.reason,
                    estimate=estimator.estimate_zone(hit.ref, now), rule_hit=hit,
                )
            continue
        if hit.rule_id == "FAC-WX-SEVERE":
            category = "severe"
        elif hit.rule_id in ("FAC-WX-WARN", "FAC-STOP-05"):
            category = "block"
        elif hit.action == "evacuate":
            category = "evac"
        elif hit.action == "block_edge":
            category = "block"
        elif hit.action == "require_confirmation":
            category = "evac"
        elif hit.action == "hold_entry":
            category = "hold"
        else:
            continue
        current = dangers.get(edge_id)
        if current is None or PRIORITY_RANK[category] > PRIORITY_RANK[current.category]:
            dangers[edge_id] = Danger(
                edge_id=edge_id, category=category, reason=hit.reason,
                estimate=estimator.estimate_zone(hit.ref, now),
                rule_hit=hit,
            )
    # 已有硬危险（气象/设施/管制）的边不再保留为待确认候选。
    for edge_id in list(candidates):
        if edge_id in dangers:
            del candidates[edge_id]
    return list(dangers.values()), list(candidates.values())


def _nearest_shelter(topo, node_id, ctx: RoutingContext):
    best = None
    for shelter in topo.shelters():
        if shelter.node_id == node_id:
            return shelter, [], [node_id], 0.0
        edge_path, node_path, weight, _ = find_path(topo, node_id, shelter.node_id, ctx)
        if edge_path is not None and (best is None or weight < best[3]):
            best = (shelter, edge_path, node_path, weight)
    return best  # 可能为 None（无可撤路径）


def build_evacuation(
    topo, dangers, candidates, ctx_template: RoutingContext, langs, now
) -> dict:
    """生成按优先级排序的疏散指令；ctx_template 提供管制/设施/占用上下文。

    candidates（证据不确定）只列为待人工核实项，不生成可执行路线。
    """
    orders = []
    upstream_alerts = set()
    danger_edge_ids = {d.edge_id for d in dangers}
    # 危险侧节点：所有危险边的端点。源自这些节点、通向外侧集结点的弧
    # 属于撤离通道（如索道下行、天梯下撤道、通往庇护点的支路），不得截留。
    danger_node_ids = {
        endpoint
        for danger in dangers
        for danger_edge in (topo.edges.get(danger.edge_id),)
        if danger_edge is not None
        for endpoint in (danger_edge.src, danger_edge.dst)
    }
    for danger in sorted(dangers, key=lambda d: -PRIORITY_RANK[d.category]):
        edge = topo.edges.get(danger.edge_id)
        if edge is None or danger.estimate is None:
            continue
        est = danger.estimate
        ctx = RoutingContext(
            topo,
            now,
            max_fitness=4,
            restrictions=ctx_template.restrictions,
            facilities=ctx_template.facilities,
            occupancy=ctx_template.occupancy,
            evacuation=True,
            langs=langs,
        )
        # 边内游客从两端就近外撤，取耗时较短的一端作为主撤离方向。
        shelter_options = []
        for endpoint in (edge.src, edge.dst):
            found = _nearest_shelter(topo, endpoint, ctx)
            if found:
                shelter_options.append((endpoint, found))
        if not shelter_options:
            orders.append(
                {
                    "edge_id": edge.edge_id,
                    "category": danger.category,
                    "priority": PRIORITY_RANK[danger.category],
                    "status": "no_safe_route",
                    "reason": danger.reason,
                    "occupancy_range": [est.low, est.high],
                    "confidence": est.confidence,
                    "requires_field_escort": True,
                }
            )
            continue
        endpoint, (shelter, edge_path, node_path, weight) = min(
            shelter_options, key=lambda c: c[1][3]
        )
        shelter_names = [
            {"lang": lang, "text": topo.localize(shelter.name, [lang])["text"]}
            for lang in langs
        ]
        used_edge_ids = [e.edge_id for e in edge_path]
        # 上游：沿通行方向汇入危险段入口、且源点在安全侧的边，
        # 对游客截留、不与撤离抢道；排除危险段自身与源自危险侧的撤离通道。
        upstream_alerts.update(
            arc.edge_id
            for arc in topo.predecessors(edge.src)
            if arc.edge_id not in danger_edge_ids and arc.src not in danger_node_ids
        )
        orders.append(
            {
                "edge_id": edge.edge_id,
                "category": danger.category,
                "priority": PRIORITY_RANK[danger.category],
                "status": "route_ready",
                "reason": danger.reason,
                "people_inside_range": [est.low, est.high],
                "people_inside_point": est.point,
                "confidence": est.confidence,
                "freshness": est.fresh,
                "uncertain": est.uncertain,
                "exit_from": endpoint,
                "shelter_id": shelter.node_id,
                "shelter_names": shelter_names,
                "egress_edges": used_edge_ids,
                "egress_nodes": node_path,
                "egress_minutes": round(weight, 1),
                "broadcast": _broadcast(
                    danger.category,
                    langs,
                    shelter=topo.localize(shelter.name, langs)["text"],
                ),
            }
        )
    # 优先级高的排前面；同优先级时不确定的（失联）先处理，因为最危险。
    orders.sort(key=lambda o: (-o.get("priority", 0), not o.get("uncertain", False)))
    unconfirmed = [
        {
            "edge_id": d.edge_id,
            "reason": d.reason,
            "freshness": d.estimate.fresh if d.estimate else "unknown",
            "confidence": d.estimate.confidence if d.estimate else 0.0,
            "occupancy_range": (
                [d.estimate.low, d.estimate.high] if d.estimate else None
            ),
            "action_required": "field_verification_before_dispatch",
            "broadcast": _broadcast(
                "upstream", langs,
                edge=topo.localize(topo.edges[d.edge_id].name, langs)["text"]
                if d.edge_id in topo.edges else d.edge_id,
            ),
        }
        for d in sorted(candidates, key=lambda d: d.edge_id)
    ]
    return {
        "generated_at": now,
        "principle": "already_inside_first",
        "langs": langs,
        "orders": orders,
        "upstream_hold_edges": sorted(upstream_alerts),
        "unconfirmed_candidates": unconfirmed,
    }
