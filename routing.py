"""路线推荐。

约束全部来自同一份拓扑与实时状态：
- 单向边：只沿声明方向通行（adjacency 已处理反向可见性）；
- 计划关闭窗口：窗口内边不可用；
- 临时管制：block 双向禁行；hold 在普通推荐中视为禁入（疏散时允许外撤）；
- 体力等级：超过游客自报等级的边不进入路线；
- 设施状态：索道/天梯/接驳 closed 不可用，degraded 给出提示；
- 多语种群体：按其语言列表返回每种语言的名称与提示，缺语回退。

路径用 Dijkstra；权重 = 行程时间 + 候车时间 + 占用拥堵附加。
"""

import heapq


# 内置多语种提示（数据中的名称自带多语种，提示语走这个小词典）。
PHRASES = {
    "oneway": {"zh": "单向路段，不可逆行", "en": "One-way path"},
    "closed_window": {"zh": "处于计划关闭窗口", "en": "Scheduled closure"},
    "facility_degraded": {"zh": "设施降速运行，等候时间增加", "en": "Reduced service, expect delays"},
    "fitness": {"zh": "需要体力等级 {level}", "en": "Fitness level {level} required"},
    "congested": {"zh": "当前较拥挤，已计入绕行权重", "en": "Crowded; weighted for detour"},
    "held": {"zh": "临时截留管制，普通路线不可进入", "en": "Entry held by temporary control"},
    "blocked": {"zh": "临时封闭管制", "en": "Closed by temporary control"},
}


def _phrase(key: str, langs: list[str], **kwargs) -> list[dict]:
    template = PHRASES[key]
    out = []
    for lang in langs:
        text = template.get(lang) or template.get("en") or template.get("zh", "")
        out.append({"lang": lang, "text": text.format(**kwargs)})
    return out


class RoutingContext:
    def __init__(
        self,
        topology,
        minute: int,
        *,
        max_fitness: int = 4,
        restrictions=None,
        facilities: dict | None = None,
        occupancy=None,
        evacuation: bool = False,
        langs=None,
    ):
        self.topo = topology
        self.minute = minute
        self.max_fitness = max_fitness
        self.restrictions = restrictions
        self.facilities = facilities or {}  # edge_id -> FacilityState
        self.occupancy = occupancy or {}  # edge_id -> ZoneEstimate
        self.evacuation = evacuation
        self.langs = langs or ["zh"]
        self.blocked = restrictions.blocked_edges(minute) if restrictions else set()
        self.held = restrictions.held_edges(minute) if restrictions else set()
        self.held_nodes = restrictions.held_nodes(minute) if restrictions else set()
        self.blocked_nodes = restrictions.blocked_nodes(minute) if restrictions else set()

    def edge_state(self, edge) -> dict:
        """返回该定向边是否可走及不可走原因/提示。"""
        notices = []
        if not edge.usable(self.minute):
            return {"passable": False, "reason": "closed_window"}
        if edge.edge_id in self.blocked:
            return {"passable": False, "reason": "blocked"}
        if edge.edge_id in self.held and not self.evacuation:
            return {"passable": False, "reason": "held"}
        # 节点管制：
        # - block（危险点）：任何上下文都不得进入；
        # - hold（截留）：正常推荐不可进入，疏散时可穿过向外撤离。
        if edge.dst in self.blocked_nodes:
            return {"passable": False, "reason": "node_blocked"}
        if edge.dst in self.held_nodes and not self.evacuation:
            return {"passable": False, "reason": "node_held"}
        if edge.fitness > self.max_fitness:
            return {"passable": False, "reason": "fitness"}
        fac = self.facilities.get(edge.edge_id)
        if fac and fac.status == "closed":
            return {"passable": False, "reason": "facility_closed"}
        if fac and fac.status == "degraded":
            notices.append("facility_degraded")
        est = self.occupancy.get(edge.edge_id)
        if est and edge.capacity and est.high / edge.capacity >= 0.7:
            notices.append("congested")
        if edge.is_oneway():
            notices.append("oneway")
        return {"passable": True, "notices": notices}

    def weight(self, edge) -> float:
        w = float(edge.travel_min)
        if edge.headway:
            w += edge.headway / 2.0  # 平均候车半个发班间隔
        fac = self.facilities.get(edge.edge_id)
        if fac and fac.status == "degraded":
            w /= max(0.2, fac.effective_throughput())
        est = self.occupancy.get(edge.edge_id)
        if est and edge.capacity:
            w += 12.0 * min(1.5, est.high / edge.capacity)
        return w


