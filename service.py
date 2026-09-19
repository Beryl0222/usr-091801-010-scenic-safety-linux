"""景区客流安全调度的 HTTP 入口与命令行。

- python3 service.py --check                 基础自检
- python3 service.py --port 8000             启动 HTTP 服务
- python3 service.py --replay peak_day       回放内置峰值场景并打印汇总
- python3 service.py --replay <path> --out report.json
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api import Api, AppState
from replay import build_from_files, load_scenario, run_replay

SERVICE_ID = "scenic-safety"
SERVICE_NAME = "景区客流安全调度"
DEFAULT_TOPOLOGY = "data/yunling_park.json"


def health_payload():
    """返回服务的基础状态（契约稳定，勿改字段）。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    # main() 启动服务前会替换为自定义拓扑对应的 Api；
    # 直接实例化 Handler 的契约测试也能拿到默认拓扑。
    api: Api | None = None

    def _api(self) -> Api | None:
        if Handler.api is None:
            try:
                Handler.api = Api(AppState(DEFAULT_TOPOLOGY))
            except (OSError, ValueError):
                return None
        return Handler.api

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._send_json(200, health_payload())
            return
        api = self._api()
        if api is None:
            self._send_json(503, {"error": "服务尚未初始化"})
            return
        status, payload = api.handle("GET", self.path, b"")
        self._send_json(status, payload)

    def do_POST(self):
        api = self._api()
        if api is None:
            self._send_json(503, {"error": "服务尚未初始化"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        status, payload = api.handle("POST", self.path, body)
        self._send_json(status, payload)

    def log_message(self, *_args):
        return


def run_replay_cli(scenario: str, out: str | None) -> int:
    if scenario in ("peak_day", "storm_recovery", "restart_chaos"):
        path = f"data/scenarios/{scenario}.jsonl"
    else:
        path = scenario
    header, events_raw = load_scenario(path)
    topo, feeds = build_from_files(header["topology"])
    report = run_replay(topo, feeds, events_raw)
    document = {
        "scenario": header["scenario"],
        "title": header.get("title", {}),
        "langs": header.get("langs", ["zh"]),
        **report,
    }
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
        print(f"回放报告已写入 {out}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    # 历史日志里的坏行已记录进 summary.parse_errors，回放仍完整产出报告。
    return 0


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--topology", default=DEFAULT_TOPOLOGY)
    parser.add_argument("--replay", metavar="SCENARIO",
                        help="回放场景名（peak_day/storm_recovery/restart_chaos）或 JSONL 路径")
    parser.add_argument("--out", help="回放报告输出文件")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        topo, feeds = build_from_files(args.topology)
        assert topo.nodes and topo.edges and feeds
        print("基础检查通过")
        return

    if args.replay:
        raise SystemExit(run_replay_cli(args.replay, args.out))

    state = AppState(args.topology)
    Handler.api = Api(state)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()

if __name__ == "__main__":
    main()
