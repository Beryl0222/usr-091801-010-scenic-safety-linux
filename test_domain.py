"""领域行为测试：置信度、保守阈值、管制、路线、疏散与回放。"""

import unittest
from pathlib import Path

from scenic.controls import ControlError, ControlRegistry
from scenic.dispatch import DispatchService
from scenic.model import load_topology
from scenic.replay import run_replay
from scenic.util import parse_time

DATA = Path(__file__).parent / "data"
T0 = parse_time("2026-09-19T08:00:00Z")


def make_service():
    return DispatchService(load_topology(DATA / "topology.json"))


def event(type_, observed, received=None, **payload):
    return {"type": type_, "observed_at": observed, "received_at": received or observed, **payload}


class OccupancyConfidenceTest(unittest.TestCase):
    def test_fresh_multi_source_data_has_high_confidence(self):
        svc = make_service()
        svc.ingest(event("turnstile_count", T0, element="s_trail_up", **{"in": 100, "out": 10}))
        svc.ingest(event("location_segment", T0, element="s_trail_up", count=95))
        estimate = svc.estimator.estimate("s_trail_up", T0 + 30)
        self.assertGreaterEqual(estimate.confidence, 0.9)
        self.assertFalse(estimate.uncertain)

    def test_stale_data_and_offline_sensor_degrade_confidence(self):
        svc = make_service()
        svc.ingest(event("turnstile_count", T0, element="s_trail_up", **{"in": 100, "out": 0}))
        svc.ingest(event("location_segment", T0, element="s_trail_up", count=95))
        svc.ingest(event("sensor_status", T0, element="s_trail_up", online=False))
        estimate = svc.estimator.estimate("s_trail_up", T0 + 2000)
        self.assertLess(estimate.confidence, 0.2)
        self.assertTrue(any("失联" in note for note in estimate.notes))

    def test_late_event_is_flagged_not_silently_absorbed(self):
        svc = make_service()
        result = svc.ingest(
            event("turnstile_count", T0, T0 + 400, element="s_trail_up", **{"in": 50, "out": 0})
        )
        self.assertTrue(any("迟到" in note for note in result["notes"]))

    def test_disagreeing_sources_take_conservative_upper_bound(self):
        svc = make_service()
        svc.ingest(event("turnstile_count", T0, element="s_ridge", **{"in": 40, "out": 0}))
        svc.ingest(event("location_segment", T0, element="s_ridge", count=110))
        estimate = svc.estimator.estimate("s_ridge", T0 + 30)
        self.assertEqual(estimate.value, 110)  # 取保守上界
        self.assertTrue(any("分歧" in note for note in estimate.notes))
        self.assertLess(estimate.confidence, 0.7)


class ConservativeThresholdTest(unittest.TestCase):
    def _fill_ridge(self, svc, online=True):
        svc.ingest(event("turnstile_count", T0, element="s_ridge", **{"in": 96, "out": 0}))
        svc.ingest(event("location_segment", T0, element="s_ridge", count=96))
        if not online:
            svc.ingest(event("sensor_status", T0, element="s_ridge", online=False))

    def test_high_confidence_uses_normal_threshold_no_alarm_at_80pct(self):
        svc = make_service()
        self._fill_ridge(svc)
        decisions = svc.evaluate(T0 + 30)
        occupancy = [d for d in decisions if d["rule"] == "segment-occupancy"]
        self.assertEqual(occupancy, [])  # 0.8 < 0.9 常规阈值

    def test_low_confidence_triggers_manual_confirmation_at_80pct(self):
        svc = make_service()
        self._fill_ridge(svc, online=False)
        decisions = svc.evaluate(T0 + 30)
        occupancy = [d for d in decisions if d["rule"] == "segment-occupancy"]
        self.assertEqual(len(occupancy), 1)
        decision = occupancy[0]
        self.assertEqual(decision["level"], "manual_confirm")
        self.assertTrue(decision["requires_confirmation"])
        self.assertLess(decision["threshold_ratio"], 0.9)
        # 决策必须说清规则、受影响路径与解除依据
        self.assertEqual(decision["affected_paths"], ["s_ridge"])
        self.assertIn("置信度", decision["basis"])
        self.assertTrue(decision["release_basis"])


class ControlTest(unittest.TestCase):
    def test_control_requires_expiry(self):
        registry = ControlRegistry()
        with self.assertRaises(ControlError):
            registry.impose("C-1", "op-a", "测试", ["s_trail_up"], T0, None)

    def test_dual_release_and_expiry(self):
        registry = ControlRegistry()
        registry.impose("C-1", "op-a", "暴雨", ["s_trail_up"], T0, T0 + 3600)
        self.assertEqual(len(registry.active_controls(T0 + 10)), 1)
        with self.assertRaises(ControlError):
            registry.release("C-1", "op-a", T0 + 20)  # 发起人不能解除
        registry.release("C-1", "op-b", T0 + 30)
        self.assertEqual(len(registry.active_controls(T0 + 40)), 1)  # 一人不够
        registry.release("C-1", "op-c", T0 + 50)
        self.assertEqual(len(registry.active_controls(T0 + 60)), 0)  # 双人解除
        # 到期自动失效
        registry.impose("C-2", "op-a", "演练", ["s_ridge"], T0, T0 + 100)
        self.assertEqual(len(registry.active_controls(T0 + 200)), 0)


