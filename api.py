"""HTTP 接口背后的应用层：一个可加锁的实时调度状态 + 请求处理函数。

所有写操作都经过事件日志（人工下发/确认/解除都会产生可重放事件），
因此每个限流响应都能回答三件事：用的哪条规则、影响哪些路径、解除依据是什么。
"""

import json
import threading
from urllib.parse import parse_qs, urlparse

from clock import format_minute
from dispatch import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    Dispatch,
    DispatchError,
)
from events import Event, parse_event
from replay import build_from_files, load_scenario, run_replay
from routing import RoutingContext, plan_route
from rules import RULEBOOK_VERSION, RULES

DEFAULT_LANGS = ["zh", "en"]


class ApiError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AppState:
    def __init__(self, topology_path: str):
        self.topology_path = topology_path
        self.topo, self.feeds = build_from_files(topology_path)
        self.dispatch = Dispatch(self.topo, self.feeds)
        self.lock = threading.RLock()

    def reset_live(self, topology_path: str | None = None):
        with self.lock:
            if topology_path:
                self.topology_path = topology_path
                self.topo, self.feeds = build_from_files(topology_path)
            self.dispatch = Dispatch(self.topo, self.feeds)

    def load_scenario_into_live(self, scenario_path: str) -> dict:
        with self.lock:
            header, events_raw = load_scenario(scenario_path)
            self.topo, self.feeds = build_from_files(header["topology"])
            self.dispatch = Dispatch(self.topo, self.feeds)
            ingested = 0
            for line_no, raw in enumerate(events_raw, start=2):
                self.dispatch.ingest(parse_event(raw, line_no))
                ingested += 1
            self.dispatch.evaluate()
            return {"scenario": header["scenario"], "events_ingested": ingested}


def _query(environ_path: str):
    parsed = urlparse(environ_path)
    return parsed.path, parse_qs(parsed.query)


def _langs(params) -> list[str]:
    raw = params.get("langs", ["zh,en"])[0]
    langs = [part.strip() for part in raw.split(",") if part.strip()]
    return langs or DEFAULT_LANGS


