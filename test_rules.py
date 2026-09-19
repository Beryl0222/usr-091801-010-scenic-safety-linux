"""规则引擎：阈值对 high 比较、不确定折减、气象与设施硬规则。"""

import unittest

from estimator import ZoneEstimate
from rules import (
    ACTION_CONFIRM,
    ACTION_EVACUATE,
    ACTION_HOLD_ENTRY,
    ACTION_MONITOR,
    RULEBOOK_VERSION,
    evaluate_facility,
    evaluate_zone,
)
from topology import Topology
from weather import WeatherBoard
from events import Event, TYPE_WEATHER_ALERT

TOPO = {
    "nodes": [{"id": "a", "kind": "zone", "name": {"zh": "A"}},
              {"id": "b", "kind": "zone", "name": {"zh": "B"}}],
    "edges": [{"id": "e1", "src": "a", "dst": "b", "kind": "trail", "capacity": 100,
               "name": {"zh": "险路"}}],
}


def est(ref="edge:e1", low=0, point=0, high=0, confidence=0.95, fresh="fresh", uncertain=False):
    return ZoneEstimate(ref, low, point, high, confidence, fresh, uncertain, ["f"], "direct")


def weather(level, refs=("e1",), hazard="rainstorm"):
    board = WeatherBoard()
    board.raise_alert(
        Event("w", TYPE_WEATHER_ALERT, 100, 100,
              {"level": level, "hazard": hazard, "scope": "edge", "refs": list(refs),
               "message": "m"}, "met")
    )
    return board


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.topo = Topology(TOPO)
        self.clear_sky = WeatherBoard()

    def test_threshold_compares_high_not_point(self):
        # 点估计 66%（看似安全），但区间上界 72% 已过 70% 截留线。
        hit = evaluate_zone(self.topo, est(point=66, high=72, confidence=0.95),
                            self.clear_sky, 100)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.rule_id, "OCC-HOLD-03")
        self.assertFalse(hit.requires_confirmation)

    def test_uncertain_evidence_uses_discounted_threshold_and_confirmation(self):
        # high=67% 不过正常 70% 线；late 折减后阈值 63%，应触发并请求人工确认。
        hit = evaluate_zone(self.topo,
                            est(point=64, high=67, confidence=0.7, fresh="late", uncertain=True),
                            self.clear_sky, 100)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.conservative)
        self.assertTrue(hit.requires_confirmation)
        self.assertEqual(hit.action, ACTION_CONFIRM)
        self.assertEqual(hit.rule_version, RULEBOOK_VERSION)
        self.assertEqual(hit.affected_edges, ["e1"])
        self.assertTrue(hit.release_basis)

    def test_below_all_thresholds_no_hit(self):
        self.assertIsNone(
            evaluate_zone(self.topo, est(point=10, high=12, confidence=0.95),
                          self.clear_sky, 100)
        )

    def test_warning_closes_and_severe_evacuates(self):
        warn = evaluate_zone(self.topo, est(high=10), weather("warning"), 100)
        self.assertEqual(warn.action, "block_edge")
        severe = evaluate_zone(self.topo, est(high=10), weather("severe"), 100)
        self.assertEqual(severe.action, ACTION_EVACUATE)

    def test_weather_scope_does_not_touch_other_edge(self):
        board = weather("severe", refs=("e9",))
        self.assertIsNone(evaluate_zone(self.topo, est(high=10), board, 100))

    def test_facility_closed_is_hard_rule(self):
        from estimator import FacilityState
        fac = FacilityState(ref="e1", status="closed", changed_at=90)
        hit = evaluate_facility(self.topo, "e1", fac, est(high=5), 100)
        self.assertEqual(hit.rule_id, "FAC-STOP-05")
        self.assertFalse(hit.requires_confirmation)
        fac.status = "open"
        self.assertIsNone(evaluate_facility(self.topo, "e1", fac, est(high=5), 100))


if __name__ == "__main__":
    unittest.main()
