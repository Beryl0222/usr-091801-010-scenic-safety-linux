"""占用估计：新鲜度、迟到、失联未知化、乱序标记。"""

import unittest

from events import Feed
from estimator import FRESH, LATE, STALE, OccupancyEstimator
from topology import Topology

TOPO = {
    "nodes": [
        {"id": "g", "kind": "gate", "name": {"zh": "闸机"}},
        {"id": "n", "kind": "zone", "capacity": 100, "name": {"zh": "节点"}},
    ],
    "edges": [
        {"id": "e1", "src": "g", "dst": "n", "kind": "trail", "capacity": 100,
         "name": {"zh": "路一"}},
        {"id": "e2", "src": "n", "dst": "g", "kind": "trail", "capacity": 100,
         "name": {"zh": "路二"}},
    ],
}


def estimator():
    topo = Topology(TOPO)
    feeds = [
        Feed("f1", "zone_count", "edge:e1", late_after=4, stale_after=10),
        Feed("fg", "gate_count", "g", late_after=4, stale_after=10),
    ]
    return topo, OccupancyEstimator(topo, feeds)


def ev(eid, minute, count, observed=None, source="f1"):
    from events import Event, TYPE_ZONE_COUNT
    return Event(eid, TYPE_ZONE_COUNT, minute, observed if observed is not None else minute,
                 {"ref": "edge:e1", "count": count}, source)


class EstimatorTest(unittest.TestCase):
    def test_fresh_reading_gives_narrow_interval(self):
        _, est = estimator()
        est.apply(ev("a", 100, 50))
        z = est.estimate_zone("edge:e1", 100)
        self.assertEqual(z.fresh, FRESH)
        self.assertFalse(z.uncertain)
        self.assertGreaterEqual(z.point, z.low)
        self.assertLessEqual(z.high - z.low, 10)  # 3% 误差 + 2 人下限

    def test_late_arrival_is_flagged_and_wider(self):
        _, est = estimator()
        est.apply(ev("a", 100, 50, observed=110))  # 迟到 10 分钟
        z = est.estimate_zone("edge:e1", 110)
        self.assertEqual(z.fresh, LATE)
        self.assertTrue(z.uncertain)
        self.assertLess(z.confidence, 0.9)
        self.assertGreater(z.high - z.low, 5)

    def test_stale_reading_drifts_then_becomes_unknown(self):
        _, est = estimator()
        est.apply(ev("a", 100, 50))
        stale = est.estimate_zone("edge:e1", 113)  # 失联 13 分钟
        self.assertEqual(stale.fresh, STALE)
        self.assertTrue(stale.uncertain)
        lost = est.estimate_zone("edge:e1", 131)  # 失联 31 分钟 > stale+20
        self.assertEqual(lost.low, 0)
        self.assertEqual(lost.high, 100)  # 物理容量上界
        self.assertIn("lost_reading", lost.sources)
        self.assertLessEqual(lost.confidence, 0.15)

    def test_out_of_order_is_marked_not_silently_trusted(self):
        _, est = estimator()
        est.apply(ev("a", 120, 80))
        est.apply(ev("b", 100, 30))  # 乱序旧读数
        state = est.feeds["f1"]
        self.assertTrue(state.out_of_order)
        z = est.estimate_zone("edge:e1", 120)
        self.assertTrue(z.uncertain)

    def test_gate_totals_in_park(self):
        _, est = estimator()
        from events import Event, TYPE_GATE_COUNT
        est.apply(Event("g1", TYPE_GATE_COUNT, 0, 0, {"gate_id": "g", "entries": 100, "exits": 0}, "fg"))
        est.apply(Event("g2", TYPE_GATE_COUNT, 10, 10, {"gate_id": "g", "entries": 20, "exits": 30}, "fg"))
        self.assertEqual(est.in_park(), 90)

    def test_no_sensor_edge_is_ghost_but_not_observed(self):
        _, est = estimator()
        self.assertNotIn("edge:e2", est.observed_refs())
        z = est.estimate_zone("edge:e2", 100)
        self.assertEqual(z.basis, "inferred_from_gate_totals")
        self.assertTrue(z.uncertain)


if __name__ == "__main__":
    unittest.main()
