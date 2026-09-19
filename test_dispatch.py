"""端到端：三个场景的安全语义、重启重建、回放确定性、自动解除。"""

import json
import unittest

from dispatch import Dispatch
from events import parse_event
from replay import build_from_files, load_scenario, run_replay

SCENARIO_DIR = "data/scenarios"


def play(name):
    header, events = load_scenario(f"{SCENARIO_DIR}/{name}.jsonl")
    topo, feeds = build_from_files(header["topology"])
    return header, events, topo, feeds, run_replay(topo, feeds, events)


class PeakScenarioTest(unittest.TestCase):
    def test_late_out_of_order_and_stale_are_counted(self):
        _, _, _, _, report = play("peak_day")
        summary = report["summary"]
        self.assertEqual(summary["late_events"], 2)
        self.assertEqual(summary["out_of_order_events"], 1)
        self.assertEqual(summary["parse_errors"], [])

    def test_fresh_overcrowding_auto_holds_then_times_out(self):
        _, events, topo, feeds, report = play("peak_day")
        # 10:30 北换乘中心 196/260=75% 新鲜读数 -> 自动截留管制。
        at_630 = next(t for t in report["timeline"] if t["event_id"] == "p13")
        self.assertIn("node:transfer_north",
                      [h["ref"] for h in at_630["hit_rules"]])
        auto = [
            r for t in report["timeline"] for r in t["changes"]["new_restrictions"]
            if r["issued_by"] == "system:auto"
        ]
        self.assertTrue(auto, "峰值场景应产生至少一条系统自动管制")
        for r in auto:
            self.assertGreater(r["expires_at"], 0)

    def test_stale_sensors_become_pending_not_enforced(self):
        _, _, _, _, report = play("peak_day")
        # 11:16 结束时多个山脊馈源失联超 20 分钟：只可挂起人工确认，
        # 不得再产生新的自动疏散管制。
        final = report["final"]
        pending_refs = {m["ref"] for m in final["measures"]
                        if m["status"] == "pending_confirmation"}
        self.assertTrue(pending_refs)
        for measure in final["measures"]:
            if measure["status"] == "active":
                self.assertTrue(measure["restriction_id"])

    def test_every_restriction_carries_rule_and_release_basis(self):
        _, _, _, _, report = play("peak_day")
        for r in report["final"]["restrictions"]:
            self.assertTrue(r["rule_id"])
            self.assertIn("release_state", r)


class StormScenarioTest(unittest.TestCase):
    def test_severe_alert_generates_priority_evacuation(self):
        header, events, topo, feeds, _ = play("storm_recovery")
        dispatch = Dispatch(topo, feeds)
        for raw in events:
            dispatch.ingest(parse_event(raw))
            if raw["id"] == "s13":  # 818 severe 告警入库后立即取快照
                break
        plan = dispatch.evacuation_plan(["zh", "en"], 818)
        severe = [o for o in plan["orders"] if o["category"] == "severe"]
        self.assertEqual({o["edge_id"] for o in severe},
                         {"e_cliff_trail", "e_ridge_north", "e_ladder_up"})
        # 已在危险区段的人优先于封闭/截留。
        priorities = [o["priority"] for o in plan["orders"]]
        self.assertEqual(priorities, sorted(priorities, reverse=True))
        # 每条疏散令都有多语种广播与就近庇护点。
        for order in severe:
            self.assertTrue(order["shelter_id"])
            langs = {b["lang"] for b in order["broadcast"]}
            self.assertEqual(langs, {"zh", "en"})
            self.assertGreaterEqual(order["people_inside_range"][1],
                                    order["people_inside_range"][0])

    def test_manual_restriction_needs_two_distinct_non_issuers(self):
        _, _, _, _, report = play("storm_recovery")
        ops = next(r for r in report["final"]["restrictions"]
                   if r["restriction_id"] == "R-OPS-001")
        self.assertEqual(ops["status"], "released")
        self.assertEqual(ops["issued_by"], "op_chen")
        release = ops["releases"][0]
        self.assertEqual(release["by"], "op_li")
        self.assertEqual(release["witness"], "op_wang")
        self.assertNotIn("op_chen", (release["by"], release["witness"]))

    def test_cable_closing_and_reopening_lifecycle(self):
        _, _, _, _, report = play("storm_recovery")
        cable = [m for m in report["final"]["measures"]
                 if m["rule"]["rule_id"] == "FAC-STOP-05"]
        self.assertTrue(cable)
        # 索道恢复后系统按规则自动解除封控。
        self.assertTrue(any(m["status"] == "auto_released" for m in cable))
        facilities = {f["facility_id"]: f for f in report["final"]["facilities"]}
        self.assertEqual(facilities["e_cable_up"]["status"], "open")

    def test_end_state_clear(self):
        _, _, _, _, report = play("storm_recovery")
        self.assertEqual(report["summary"]["active_restrictions"], 0)
        self.assertEqual(report["final"]["alerts"], [])


class RestartScenarioTest(unittest.TestCase):
    def test_bad_event_line_is_recorded_not_fatal(self):
        _, _, _, _, report = play("restart_chaos")
        errors = report["summary"]["parse_errors"]
        self.assertEqual(len(errors), 1)
        self.assertIn("drone_sighting", errors[0]["error"])

    def test_manual_short_restriction_expires_before_restart(self):
        _, _, _, _, report = play("restart_chaos")
        r = next(x for x in report["final"]["restrictions"]
                 if x["restriction_id"] == "R-OPS-010")
        self.assertEqual(r["status"], "expired")  # 760 到期，900 才重启

    def test_restart_rebuilds_from_log_deterministically(self):
        _, _, _, _, report = play("restart_chaos")
        self.assertEqual(report["summary"]["restarts"], 1)
        restart_event = next(t for t in report["timeline"] if t["ingest"].get("rebuilt"))
        # 重启前已生效（未到期）的管制集合，必须与日志重建后完全一致；
        # 已到期的人工短时管制（R-OPS-010，760 到期）两边都不在。
        self.assertEqual(
            restart_event["ingest"]["active_restrictions_before"],
            restart_event["ingest"]["active_restrictions_after"],
        )
        self.assertNotIn("R-OPS-010", restart_event["ingest"]["active_restrictions_after"])
        self.assertGreaterEqual(restart_event["ingest"]["events_replayed"], 10)
        # 重放两遍：最终状态必须完全一致（无随机编号、无内存残留依赖）。
        _, _, _, _, second = play("restart_chaos")
        self.assertEqual(
            json.dumps(report["final"], sort_keys=True, ensure_ascii=False),
            json.dumps(second["final"], sort_keys=True, ensure_ascii=False),
        )

    def test_night_peak_auto_measure_releases_by_rule(self):
        _, _, _, _, report = play("restart_chaos")
        # 夜场进场通道 350/420=83%（折减后触发），散场后占用回落，
        # 系统措施应按解除依据自动解除，而非拖到 TTL。
        stage = [m for m in report["final"]["measures"]
                 if m["ref"] == "edge:e_south_stage" and m["action"] != "monitor"]
        statuses = [m["status"] for m in stage]
        self.assertIn("auto_released", statuses)


class RoutingIntegrationTest(unittest.TestCase):
    def test_night_closure_window_visible_in_topology(self):
        topo, _ = build_from_files("data/yunling_park.json")
        edge = topo.edges["e_south_stage"]
        self.assertTrue(any(w.active_at(1280) for w in edge.closures))
        self.assertFalse(any(w.active_at(1320) for w in edge.closures))


if __name__ == "__main__":
    unittest.main()
