"""验证基础服务在业务模块开发前保持可运行，并覆盖值班席 HTTP 接口契约。"""

import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import App, Handler, SERVICE_ID, SERVICE_NAME, health_payload


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(
            health_payload(),
            {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME},
        )

    def test_health_endpoint_returns_json(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertEqual(json.load(response), health_payload())

    def test_unknown_route_is_not_exposed(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


class ApiContractTest(unittest.TestCase):
    """值班席接口：事件汇入、决策解释、管制双人解除、路线推荐。"""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer

        Handler.app = App()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        Handler.app = None

    def _get(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return json.load(response)

    def _post(self, path, payload):
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def test_event_ingest_updates_occupancy_estimate(self):
        now = time.time()
        result = self._post(
            "/api/events",
            {
                "type": "turnstile_count",
                "observed_at": now,
                "received_at": now,
                "element": "s_gate_t1",
                "in": 120,
                "out": 0,
            },
        )
        self.assertEqual(result["ingested"], 1)
        state = self._get("/api/state")
        estimate = next(e for e in state["estimates"] if e["element"] == "s_gate_t1")
        self.assertEqual(estimate["value"], 120)
        self.assertGreater(estimate["confidence"], 0)

    def test_control_requires_expiry_and_dual_release(self):
        with self.assertRaises(HTTPError) as error:
            self._post(
                "/api/controls",
                {"initiator": "op-a", "reason": "测试", "elements": ["s_trail_up"]},
            )
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

        expires = time.time() + 600
        control = self._post(
            "/api/controls",
            {
                "control_id": "C-HTTP-1",
                "initiator": "op-a",
                "reason": "山脊落石风险",
                "elements": ["s_trail_up"],
                "expires_at": expires,
            },
        )
        self.assertTrue(control["active"])

        decisions = self._get("/api/decisions")["decisions"]
        decision = next(d for d in decisions if d["rule"] == "operator-control")
        self.assertEqual(decision["affected_paths"], ["s_trail_up"])
        self.assertIn("op-a", decision["basis"])
        self.assertIn("两名独立操作员", decision["release_basis"])

        with self.assertRaises(HTTPError):
            self._post(
                "/api/controls/release",
                {"control_id": "C-HTTP-1", "operator": "op-a"},
            )
        first = self._post(
            "/api/controls/release", {"control_id": "C-HTTP-1", "operator": "op-b"}
        )
        self.assertTrue(first["active"])  # 第一人确认后仍未解除
        second = self._post(
            "/api/controls/release", {"control_id": "C-HTTP-1", "operator": "op-c"}
        )
        self.assertFalse(second["active"])  # 双人确认后解除

    def test_route_recommendation_is_multilingual(self):
        route = self._get("/api/routes?from=gate_a&to=summit&lang=en&exertion=1")
        self.assertTrue(route["ok"])
        self.assertTrue(any(step.startswith("Take ") for step in route["instructions"]))
        with self.assertRaises(HTTPError) as error:
            self._get("/api/routes?from=gate_a&to=nowhere")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_restart_keeps_decisions_consistent(self):
        result = self._post("/api/restart", {})
        self.assertTrue(result["restarted"])
        self.assertTrue(result["consistent"])


if __name__ == "__main__":
    unittest.main()
