"""景区客流安全调度：HTTP 值班后端与回放/检查入口。"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from scenic.controls import ControlError
from scenic.dispatch import DispatchService
from scenic.model import load_topology
from scenic.replay import run_replay

SERVICE_ID = "scenic-safety"
SERVICE_NAME = "景区客流安全调度"
DATA_DIR = Path(__file__).parent / "data"


def health_payload():
    """返回服务的基础状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class App:
    """持有调度服务实例，支持加锁访问与重启模拟。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.service = DispatchService(load_topology(DATA_DIR / "topology.json"))

    def restart(self):
        """模拟服务重启：快照 → 全新实例恢复 → 校验决策一致。"""
        with self.lock:
            snapshot = self.service.snapshot()
            before = [d["id"] for d in self.service.evaluate()]
            self.service = DispatchService.restore(
                load_topology(DATA_DIR / "topology.json"), snapshot
            )
            after = [d["id"] for d in self.service.evaluate()]
            return {"restarted": True, "consistent": before == after}


class Handler(BaseHTTPRequestHandler):
    """值班席 HTTP 接口；/health 不依赖应用状态。"""

    app = None

    # --- 基础工具 ---
    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def _app(self):
        if Handler.app is None:
            self._send({"error": "service not initialized"}, status=503)
            return None
        return Handler.app

    # --- 路由 ---
    def do_GET(self):
        split = urlsplit(self.path)
        path, query = split.path, parse_qs(split.query)
        if path == "/health":
            self._send(health_payload())
            return
        if path not in ("/api/state", "/api/decisions", "/api/evacuation", "/api/routes"):
            self._send({"error": "not found"}, status=404)
            return
        app = self._app()
        if app is None:
            return
        with app.lock:
            if path == "/api/state":
                self._send(app.service.state())
            elif path == "/api/decisions":
                self._send({"decisions": app.service.evaluate()})
            elif path == "/api/evacuation":
                app.service.evaluate()
                self._send({"evacuation": app.service.evacuation})
            elif path == "/api/routes":
                group = {
                    "language": query.get("lang", ["zh"])[0],
                    "max_exertion": int(query.get("exertion", ["5"])[0]),
                }
                route = app.service.recommend(
                    query.get("from", ["gate_a"])[0],
                    query.get("to", ["summit"])[0],
                    group,
                )
                self._send(route, status=200 if route.get("ok") else 404)
            else:
                self._send({"error": "not found"}, status=404)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in (
            "/api/events",
            "/api/controls",
            "/api/controls/release",
            "/api/replay",
            "/api/restart",
        ):
            self._send({"error": "not found"}, status=404)
            return
        app = self._app()
        if app is None:
            return
        try:
            body = self._body()
        except (ValueError, UnicodeDecodeError):
            self._send({"error": "invalid json"}, status=400)
            return
        with app.lock:
            if path == "/api/events":
                events = body if isinstance(body, list) else [body]
                notes = []
                for raw in events:
                    notes.extend(app.service.ingest(raw)["notes"])
                self._send(
                    {"ingested": len(events), "notes": notes, "decisions": app.service.decisions}
                )
            elif path == "/api/controls":
                try:
                    control = app.service.impose_control(
                        initiator=body.get("initiator"),
                        reason=body.get("reason", ""),
                        elements=body.get("elements", []),
                        expires_at=body.get("expires_at"),
                        control_id=body.get("control_id"),
                    )
                except (ControlError, ValueError) as exc:
                    self._send({"error": str(exc)}, status=400)
                    return
                self._send(control, status=201)
            elif path == "/api/controls/release":
                try:
                    control = app.service.release_control(
                        body.get("control_id"), body.get("operator")
                    )
                except (ControlError, ValueError) as exc:
                    self._send({"error": str(exc)}, status=400)
                    return
                self._send(control)
            elif path == "/api/replay":
                self._send(run_replay(DATA_DIR / "topology.json", DATA_DIR / "events.json"))
            elif path == "/api/restart":
                self._send(app.restart())
            else:
                self._send({"error": "not found"}, status=404)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        report = run_replay(DATA_DIR / "topology.json", DATA_DIR / "events.json")
        assert report["restart_consistent"], "服务重启后决策不一致"
        print("基础检查通过")
        return
    if args.replay:
        report = run_replay(DATA_DIR / "topology.json", DATA_DIR / "events.json")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    Handler.app = App()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