class RoutingTest(unittest.TestCase):
    def test_one_way_segments_not_traversed_backwards(self):
        svc = make_service()
        route = svc.recommend("summit", "exit_a", {"language": "zh", "max_exertion": 5}, now=T0)
        self.assertTrue(route["ok"])
        self.assertNotIn("s_ridge", route["segments"])
        self.assertNotIn("s_trail_up", route["segments"])
        self.assertIn("s_summit_down", route["segments"])

    def test_exertion_level_filters_routes(self):
        svc = make_service()
        svc.ingest(event("facility_status", T0, facility="F1", status="down"))
        easy = svc.recommend("gate_a", "summit", {"max_exertion": 1}, now=T0)
        self.assertFalse(easy["ok"])  # 索道停运后，体力1级无路上山
        fit = svc.recommend("gate_a", "summit", {"max_exertion": 2}, now=T0)
        self.assertTrue(fit["ok"])
        self.assertIn("s_ladder_up", fit["segments"])

    def test_closure_window_blocks_night_venue_by_day(self):
        svc = make_service()
        daytime = svc.recommend("hub_t1", "night_plaza", {}, now=parse_time("2026-09-19T10:00:00Z"))
        self.assertFalse(daytime["ok"])
        evening = svc.recommend("hub_t1", "night_plaza", {}, now=parse_time("2026-09-19T18:00:00Z"))
        self.assertTrue(evening["ok"])

    def test_multilingual_instructions(self):
        svc = make_service()
        route = svc.recommend("gate_a", "summit", {"language": "en", "max_exertion": 1}, now=T0)
        self.assertTrue(route["ok"])
        self.assertTrue(any(step.startswith("Take ") for step in route["instructions"]))
        route_ja = svc.recommend("gate_a", "summit", {"language": "ja", "max_exertion": 1}, now=T0)
        self.assertTrue(any("で" in step for step in route_ja["instructions"]))


class WeatherAndEvacuationTest(unittest.TestCase):
    def _storm_service(self):
        svc = make_service()
        svc.ingest(event("location_segment", T0, element="s_ridge", count=126))
        svc.ingest(
            event(
                "weather_alert", T0,
                alert_id="W-1", zones=["ridge"], severity="red",
                start="2026-09-19T08:00:00Z", end="2026-09-19T11:00:00Z",
            )
        )
        return svc

    def test_weather_alert_closes_zone_and_explains(self):
        svc = self._storm_service()
        decisions = svc.evaluate(T0 + 60)
        weather = [d for d in decisions if d["rule"] == "weather-zone-closure"]
        self.assertEqual(len(weather), 1)
        self.assertEqual(weather[0]["affected_paths"], ["s_ridge"])
        self.assertIn("W-1", weather[0]["basis"])
        self.assertIn("11:00", weather[0]["release_basis"])

    def test_route_avoids_storm_zone(self):
        svc = self._storm_service()
        route = svc.recommend("gate_a", "summit", {"max_exertion": 5}, now=T0 + 60)
        self.assertTrue(route["ok"])
        self.assertNotIn("s_ridge", route["segments"])

    def test_people_in_danger_zone_get_evacuation_priority(self):
        svc = self._storm_service()
        decisions = svc.evaluate(T0 + 60)
        evacuation = [d for d in decisions if d["rule"] == "evacuation-priority"]
        self.assertEqual(len(evacuation), 1)
        plan = evacuation[0]["plan"]
        self.assertEqual(plan[0]["element"], "s_ridge")
        self.assertEqual(plan[0]["occupancy"], 126)
        self.assertEqual(plan[0]["exit"], "exit_a")
        self.assertIn("s_ridge", evacuation[0]["affected_paths"])

    def test_evacuation_may_reverse_one_way_segment(self):
        svc = make_service()
        svc.ingest(event("location_segment", T0, element="trail_junction", count=390))
        svc.evaluate(T0 + 30)
        plan = [p for p in svc.evacuation if p["element"] == "trail_junction"]
        self.assertEqual(len(plan), 1)
        self.assertIn("s_trail_up", plan[0]["contra_flow"])

    def test_facility_outage_and_recovery(self):
        svc = make_service()
        svc.ingest(event("facility_status", T0, facility="F1", status="down"))
        outage = [d for d in svc.decisions if d["rule"] == "facility-outage"]
        self.assertEqual(outage[0]["affected_paths"], ["s_cable_up", "s_cable_down"])
        svc.ingest(event("facility_status", T0 + 600, facility="F1", status="up"))
        self.assertEqual([d for d in svc.decisions if d["rule"] == "facility-outage"], [])


class ReplayTest(unittest.TestCase):
    def test_replay_is_deterministic_and_restart_consistent(self):
        first = run_replay(DATA / "topology.json", DATA / "events.json")
        second = run_replay(DATA / "topology.json", DATA / "events.json")
        self.assertTrue(first["restart_consistent"])
        self.assertEqual(
            [d["id"] for d in first["final_decisions"]],
            [d["id"] for d in second["final_decisions"]],
        )
        self.assertEqual(len(first["timeline"]), 15)

    def test_replay_covers_peak_outage_recovery_and_dual_release(self):
        report = run_replay(DATA / "topology.json", DATA / "events.json")
        final_rules = {d["rule"] for d in report["final_decisions"]}
        # 设施已恢复、管制已双人解除 → 最终不应再有这两类决策
        self.assertNotIn("facility-outage", final_rules)
        self.assertNotIn("operator-control", final_rules)
        # 暴雨红色告警窗口内，山脊仍有游客 → 疏散优先仍然有效
        self.assertIn("evacuation-priority", final_rules)
        # 回放中出现过设施停运与管制
        seen = {d["id"] for step in report["timeline"] for d in step["decisions"]}
        self.assertIn("facility-outage:F1", seen)
        self.assertIn("operator-control:C-1", seen)
        # 迟到事件被标记而非静默吸收
        notes = [n for step in report["timeline"] for n in step["notes"]]
        self.assertTrue(any("迟到" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
