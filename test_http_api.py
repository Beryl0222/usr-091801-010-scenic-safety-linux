"""HTTP 契约：健康端点稳定、限流解释完整、写操作校验、404 行为。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from api import Api, AppState
from service import Handler, SERVICE_ID, health_payload


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = AppState("data/yunling_park.json")
        Handler.api = Api(cls.state)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        # 每个测试用独立的 live 状态，避免相互污染。
        self.state.reset_live("data/yunling_park.json")

    def get(self, path):
        with urlopen(f"{self.base}{path}", timeout=3) as response:
            return response.status, json.load(response)

    def post(self, path, body):
        data = json.dumps(body).encode()
        req = Request(f"{self.base}{path}", data=data,
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_health_payload_unchanged(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "service": SERVICE_ID,
                                "name": health_payload()["name"]})

    def test_rules_endpoint_explains_thresholds(self):
        _, body = self.get("/rules")
        ids = {r["rule_id"] for r in body["occupancy_rules"]}
        self.assertEqual(ids, {"OCC-EVAC-01", "OCC-CLOSE-02", "OCC-HOLD-03", "OCC-WATCH-04"})
        for rule in body["occupancy_rules"]:
            self.assertTrue(rule["release"])
        self.assertIn("阈值", "".join(body["principles"]))

    def test_flow_limit_explains_rule_paths_and_release(self):
        self.post("/events", {
            "id": "h1", "type": "gate_count", "occurred_at": 600, "observed_at": 600,
            "source": "G-EAST",
            "payload": {"gate_id": "gate_east", "entries": 400, "exits": 0}})
        self.post("/events", {
            "id": "h2", "type": "zone_count", "occurred_at": 600, "observed_at": 600,
            "source": "Z-NORTH", "payload": {"ref": "node:transfer_north", "count": 210}})
        _, measures = self.get("/measures")
        active = [m for m in measures["measures"] if m["status"] == "active"]
        self.assertTrue(active)
        rule = active[0]["rule"]
        # 每次限流必须说清：规则、证据、受影响路径、解除依据。
        self.assertTrue(rule["rule_id"] and rule["rule_version"])
        self.assertIn("evidence", rule)
        rid = active[0]["restriction_id"]
        _, detail = self.get(f"/restrictions/{rid}")
        self.assertEqual(detail["nodes"], ["transfer_north"])
        self.assertTrue(detail["release_basis"])
        self.assertIn("双人", detail["release_state"])

    def test_unknown_evidence_requires_confirmation(self):
        self.post("/events", {
            "id": "s1", "type": "zone_count", "occurred_at": 600, "observed_at": 600,
            "source": "Z-RIDGE", "payload": {"ref": "edge:e_ridge_north", "count": 48}})
        for i, minute in enumerate(range(605, 635, 5)):
            self.post("/events", {
                "id": f"g{i}", "type": "gate_count", "occurred_at": minute,
                "observed_at": minute, "source": "G-EAST",
                "payload": {"gate_id": "gate_east", "entries": 1, "exits": 0}})
        _, measures = self.get("/measures")
        pending = [m for m in measures["measures"]
                   if m["status"] == "pending_confirmation" and m["ref"] == "edge:e_ridge_north"]
        self.assertTrue(pending)
        # 未确认前不得存在该边的生效管制。
        _, restrictions = self.get("/restrictions")
        self.assertFalse(
            any(r["status"] == "active" and "e_ridge_north" in r["edges"]
                for r in restrictions["restrictions"]))

    def test_manual_restriction_requires_expiry(self):
        status, body = self.post("/restrictions", {
            "restriction_id": "R-X", "issued_by": "op_a", "reason": "风大",
            "edges": ["e_extreme_up"]})
        self.assertEqual(status, 400)
        self.assertIn("expires_at", body["error"])

    def test_replay_is_side_effect_free_for_live(self):
        _, before = self.get("/state")
        status, body = self.post("/replay", {"scenario": "storm_recovery"})
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["events_ingested"], 27)
        _, after = self.get("/state")
        self.assertEqual(before["events_in_log"], after["events_in_log"])

    def test_unknown_route_404(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
