"""路线与疏散：单向、关闭窗口、体力、管制、疏散优先顺序。"""

import unittest

from restrictions import RestrictionBoard
from routing import RoutingContext, plan_route
from topology import DIRECTION_ONEWAY, Topology
from events import Event

# a -- 单向向上 --> b -- 双向险路(fitness 4) --> summit
# a -- 平缓绕行 --> c --平缓--> summit
# b -- 下撤单向 --> shelter
TOPO = {
    "nodes": [
        {"id": "a", "kind": "plaza", "name": {"zh": "甲"}},
        {"id": "b", "kind": "station", "name": {"zh": "乙"}},
        {"id": "c", "kind": "plaza", "name": {"zh": "丙"}},
        {"id": "summit", "kind": "summit", "safe": False, "name": {"zh": "顶"}},
        {"id": "shelter", "kind": "shelter", "name": {"zh": "避雨点", "en": "Shelter"}},
    ],
    "edges": [
        {"id": "up", "src": "a", "dst": "b", "kind": "cableway",
         "direction": DIRECTION_ONEWAY, "fitness": 1, "travel_min": 10,
         "capacity": 100, "name": {"zh": "上行索道", "en": "Up Cable"}},
        {"id": "hard", "src": "b", "dst": "summit", "kind": "trail", "fitness": 4,
         "direction": DIRECTION_ONEWAY,
         "travel_min": 20, "capacity": 100, "name": {"zh": "险路", "en": "Hard"}},
        {"id": "detour1", "src": "a", "dst": "c", "kind": "trail", "fitness": 1,
         "travel_min": 8, "capacity": 100, "name": {"zh": "绕行一", "en": "D1"}},
        {"id": "detour2", "src": "c", "dst": "summit", "kind": "trail", "fitness": 2,
         "direction": DIRECTION_ONEWAY,
         "travel_min": 8, "capacity": 100,
         "closures": [{"start": 600, "end": 660, "reason": "窗口关闭"}],
         "name": {"zh": "绕行二", "en": "D2"}},
        {"id": "down", "src": "b", "dst": "shelter", "kind": "trail",
         "direction": DIRECTION_ONEWAY, "fitness": 1, "travel_min": 5,
         "capacity": 100, "name": {"zh": "下撤道", "en": "Down"}},
    ],
}


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.topo = Topology(TOPO)

    def ctx(self, minute=300, fitness=4, restrictions=None):
        return RoutingContext(self.topo, minute, max_fitness=fitness,
                              restrictions=restrictions, langs=["zh", "en"])

    def test_oneway_cannot_be_reversed(self):
        plan = plan_route(self.topo, "b", "a", self.ctx())
        self.assertFalse(plan["feasible"])

    def test_fitness_limits_route_to_detour(self):
        plan = plan_route(self.topo, "a", "summit", self.ctx(fitness=2))
        self.assertTrue(plan["feasible"])
        self.assertEqual([s["edge_id"] for s in plan["steps"]], ["detour1", "detour2"])
        self.assertLessEqual(plan["fitness_required"], 2)

    def test_closure_window_blocks(self):
        blocked = plan_route(self.topo, "a", "summit", self.ctx(minute=630, fitness=2))
        self.assertFalse(blocked["feasible"])
        open_plan = plan_route(self.topo, "a", "summit", self.ctx(minute=700, fitness=2))
        self.assertTrue(open_plan["feasible"])

    def test_multilingual_steps(self):
        plan = plan_route(self.topo, "a", "b", self.ctx())
        names = {n["lang"]: n["text"] for n in plan["steps"][0]["names"]}
        self.assertEqual(names["zh"], "上行索道")
        self.assertEqual(names["en"], "Up Cable")

    def test_hold_blocks_normal_route_but_evacuation_can_exit(self):
        board = RestrictionBoard()
        board.issue(Event("r", "restriction", 100, 100, {
            "restriction_id": "R1", "action": "hold", "edges": ["up"], "nodes": [],
            "reason": "检修", "issued_by": "op_a", "expires_at": 900}, "ops"))
        normal = plan_route(self.topo, "a", "b", self.ctx(restrictions=board))
        self.assertFalse(normal["feasible"])
        evac = RoutingContext(self.topo, 100, restrictions=board, evacuation=True)
        # 疏散时 hold 边允许顺向离开（a->b 是进入，但 down 是 b->shelter 的外撤）
        out = plan_route(self.topo, "b", "shelter", evac)
        self.assertTrue(out["feasible"])
        self.assertEqual(out["steps"][0]["edge_id"], "down")


if __name__ == "__main__":
    unittest.main()