def _localize_names(topo, ids, langs, kind):
    names = {}
    for lid in ids:
        obj = topo.edges[lid] if kind == "edge" else topo.nodes[lid]
        names[lid] = [
            {"lang": lang, "text": topo.localize(obj.name, [lang])["text"]}
            for lang in langs
        ]
    return names


def find_path(topo, src: str, dst: str, ctx: RoutingContext):
    """Dijkstra；返回 (定向边序列, 节点序列, 权重, 拒绝原因表)。"""
    if src not in topo.nodes or dst not in topo.nodes:
        return None, None, None, {"missing": "起终点不在拓扑中"}
    dist = {src: 0.0}
    prev: dict[str, tuple[str, object]] = {}
    queue = [(0.0, src)]
    rejections: dict[str, list[str]] = {}
    while queue:
        d, node = heapq.heappop(queue)
        if d > dist.get(node, float("inf")):
            continue
        if node == dst:
            break
        for edge in topo.incident(node):
            state = ctx.edge_state(edge)
            if not state["passable"]:
                rejections.setdefault(edge.edge_id, []).append(state["reason"])
                continue
            nd = d + ctx.weight(edge)
            if nd < dist.get(edge.dst, float("inf")):
                dist[edge.dst] = nd
                prev[edge.dst] = (node, edge)
                heapq.heappush(queue, (nd, edge.dst))
    if dst not in dist:
        return None, None, None, {"no_route": rejections}
    edge_path = []
    node_path = [dst]
    cur = dst
    while cur != src:
        node, edge = prev[cur]
        edge_path.append(edge)
        node_path.append(node)
        cur = node
    edge_path.reverse()
    node_path.reverse()
    return edge_path, node_path, dist[dst], {}


def plan_route(topo, src: str, dst: str, ctx: RoutingContext) -> dict:
    langs = ctx.langs
    edge_path, node_path, total, fail = find_path(topo, src, dst, ctx)
    if edge_path is None:
        return {
            "feasible": False,
            "from": src,
            "to": dst,
            "langs": langs,
            "constraints_applied": _summarize_rejections(topo, fail, ctx),
        }
    edge_ids = [e.edge_id for e in edge_path]
    edge_names = _localize_names(topo, edge_ids, langs, "edge")
    node_names = _localize_names(topo, node_path, langs, "node")
    steps = []
    all_notice_keys = []
    for edge in edge_path:
        state = ctx.edge_state(edge)
        all_notice_keys.extend(state["notices"])
        steps.append(
            {
                "edge_id": edge.edge_id,
                "names": edge_names[edge.edge_id],
                "kind": edge.kind,
                "direction": edge.direction,
                "fitness": edge.fitness,
                "travel_min": edge.travel_min,
                "notices": [n for key in state["notices"] for n in _phrase(key, langs, level=edge.fitness)],
            }
        )
    constraints = [
        {
            "type": "max_fitness",
            "detail": [{"lang": l["lang"], "text": l["text"]} for l in _phrase("fitness", langs, level=ctx.max_fitness)],
        },
        {"type": "scheduled_closures_respected", "minute": ctx.minute},
        {"type": "oneway_respected"},
    ]
    if ctx.restrictions and ctx.restrictions.active(ctx.minute):
        constraints.append(
            {
                "type": "active_restrictions",
                "restriction_ids": [r.restriction_id for r in ctx.restrictions.active(ctx.minute)],
            }
        )
    return {
        "feasible": True,
        "from": src,
        "to": dst,
        "from_names": node_names[src],
        "to_names": node_names[dst],
        "langs": langs,
        "steps": steps,
        "nodes": [{"id": n, "names": node_names[n]} for n in node_path],
        "total_travel_min": round(total, 1),
        "fitness_required": max((e.fitness for e in edge_path), default=1),
        "notices": [
            {"key": key, "i18n": _phrase(key, langs)}
            for key in sorted(set(all_notice_keys))
        ],
        "constraints_applied": constraints,
    }


def _summarize_rejections(topo, fail: dict, ctx: RoutingContext) -> list:
    if "missing" in fail:
        return [{"type": "missing_endpoint", "detail": fail["missing"]}]
    reasons: dict[str, set] = {}
    for edge_id, why in fail.get("no_route", {}).items():
        reasons.setdefault(edge_id, set()).update(why)
    out = []
    for edge_id, why in reasons.items():
        out.append({"edge_id": edge_id, "blocked_by": sorted(why)})
    return out
