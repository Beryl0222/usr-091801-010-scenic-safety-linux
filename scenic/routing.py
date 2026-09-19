"""路线推荐与疏散路径。

推荐路线尊重单向路段、关闭窗口、体力等级与实时占用；疏散路径为已进入
危险区段的游客优先生成，必要时允许逆行单向路段（明确标记 contra_flow）。
"""

import heapq

from .model import localize

INSTRUCTION_TEMPLATES = {
    "zh": "沿「{name}」前往{to}",
    "en": "Take {name} to {to}",
    "ja": "「{name}」で{to}へ",
    "ko": "「{name}」을 따라 {to}(으)로 이동",
}
SUPPORTED_LANGUAGES = tuple(INSTRUCTION_TEMPLATES)


def _ratio_of(estimates, element_id):
    estimate = estimates.get(element_id)
    return estimate.ratio if estimate is not None else 0.0


def _dijkstra(topology, start, goals, edge_iter):
    """通用最短路；edge_iter(node) 产出 (邻居, 路段id, 权重, 是否逆行)。"""
    goals = set(goals)
    best = {start: 0.0}
    prev = {}
    queue = [(0.0, start)]
    while queue:
        cost, node = heapq.heappop(queue)
        if cost > best.get(node, float("inf")):
            continue
        if node in goals:
            legs = []
            cursor = node
            while cursor != start:
                parent, segment_id, contra = prev[cursor]
                legs.append((segment_id, contra))
                cursor = parent
            legs.reverse()
            return node, cost, legs
        for neighbor, segment_id, weight, contra in edge_iter(node):
            new_cost = cost + weight
            if new_cost < best.get(neighbor, float("inf")):
                best[neighbor] = new_cost
                prev[neighbor] = (node, segment_id, contra)
                heapq.heappush(queue, (new_cost, neighbor))
    return None


def recommend_route(topology, estimates, closed, start, goal, group, now):
    """为游客群体推荐路线；closed 为当前不可用路段集合。"""
    lang = group.get("language", "zh")
    if lang not in INSTRUCTION_TEMPLATES:
        lang = "zh"
    max_exertion = int(group.get("max_exertion", 5))

    if start not in topology.nodes or goal not in topology.nodes:
        return {"ok": False, "reason": "unknown_node", "language": lang}

    def edge_iter(node):
        for seg in topology.segments.values():
            if seg.id in closed or seg.exertion > max_exertion:
                continue
            weight = seg.minutes * (1.0 + _ratio_of(estimates, seg.id))
            if seg.src == node:
                yield seg.dst, seg.id, weight, False
            if not seg.one_way and seg.dst == node:
                yield seg.src, seg.id, weight, False

    result = _dijkstra(topology, start, {goal}, edge_iter)
    if result is None:
        return {"ok": False, "reason": "no_route", "language": lang}
    _, cost, legs = result
    template = INSTRUCTION_TEMPLATES[lang]
    instructions = []
    for segment_id, _contra in legs:
        seg = topology.segments[segment_id]
        instructions.append(
            template.format(
                name=localize(seg.name, lang),
                to=localize(topology.nodes[seg.dst].name, lang),
            )
        )
    return {
        "ok": True,
        "from": start,
        "to": goal,
        "language": lang,
        "segments": [sid for sid, _ in legs],
        "minutes": round(cost, 1),
        "max_exertion": max_exertion,
        "instructions": instructions,
    }


def evacuation_plan(topology, estimates, closed_hard, danger_elements, now):
    """为仍有游客的危险元素生成疏散路径，按占用人数降序优先。

    closed_hard 只包含物理上不可通行的路段（设施停运、关闭窗口）；
    气象与管制类软关闭不阻挡撤离。单向路段允许逆行并标记 contra_flow。
    """
    exits = topology.exits()

    def edge_iter(node):
        for seg in topology.segments.values():
            if seg.id in closed_hard:
                continue
            if seg.src == node:
                yield seg.dst, seg.id, seg.minutes, False
            elif seg.dst == node:
                # 疏散时允许逆行单向路段
                yield seg.src, seg.id, seg.minutes, seg.one_way

    plans = []
    for element in danger_elements:
        estimate = estimates.get(element)
        if estimate is None or estimate.value <= 0:
            continue
        best = None
        for start in topology.node_ids_of(element):
            result = _dijkstra(topology, start, exits, edge_iter)
            if result is None:
                continue
            exit_node, cost, legs = result
            if best is None or cost < best[1]:
                best = (exit_node, cost, legs)
        if best is None:
            continue
        exit_node, cost, legs = best
        plans.append(
            {
                "element": element,
                "occupancy": round(estimate.value, 1),
                "confidence": estimate.confidence,
                "exit": exit_node,
                "minutes": round(cost, 1),
                "route": [sid for sid, _ in legs],
                "contra_flow": [sid for sid, contra in legs if contra],
            }
        )
    plans.sort(key=lambda plan: (-plan["occupancy"], plan["element"]))
    return plans
