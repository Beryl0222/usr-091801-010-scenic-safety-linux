"""场景回放：按入库顺序喂入乱序事件，周期性评估并记录全过程。

回放只信任事件文件的行顺序（入库顺序）；事件的 occurred_at 可以倒退（乱序）、
可以晚于 observed_at（迟到）。每摄入一个事件后在该入库时刻评估一次，
措施的产生/挂起/解除、管制状态、馈源新鲜度都进入时间线报告。
"""

import json
from dataclasses import dataclass

from clock import format_minute
from events import Feed, parse_event
from dispatch import (
    STATUS_ACTIVE,
    STATUS_CLEARED,
    STATUS_DENIED,
    STATUS_PENDING,
    Dispatch,
)
from topology import Topology


def load_scenario(path: str):
    topology_raw = None
    events_raw = []
    with open(path, encoding="utf-8") as handle:
        first = json.loads(handle.readline())
        if first.get("scenario"):
            header = first
            for line in handle:
                line = line.strip()
                if line:
                    events_raw.append(json.loads(line))
        else:
            raise ValueError("场景文件首行必须是 scenario 头")
    return header, events_raw


def build_from_files(topology_path: str, feeds=None) -> tuple[Topology, list[Feed]]:
    with open(topology_path, encoding="utf-8") as handle:
        raw = json.load(handle)
    topo = Topology(raw)
    feed_defs = [Feed.from_raw(item) for item in (feeds or raw.get("feeds", []))]
    return topo, feed_defs


@dataclass
class ReplayConfig:
    langs: tuple = ("zh", "en")


def run_replay(topology: Topology, feeds: list[Feed], events_raw: list[dict]) -> dict:
    dispatch = Dispatch(topology, feeds)
    timeline = []
    parse_errors = []

    known_measures = {}  # measure_id -> 上次状态，用于检测变化
    known_restrictions: set[str] = set()

    def snapshot_changes(result, at, note=""):
        changes = {"new_measures": [], "measure_updates": [], "new_restrictions": []}
        for measure in result["measures"]:
            mid = measure["measure_id"]
            prev = known_measures.get(mid)
            if prev is None:
                changes["new_measures"].append(
                    {"measure_id": mid, "status": measure["status"],
                     "action": measure["action"], "ref": measure["ref"],
                     "rule_id": measure["rule"]["rule_id"]}
                )
            elif prev != measure["status"]:
                changes["measure_updates"].append(
                    {"measure_id": mid, "from": prev, "to": measure["status"],
                     "release_ready": measure["release_ready"],
                     "recommendation": measure["release_recommendation"]}
                )
            known_measures[mid] = measure["status"]
        for restriction in dispatch.restrictions.all():
            if restriction.restriction_id not in known_restrictions:
                changes["new_restrictions"].append(
                    {
                        "restriction_id": restriction.restriction_id,
                        "action": restriction.action,
                        "edges": list(restriction.edge_ids),
                        "issued_by": restriction.issued_by,
                        "rule_id": restriction.rule_id,
                        "expires_at": restriction.expires_at,
                    }
                )
                known_restrictions.add(restriction.restriction_id)
        return changes

    for line_no, raw in enumerate(events_raw, start=2):
        try:
            event = parse_event(raw, line_no)
        except ValueError as exc:
            parse_errors.append({"line": line_no, "error": str(exc)})
            continue
        feed = dispatch.estimator.feeds.get(event.source)
        before = {
            "last_occurred": feed.last.occurred_at if feed and feed.last else None,
        }
        ingest_info = dispatch.ingest(event)
        result = dispatch.evaluate(event.observed_at)
        changes = snapshot_changes(result, event.observed_at)
        out_of_order = False
        late = False
        if feed and feed.feed.type in ("zone_count", "gate_count"):
            out_of_order = (
                before["last_occurred"] is not None
                and event.occurred_at < before["last_occurred"]
            )
            late = event.is_late(feed.feed.late_after)
        timeline.append(
            {
                "event_id": event.event_id,
                "type": event.type,
                "source": event.source,
                "occurred_at": event.occurred_at,
                "occurred_hhmm": format_minute(event.occurred_at),
                "observed_at": event.observed_at,
                "observed_hhmm": format_minute(event.observed_at),
                "delay_min": event.delay,
                "late": late,
                "out_of_order": out_of_order,
                "ingest": ingest_info,
                "changes": changes,
                "hit_rules": [
                    {
                        "rule_id": h["rule_id"],
                        "ref": h["ref"],
                        "action": h["action"],
                        "freshness": h["freshness"],
                        "uncertain": h["uncertain"],
                        "requires_confirmation": h["requires_confirmation"],
                        "ratio": h["observed_high_ratio"],
                    }
                    for h in result["hits"]
                ],
                "active_restriction_ids": [
                    r.restriction_id for r in dispatch.restrictions.active(event.observed_at)
                ],
                "in_park": result["in_park"],
            }
        )

    final_at = dispatch.clock.now()
    final = {
        "clock": final_at,
        "clock_hhmm": format_minute(final_at),
        "in_park": dispatch.estimator.in_park(),
        "restarts": dispatch.restarts,
        "rulebook_version": result["rulebook_version"],
        "alerts": [a.to_dict() for a in dispatch.weather.active()],
        "feeds": dispatch.estimator.feed_health(final_at),
        "zones": dispatch.zone_views(final_at),
        "facilities": dispatch.facility_views(final_at),
        "restrictions": [r.to_dict(final_at) for r in dispatch.restrictions.all()],
        "measures": [m.to_dict(final_at) for m in dispatch.measures.values()],
        "evacuation": dispatch.evacuation_plan(list(ReplayConfig.langs), final_at),
    }
    summary = {
        "events_ingested": len(timeline),
        "late_events": sum(1 for t in timeline if t["late"]),
        "out_of_order_events": sum(1 for t in timeline if t["out_of_order"]),
        "restarts": len(dispatch.restarts),
        "parse_errors": parse_errors,
        "active_restrictions": len(dispatch.restrictions.active(final_at)),
        "pending_confirmation": [
            m["measure_id"]
            for m in final["measures"]
            if m["status"] == STATUS_PENDING
        ],
        "stale_feeds": [f["feed_id"] for f in final["feeds"] if f["freshness"] == "stale"],
        "ghost_feeds": [f["feed_id"] for f in final["feeds"] if f["freshness"] == "ghost"],
    }
    return {"timeline": timeline, "final": final, "summary": summary}
