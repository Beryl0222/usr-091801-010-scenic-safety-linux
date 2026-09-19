"""景区拓扑：节点与有向边。

节点 = 闸机入口、广场、换乘点、索道站、观景台等；
边 = 步行路段（可单向）、索道/天梯/接驳班线（有容量与发班间隔）。

每条边可声明体力等级、容量（人）、计划关闭窗口与多语种名称，
路线推荐与疏散判断都只读这一份拓扑。
"""

from dataclasses import dataclass, field

# 体力等级 1（平缓）.. 5（极限路段，如天梯冲刺段）。
MIN_FITNESS = 1
MAX_FITNESS = 4

DIRECTION_ONEWAY = "oneway"
DIRECTION_BIDIR = "bidir"


@dataclass(frozen=True)
class ClosureWindow:
    """计划关闭窗口（半开区间 [start, end)），如夜间演出布场。"""

    start: int
    end: int
    reason: str = ""

    def active_at(self, minute: int) -> bool:
        if self.start <= self.end:
            return self.start <= minute < self.end
        # 允许跨零点窗口，如 21:30-02:00
        return minute >= self.start or minute < self.end


@dataclass(frozen=True)
class Node:
    node_id: str
    name: dict  # {"zh": "东入口", "en": "East Gate", ...}
    kind: str  # gate/plaza/transfer/station/summit/shelter/exitzone
    safe: bool = True  # 庇护点/出口/广场视为安全集结点
    exit_point: bool = False
    capacity: int = 0  # 换乘点/广场的瞬时承载；0 表示不约束


@dataclass(frozen=True)
class Edge:
    edge_id: str
    name: dict
    src: str
    dst: str
    kind: str  # trail/cableway/ladder/shuttle
    direction: str = DIRECTION_BIDIR
    capacity: int = 0  # 同时在段人数上限；0 表示不做在段容量约束
    fitness: int = 1
    headway: int = 0  # 发班/通过间隔（分钟），索道/接驳用
    throughput_per_min: float = 0.0  # 每分钟最大通过能力
    travel_min: int = 10  # 穿越该边的典型用时（分钟）
    closures: tuple[ClosureWindow, ...] = field(default_factory=tuple)

    def is_oneway(self) -> bool:
        return self.direction == DIRECTION_ONEWAY

    def usable(self, minute: int) -> bool:
        """计划关闭窗口内不可用（临时管制由路由层另行判定）。"""
        return not any(window.active_at(minute) for window in self.closures)


class Topology:
    def __init__(self, raw: dict):
        self.raw = raw
        self.park_id = raw.get("park_id", "park")
        self.nodes: dict[str, Node] = {}
        for item in raw.get("nodes", []):
            self.nodes[item["id"]] = Node(
                node_id=item["id"],
                name=item.get("name", {"zh": item["id"]}),
                kind=item.get("kind", "zone"),
                safe=item.get("safe", True),
                exit_point=item.get("exit_point", False),
                capacity=int(item.get("capacity", 0)),
            )
        self.edges: dict[str, Edge] = {}
        self.adjacency: dict[str, list[Edge]] = {nid: [] for nid in self.nodes}
        for item in raw.get("edges", []):
            closures = tuple(
                ClosureWindow(
                    start=c["start"], end=c["end"], reason=c.get("reason", "")
                )
                for c in item.get("closures", [])
            )
            edge = Edge(
                edge_id=item["id"],
                name=item.get("name", {"zh": item["id"]}),
                src=item["src"],
                dst=item["dst"],
                kind=item.get("kind", "trail"),
                direction=item.get("direction", DIRECTION_BIDIR),
                capacity=int(item.get("capacity", 0)),
                fitness=max(MIN_FITNESS, min(MAX_FITNESS, int(item.get("fitness", 1)))),
                headway=int(item.get("headway", 0)),
                throughput_per_min=float(item.get("throughput_per_min", 0.0)),
                travel_min=int(item.get("travel_min", 10)),
                closures=closures,
            )
            self.edges[edge.edge_id] = edge
            self.adjacency.setdefault(edge.src, []).append(edge)
            if not edge.is_oneway():
                # 反向边复用同一 id：封锁/占用对两个方向同时生效。
                reverse = Edge(
                    edge_id=edge.edge_id,
                    name=edge.name,
                    src=edge.dst,
                    dst=edge.src,
                    kind=edge.kind,
                    direction=DIRECTION_BIDIR,
                    capacity=edge.capacity,
                    fitness=edge.fitness,
                    headway=edge.headway,
                    throughput_per_min=edge.throughput_per_min,
                    travel_min=edge.travel_min,
                    closures=edge.closures,
                )
                self.adjacency.setdefault(reverse.src, []).append(reverse)

    def edge_objects(self):
        return self.edges.values()

    def exits(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.exit_point]

    def shelters(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.safe]

    def incident(self, node_id: str) -> list[Edge]:
        return self.adjacency.get(node_id, [])

    def predecessors(self, node_id: str) -> list[Edge]:
        """所有沿通行方向汇入 node 的定向边（用于确定危险段上游截留点）。"""
        return [
            arc
            for arcs in self.adjacency.values()
            for arc in arcs
            if arc.dst == node_id
        ]

    def localize(self, value: dict, langs: list[str]) -> dict:
        """按偏好语言返回名称，缺失时回退到 zh / en / 任意值。"""
        for lang in langs:
            if lang in value:
                return {"lang": lang, "text": value[lang]}
        for fallback in ("zh", "en"):
            if fallback in value:
                return {"lang": fallback, "text": value[fallback]}
        lang, text = next(iter(value.items()), ("?", ""))
        return {"lang": lang, "text": text}
