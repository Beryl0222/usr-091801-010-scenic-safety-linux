# 景区客流安全调度

传统山地景区加入徒步线、索道、天梯、夜间演出和极限运动后，游客不再走同一条观光路线。
本服务把**闸机计数、接驳容量、设施运行状态、气象告警与匿名位置区段**汇成
**带置信度的占用区间**，在换乘点与山地路段出现局部风险前给出可解释的限流与疏散建议。

## 设计原则

1. **不假装精确**：每个区段给出 `[low, point, high]` 占用区间与置信度；阈值永远对
   **上界 high** 比较，从不比较点估计。
2. **迟到/失联更保守**：证据迟到时阈值折减（70% 截留线按 63% 执行）；失联超过阈值后
   动作降为 `require_confirmation`，只挂人工确认，不自动封控；失联过久的旧读数视为
   “占用未知”，区间退化为 `[0, 物理容量]`，绝不拿陈旧计数冒充当前人数。
3. **规则可解释**：每次限流都记录规则版本、证据、受影响路径与**解除依据**
   （如“占用上界连续 8 分钟低于 55%”）。
4. **管制有寿命**：临时管制必须有发起人、原因、**到期时间**；到期自动失效；
   未到期解除需**双人确认**（解除人 ≠ 复核人，且两人都不能是发起人）。
5. **已进入危险区段者优先疏散**：上游只截留、不与撤离抢道；hold 边允许场内游客
   顺向外撤，block 边双向禁行。
6. **可重放**：所有状态由事件日志重建。`service_restart` 后重放日志即可恢复全部管制，
   重放结果确定性（无随机编号）。

## 领域模型

- `topology.py`：节点（闸机/广场/换乘点/索道站/观景台/庇护点）与有向边
  （步道/索道/天梯/接驳班线），边有方向、容量、体力等级 1–4、发车间隔、
  计划关闭窗口与多语种名称。
- `events.py`：事件同时带 `occurred_at`（现场发生）与 `observed_at`（入库）时间，
  差值超阈值即迟到；馈源（Feed）声明迟到/失联时限。
- `estimator.py`：占用区间估计。直接区段计数 → 窄区间；迟到/乱序 → 放宽并降置信度；
  失联 → 漂移放大；失联过久 → 占用未知。闸机累计给出在园总人数。
- `weather.py`：advisory/warning/severe 三级告警状态机，迟到旧告警不能覆盖新告警，
  迟到旧解除不能抹掉新告警。
- `restrictions.py`：临时管制（hold 只出不进 / block 双向封闭），到期与双人解除。
- `rules.py`：规则手册 `scenic-rules-2026.1`。占用四级
  （关注 50% / 截留 70% / 封闭 85% / 疏散 95%），不确定折减；另含设施停运
  `FAC-STOP-05` 与气象 `FAC-WX-WARN/SEVERE` 硬规则。
- `routing.py`：Dijkstra 路线推荐，遵守单向、关闭窗口、体力、管制、设施状态与拥堵权重，
  输出多语种步骤与提示。
- `evacuation.py`：按 severe > block > evac > hold 排序疏散指令，就近庇护点、
  多语种广播、上游截留边列表；证据不足的区段只列为“待人工核实候选”，不下令。
- `dispatch.py`：措施（measure）生命周期 `monitoring / pending_confirmation /
  active / condition_cleared / denied / auto_released / expired`。证据充分自动下发
  管制；不确定只挂确认；生效管制不会因证据变差而放松；解除判据持续满足后系统按规则
  自动解除（复核方记为规则手册版本），人工管制仍须双人解除。
- `replay.py`：按入库顺序回放 JSONL，输出逐事件时间线与最终报告。

## 数据

- `data/yunling_park.json`：云岭山地景区拓扑（16 个节点、19 条边、14 个馈源）。
- `data/scenarios/peak_day.jsonl`：上午峰值入园，含迟到读数、乱序读数、馈源失联，
  换乘点自动截留、山脊段失联转人工确认。
- `data/scenarios/storm_recovery.jsonl`：索道停运封控、暴雨/雷暴预警与疏散、
  人工双人解除、设施恢复后按规则自动解封。
- `data/scenarios/restart_chaos.jsonl`：乱序/迟到、坏事件行、人工短时管制到期、
  服务重启重建、夜场峰值自动限流与散场后自动解除。

## 运行

```bash
python3 service.py --check                    # 基础自检
python3 service.py --port 8000                # 启动 HTTP 服务
python3 service.py --replay peak_day          # 回放场景，打印汇总
python3 service.py --replay storm_recovery --out report.json
npm test                                      # 运行全部 49 个测试
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康端点（字段稳定的契约） |
| GET | `/state` | 时钟、在园人数、规则版本、重启记录、日志事件数 |
| GET | `/rules` | 规则手册：阈值、折减原则、各规则的解除依据 |
| GET | `/topology?langs=zh,en,ja` | 节点/边/方向/容量/体力/关闭窗口（多语种） |
| GET | `/zones` | 全部观测区段的占用区间、置信度、新鲜度 |
| GET | `/facilities` | 索道/天梯/接驳状态、通行能力、余座、观测龄 |
| GET | `/feeds` | 馈源健康：fresh/late/stale/ghost、乱序标记 |
| GET | `/alerts` | 生效气象告警 |
| GET | `/restrictions`、`/restrictions/{id}` | 管制及解除依据/受影响路径名称 |
| GET | `/measures` | 当前规则命中与措施（含 pending_confirmation） |
| GET | `/evacuation?langs=zh,en` | 疏散指令、优先级、多语种广播、待核实候选 |
| GET | `/routes?from=…&to=…&fitness=2&langs=zh,en` | 合规路线或不可行原因 |
| GET | `/events?limit=100` | 事件日志 |
| POST | `/events` | 摄入一个观测/告警/管制事件 |
| POST | `/evaluate` | 在当前时钟执行一次规则评估 |
| POST | `/measures/{id}/confirm` | 值班员确认不确定证据（body: `{"actor": "…"}`） |
| POST | `/measures/{id}/deny` | 驳回挂起措施 |
| POST | `/restrictions` | 人工管制（必须含 `issued_by`、`reason`、`expires_at`） |
| POST | `/restrictions/{id}/release` | 双人解除（`released_by` + `witness`，均不得为发起人） |
| POST | `/replay` | 无副作用回放内置场景（`{"scenario":"peak_day"}`）或文件路径 |
| POST | `/reset` | 清空 live 状态 |

限流响应示例（换乘点截留）中 `rule` 字段说清三件事：

- `rule_id` / `rule_version`：`OCC-HOLD-03` @ `scenic-rules-2026.1`
- `evidence`：占用上界 206 / 容量 260 = 79%、新鲜度、馈源来源
- `release_basis`：占用上界连续 8 分钟低于 55%；对应管制的 `release_state` 说明
  到期自动失效或双人解除条件。