class Api:
    def __init__(self, state: AppState):
        self.state = state

    # -------------------------------------------------------------- 路由入口

    def handle(self, method: str, path: str, body: bytes) -> tuple[int, dict]:
        route, params = _query(path)
        try:
            data = self._dispatch(method, route, params, body)
            return 200, data
        except ApiError as exc:
            return exc.status, {"error": exc.message}
        except DispatchError as exc:
            return 409, {"error": str(exc)}
        except (KeyError, ValueError) as exc:
            return 400, {"error": str(exc)}

    def _dispatch(self, method, route, params, body) -> dict:
        payload = json.loads(body.decode()) if body else {}
        d = self.state.dispatch

        if method == "GET":
            if route == "/health":
                # 健康端点不走锁也不暴露内部字段。
                return {"status": "ok"}
            with self.state.lock:
                now = d.clock.now()
                if route == "/state":
                    return {
                        "clock": now,
                        "clock_hhmm": format_minute(now),
                        "in_park": d.estimator.in_park(),
                        "total_entries": d.estimator.total_entries,
                        "total_exits": d.estimator.total_exits,
                        "rulebook_version": RULEBOOK_VERSION,
                        "restarts": d.restarts,
                        "events_in_log": len(d.event_log),
                        "topology": self.state.topo.park_id,
                    }
                if route == "/rules":
                    return self._rules_view()
                if route == "/topology":
                    return self._topology_view(_langs(params))
                if route == "/zones":
                    return {"clock_hhmm": format_minute(now), "zones": d.zone_views(now)}
                if route == "/facilities":
                    return {"clock_hhmm": format_minute(now), "facilities": d.facility_views(now)}
                if route == "/feeds":
                    return {"clock_hhmm": format_minute(now), "feeds": d.estimator.feed_health(now)}
                if route == "/alerts":
                    return {"alerts": [a.to_dict() for a in d.weather.active()]}
                if route == "/restrictions":
                    return {"restrictions": [r.to_dict(now) for r in d.restrictions.all()]}
                if route.startswith("/restrictions/"):
                    return self._restriction_detail(route.rsplit("/", 1)[1], now)
                if route == "/measures":
                    result = d.evaluate(now)
                    return {
                        "clock_hhmm": format_minute(now),
                        "measures": result["measures"],
                        "hits": result["hits"],
                    }
                if route == "/events":
                    limit = int(params.get("limit", ["100"])[0])
                    return {"events": [self._event_json(e) for e in d.event_log[-limit:]]}
                if route == "/evacuation":
                    plan = d.evacuation_plan(_langs(params), now)
                    return plan
                if route == "/routes":
                    return self._route(params, now)
                if route == "/scenarios":
                    return {"scenarios": ["peak_day", "storm_recovery", "restart_chaos"]}
            raise ApiError(404, f"未知路径: {route}")

        if method == "POST":
            with self.state.lock:
                if route == "/events":
                    return self._ingest_one(payload)
                if route == "/evaluate":
                    return d.evaluate()
                if route.startswith("/measures/"):
                    return self._measure_action(route, payload)
                if route == "/restrictions":
                    return self._issue_manual(payload)
                if route.startswith("/restrictions/") and route.endswith("/release"):
                    rid = route.split("/")[2]
                    result = d.release_restriction(
                        rid, payload["released_by"], payload["witness"]
                    )
                    return {"released": result}
                if route == "/replay":
                    return self._run_replay(payload)
                if route == "/reset":
                    self.state.reset_live(payload.get("topology"))
                    return {"reset": True}
            raise ApiError(404, f"未知路径: {route}")

        raise ApiError(405, f"不支持的方法: {method}")

    # -------------------------------------------------------------- 视图

    def _rules_view(self) -> dict:
        return {
            "rulebook_version": RULEBOOK_VERSION,
            "principles": [
                "阈值一律对占用区间上界 high 比较，不比较点估计",
                "证据迟到/失联/乱序时阈值折减并转人工确认，不自动下发硬管制",
                "生效管制不因证据变差而放松；只能按解除依据或到期收口",
                "人工管制未到期解除必须双人确认，且解除人不能是发起人",
            ],
            "occupancy_rules": RULES,
            "special_rules": [
                {
                    "rule_id": "FAC-STOP-05",
                    "name": "设施停运封控",
                    "release_basis": "设施恢复 open 状态，并完成空载试车确认",
                },
                {
                    "rule_id": "FAC-WX-WARN",
                    "name": "气象预警封闭",
                    "release_basis": "气象预警解除，且现场复核无次生危险",
                },
                {
                    "rule_id": "FAC-WX-SEVERE",
                    "name": "严重气象疏散",
                    "release_basis": "严重告警解除，且现场复核无次生危险",
                },
            ],
        }

    def _topology_view(self, langs) -> dict:
        topo = self.state.topo
        return {
            "park_id": topo.park_id,
            "nodes": [
                {
                    "id": n.node_id,
                    "kind": n.kind,
                    "names": [
                        {"lang": lang, "text": topo.localize(n.name, [lang])["text"]}
                        for lang in langs
                    ],
                    "safe": n.safe,
                    "exit_point": n.exit_point,
                    "capacity": n.capacity,
                }
                for n in topo.nodes.values()
            ],
            "edges": [
                {
                    "id": e.edge_id,
                    "kind": e.kind,
                    "src": e.src,
                    "dst": e.dst,
                    "direction": e.direction,
                    "capacity": e.capacity,
                    "fitness": e.fitness,
                    "headway": e.headway,
                    "travel_min": e.travel_min,
                    "names": [
                        {"lang": lang, "text": topo.localize(e.name, [lang])["text"]}
                        for lang in langs
                    ],
                    "closures": [
                        {"start": format_minute(c.start), "end": format_minute(c.end),
                         "reason": c.reason}
                        for c in e.closures
                    ],
                }
                for e in topo.edge_objects()
            ],
        }

    def _restriction_detail(self, rid: str, now: int) -> dict:
        restriction = self.state.dispatch.restrictions.get(rid)
        if restriction is None:
            raise ApiError(404, f"管制不存在: {rid}")
        detail = restriction.to_dict(now)
        # 明确解释“受影响路径”的多语种名称与当前状态。
        detail["affected_path_names"] = [
            {
                "edge_id": eid,
                "names": [
                    {"lang": lang,
                     "text": self.state.topo.localize(self.state.topo.edges[eid].name, [lang])["text"]}
                    for lang in ("zh", "en")
                ],
            }
            for eid in detail["edges"] if eid in self.state.topo.edges
        ]
        return detail

    def _route(self, params, now) -> dict:
        src = params.get("from", [""])[0]
        dst = params.get("to", [""])[0]
        if not src or not dst:
            raise ApiError(400, "路线查询需要 from 与 to 参数")
        max_fitness = int(params.get("fitness", ["4"])[0])
        langs = _langs(params)
        kwargs = self.state.dispatch.routing_context_kwargs()
        ctx = RoutingContext(
            self.state.topo, now, max_fitness=max_fitness, langs=langs, **kwargs
        )
        plan = plan_route(self.state.topo, src, dst, ctx)
        plan["clock_hhmm"] = format_minute(now)
        return plan

    def _event_json(self, event) -> dict:
        return {
            "event_id": event.event_id,
            "type": event.type,
            "occurred_at": event.occurred_at,
            "occurred_hhmm": format_minute(event.occurred_at),
            "observed_at": event.observed_at,
            "observed_hhmm": format_minute(event.observed_at),
            "source": event.source,
            "payload": event.payload,
        }

    # -------------------------------------------------------------- 写操作

    def _ingest_one(self, payload: dict) -> dict:
        event = parse_event(payload)
        info = self.state.dispatch.ingest(event)
        result = self.state.dispatch.evaluate()
        return {
            "ingested": info,
            "pending_confirmation": [
                m["measure_id"] for m in result["measures"] if m["status"] == STATUS_PENDING
            ],
            "active_restrictions": [
                r.restriction_id
                for r in self.state.dispatch.restrictions.active(self.state.dispatch.clock.now())
            ],
        }

    def _measure_action(self, route, payload) -> dict:
        parts = route.strip("/").split("/")
        # /measures/{id}/confirm | /deny
        if len(parts) != 3 or parts[0] != "measures":
            raise ApiError(404, f"未知路径: {route}")
        mid, action = parts[1], parts[2]
        d = self.state.dispatch
        if action == "confirm":
            actor = payload.get("actor", "")
            if not actor:
                raise ApiError(400, "确认必须提供 actor（值班员）")
            measure = d.confirm_measure(mid, actor, ttl=payload.get("ttl"))
        elif action == "deny":
            actor = payload.get("actor", "")
            if not actor:
                raise ApiError(400, "驳回必须提供 actor")
            measure = d.deny_measure(mid, actor, payload.get("note", ""))
        else:
            raise ApiError(404, f"未知措施动作: {action}")
        d.evaluate()
        return {"measure": measure.to_dict(d.clock.now())}

    def _issue_manual(self, payload: dict) -> dict:
        """运营人工临时管制：必须有发起人、原因、到期时间。"""
        now = self.state.dispatch.clock.now()
        required = ("issued_by", "reason", "expires_at")
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise ApiError(400, f"人工管制缺少必填字段: {missing}")
        event = Event(
            event_id=payload.get("event_id", f"evt-manual-{self.state.dispatch._next_nonce()}"),
            type="restriction",
            occurred_at=int(payload.get("occurred_at", now)),
            observed_at=now,
            source=payload.get("source", "ops-console"),
            payload={
                "restriction_id": payload["restriction_id"],
                "action": payload.get("action", "hold"),
                "edges": payload.get("edges", []),
                "nodes": payload.get("nodes", []),
                "reason": payload["reason"],
                "issued_by": payload["issued_by"],
                "confirm_by": payload.get("confirm_by", ""),
                "expires_at": int(payload["expires_at"]),
                "rule_id": payload.get("rule_id", "manual"),
                "release_basis": payload.get("release_basis", "值班员双人确认解除"),
            },
        )
        self.state.dispatch.ingest(event)
        self.state.dispatch.evaluate()
        return {
            "issued": payload["restriction_id"],
            "note": "未到期解除需要与发起人不同的两名值班员双人确认",
        }

    def _run_replay(self, payload: dict) -> dict:
        """回放对 live 状态无副作用：构建独立 Dispatch 跑完整时间线。"""
        scenario = payload.get("scenario")
        path = payload.get("path") or (
            f"data/scenarios/{scenario}.jsonl" if scenario else None
        )
        if not path:
            raise ApiError(400, "replay 需要 scenario 名称或 path")
        header, events_raw = load_scenario(path)
        topo, feeds = build_from_files(header["topology"])
        report = run_replay(topo, feeds, events_raw)
        return {"scenario": header["scenario"], "title": header.get("title", {}), **report}
