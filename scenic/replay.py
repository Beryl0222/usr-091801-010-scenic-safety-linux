"""值班席回放：乱序事件流、设施恢复与服务重启模拟。

回放按事件到达顺序（而非观测顺序）注入，模拟真实运行中数据乱序与迟到；
随后通过快照-恢复模拟服务重启，验证重启前后决策一致。
"""

import json

from .dispatch import DispatchService
from .model import load_topology


def run_replay(topology_path, events_path):
    topology = load_topology(topology_path)
    with open(events_path, encoding="utf-8") as fh:
        events = json.load(fh)

    service = DispatchService(topology)
    timeline = []
    for raw in events:
        result = service.ingest(raw)
        timeline.append(
            {
                "seq": raw.get("seq"),
                "type": raw["type"],
                "observed_at": raw.get("observed_at"),
                "received_at": raw.get("received_at"),
                "notes": result["notes"],
                "decisions": [
                    {"id": d["id"], "level": d["level"]} for d in result["decisions"]
                ],
            }
        )

    # 服务重启：快照 → 全新实例恢复 → 决策必须一致
    snapshot = service.snapshot()
    before = service.evaluate()
    restored = DispatchService.restore(topology, snapshot)
    after = restored.evaluate()
    restart_consistent = [d["id"] for d in before] == [d["id"] for d in after]

    return {
        "events": len(events),
        "timeline": timeline,
        "restart_consistent": restart_consistent,
        "final_decisions": after,
        "evacuation": restored.evacuation,
    }
