"""景区拓扑：节点、有向路段、设施与关闭窗口。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .util import parse_time


def localize(name, lang="zh"):
    """多语种名称回退：请求语种 → 英文 → 中文。"""
    if not isinstance(name, dict):
        return str(name)
    return name.get(lang) or name.get("en") or name.get("zh") or ""


@dataclass
class Node:
    id: str
    kind: str  # entrance / exit / hub / station / poi / venue
    name: dict
    capacity: float


@dataclass
class Segment:
    id: str
    src: str
    dst: str
    kind: str  # shuttle / walk / hike / cable / ladder
    name: dict
    capacity: float
    minutes: float
    exertion: int  # 体力等级 1-5
    one_way: bool = False
    zone: str | None = None  # 气象告警分区
    facility: str | None = None
    closure_windows: list = field(default_factory=list)

    def closed_at(self, now):
        """关闭窗口：绝对 ISO 区间，或每日 HH:MM 区间（UTC，可跨午夜）。"""
        for window in self.closure_windows:
            if "daily_start" in window:
                hm = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%H:%M")
                start, end = window["daily_start"], window["daily_end"]
                if start <= end:
                    if start <= hm < end:
                        return True
                elif hm >= start or hm < end:
                    return True
            elif parse_time(window["start"]) <= now < parse_time(window["end"]):
                return True
        return False


@dataclass
class Facility:
    id: str
    kind: str
    name: dict
    segments: list


class Topology:
    """景区静态结构；节点与路段统称为元素，都有容量上限。"""

    def __init__(self, nodes, segments, facilities):
        self.nodes = {n.id: n for n in nodes}
        self.segments = {s.id: s for s in segments}
        self.facilities = {f.id: f for f in facilities}
        for seg in segments:
            for endpoint in (seg.src, seg.dst):
                if endpoint not in self.nodes:
                    raise ValueError(f"路段 {seg.id} 引用了未知节点 {endpoint}")
        for fac in facilities:
            for sid in fac.segments:
                if sid not in self.segments:
                    raise ValueError(f"设施 {fac.id} 引用了未知路段 {sid}")

    @classmethod
    def from_dict(cls, data):
        nodes = [Node(n["id"], n["kind"], n.get("name", {}), float(n["capacity"])) for n in data["nodes"]]
        segments = [
            Segment(
                id=s["id"],
                src=s["src"],
                dst=s["dst"],
                kind=s["kind"],
                name=s.get("name", {}),
                capacity=float(s["capacity"]),
                minutes=float(s["minutes"]),
                exertion=int(s["exertion"]),
                one_way=bool(s.get("one_way", False)),
                zone=s.get("zone"),
                facility=s.get("facility"),
                closure_windows=list(s.get("closure_windows", [])),
            )
            for s in data["segments"]
        ]
        facilities = [
            Facility(f["id"], f["kind"], f.get("name", {}), list(f["segments"]))
            for f in data.get("facilities", [])
        ]
        return cls(nodes, segments, facilities)

    def element_ids(self):
        return list(self.nodes) + list(self.segments)

    def capacity_of(self, element_id):
        if element_id in self.segments:
            return self.segments[element_id].capacity
        return self.nodes[element_id].capacity

    def name_of(self, element_id, lang="zh"):
        element = self.segments.get(element_id) or self.nodes.get(element_id)
        return localize(element.name, lang) if element else element_id

    def node_ids_of(self, element_id):
        if element_id in self.segments:
            seg = self.segments[element_id]
            return [seg.src, seg.dst]
        return [element_id]

    def exits(self):
        return [n.id for n in self.nodes.values() if n.kind == "exit"]

    def segments_in_zones(self, zones):
        wanted = set(zones)
        return [s.id for s in self.segments.values() if s.zone in wanted]


def load_topology(path):
    with open(path, encoding="utf-8") as fh:
        return Topology.from_dict(json.load(fh))
