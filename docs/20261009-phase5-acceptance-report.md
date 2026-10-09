# Phase 5 验收报告：Qwen 引擎和情绪识别

**验收对象**：`ee9403029d7fcf117250abb2530a95b3c913b22e`
**验收日期**：2026-10-09
**依据**：`docs/20261009-phase5-qwen-engine-emotion-session-taskbook.md`
**验收环境**：Windows 10，Python 3.11，项目 `.venv`，本地 Qwen 兼容 API；隔离服务端口 `8001`

## 1. 最终结论

`ee94030` 已交付 Qwen 决策引擎、五级情绪字段、投诉风险路由、四引擎前端切换、决策元数据展示和会话自动提炼。自动化测试、Qwen 真实决策、WebSocket 黑盒流程、会话提炼和知识库隔离均有通过证据。

**严格验收判定：部分通过。**

部分通过的原因不是 Qwen 主流程不可用，而是任务书“所有引擎都满足五级情绪和投诉风险契约”的要求仍未完全满足：

1. Jev 和 Kev 的解析层没有校验 `user_emotion` 是否属于五级枚举，仍可能把非法值（例如旧值 `negative`）直接写入 `DecisionResult`。
2. Jev 和 Kev 的解析层没有在 `user_emotion == "complaint_risk"` 时强制 `escalate_to_human=True`。本次构造反例复现为 `complaint_risk + escalate_to_human=False`。主路由有保底逻辑，因此当前编排路径仍会转人工，但引擎结果契约本身不满足“一律转人工”。

以上两项属于实现缺口，本次验收未擅自修改代码；应在下一次整改提交中补齐并增加对应单元测试。

## 2. 基线和提交复核

- `HEAD`：`ee9403029d7fcf117250abb2530a95b3c913b22e`
- `origin/master`：`ee9403029d7fcf117250abb2530a95b3c913b22e`
- 工作区：验收开始时干净；测试和黑盒验证未修改跟踪文件
- `git diff --check ee94030^ ee94030`：通过
- 当前验收新增文件之外，未修改实现代码

提交主体包含 Qwen 引擎、解析器、四引擎工厂、情绪规则、路由、WebSocket 元数据、会话提炼、前端及 Phase 5 测试。

## 3. 自动化测试

### 3.1 Phase 5 专项测试

执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/test_phase5_units.py
```

结果：

```text
14 passed in 4.08s
```

覆盖内容包括：

- 工厂创建 Qwen
- Qwen 合法 JSON 映射
- 乱码降级到 `qwen_failed`
- 超时降级
- Qwen `complaint_risk` 强制转人工
- 规则引擎五级情绪
- Jev/Kev 五级情绪常量
- 路由投诉风险保底
- 默认 `SESSION_END_SECONDS=20`
- 新消息取消提炼计时
- 编号从 60 开始及续接
- `data/sdwan.md` 未修改
- 提炼失败只记录日志
- 同一会话只提炼一次

### 3.2 全量 pytest

执行：

```text
.venv/Scripts/python.exe -m pytest tests -q
```

结果：

```text
80 passed, 1 skipped in 52.06s
```

满足任务书要求的至少 72 passed、最多 1 skipped、0 failed。唯一 skipped 为外部真实 LLM 探测在服务不可用时按既有设计跳过，不是 Phase 5 失败。

### 3.3 历史回归入口

执行 ` .venv/Scripts/python.exe tests/run_all.py`，结果：

```text
Phase 1 单元验收：13/13
Phase 2 单元验收：23/23
既有端到端验收：7/7
全部验收通过
```

该入口不覆盖 Phase 3-5 的全部 pytest 测试，不能替代全量 pytest；这里只作为旧回归参考。

## 4. 静态契约检查

### 4.1 通过项

- `SUPPORTED_ENGINES` 为 `('rule', 'jev', 'kev', 'qwen')`，四个引擎工厂均可创建。
- Qwen 使用 `asyncio.wait_for`，默认决策超时 10 秒。
- Qwen 乱码、缺字段、非法 JSON 和调用异常统一降级为 `intent="unclear"`、置信度 0、`engine="qwen_failed"`，不向聊天流程抛异常。
- Qwen 解析层对意图和情绪枚举进行校验，并对 `complaint_risk` 强制升级。
- 规则引擎已拆分 `dissatisfied` 和 `complaint_risk`，投诉风险在规则引擎层升级。
- `backend` 范围执行 `grep -rn '"negative"' backend` 无输出。
- 路由器对 `complaint_risk` 有统一人工转接保底。
- WebSocket `response.data` 携带 `engine`、`decision_latency_ms`、`intent`、`intent_confidence`、`emotion`、`path` 和 `sources`。
- 前端包含 Qwen 按钮、四引擎差异化高亮、决策元数据显示，以及 `dissatisfied` 橙色和 `complaint_risk` 红色样式。
- 运行时知识库从 `KNOWLEDGE_BASE_PATH`（本项目为 `data/sdwan.md`）加载；`data/sdwan-real.md` 是独立提炼输出，不进入运行时知识库。

### 4.2 未通过项

#### P1：Jev/Kev 未强制投诉风险转人工

通过直接调用解析函数构造以下返回：

```text
user_emotion=complaint_risk
escalate_to_human=false
```

结果：

```text
Jev: complaint_risk, False
Kev: complaint_risk, False
```

证据位置：

- `backend/decision_layer/jev_client.py` 的 `user_emotion`/`escalate_to_human` 解析
- `backend/decision_layer/kev_client.py` 的 `_result_from_answers` 和文本解析

`backend/orchestrator/router.py` 的保底条件可以在完整编排中拦截该情绪，但不能弥补引擎层 DecisionResult 契约缺口。

#### P1：Jev/Kev 未校验五级情绪枚举

Jev 解析返回 `user_emotion="negative"` 时，本次复现仍得到 `negative`，没有降级或拒绝。Kev 的 HTTP 和文本解析同样直接接收模型返回字符串。现有 Phase 5 测试只检查常量，不检查这两个解析路径的实际枚举约束。

#### P2：四引擎黑盒自动化证据不完整

本次手工 WebSocket 验收验证了 `rule → qwen → rule` 切换，且 Qwen 响应正常；已有自动化 E2E 主要覆盖 `jev → rule`。因此功能已验证，但没有自动化逐一覆盖四个按钮和四个引擎的完整运行链路。

#### P2：少量旧文案仍描述三引擎

以下实现/配置文案仍需后续同步：

- `backend/config.py` 的 `DECISION_ENGINE` 描述
- `backend/main.py` 无效引擎错误提示仍列 `rule/jev/kev`
- `backend/main.py` 和 `backend/decision_layer/base.py` 的注释仍有三引擎表述
- `README.md` 的部分历史章节仍写“三引擎”

任务书限定的 backend `grep -rn '"negative"' backend` 已通过；历史任务书和旧 Phase 3 证据中的 `negative` 不属于本次 backend 实现检查范围。

## 5. 隔离服务黑盒验证

为避免干扰已有服务，使用当前提交在 `127.0.0.1:8001` 启动隔离实例，完成后已停止该实例，未终止其他端口进程。

### 5.1 HTTP 接口

- `GET /healthz`：HTTP 200，`app=sdwan-kf-xiaoli`，`status=ok`，`qa_pairs=58`
- `GET /engine`：初始引擎为 `rule`
- `GET /metrics`：HTTP 200
- `POST /metrics/reset`：HTTP 200，计数清零

### 5.2 WebSocket

通过 WebSocket 验证：

- 状态顺序：`waiting → thinking → response`
- `rule → qwen → rule` 切换成功，`engine_switched` 确认与 `GET /engine` 一致
- 正常价格问题响应带有 `engine=rule`、`intent=price_inquiry`、`intent_confidence=0.9`、`emotion=neutral`、`decision_latency_ms=0`
- 投诉消息“再不处理我就投诉了”响应为：

```json
{
  "engine": "qwen",
  "path": "human_escalation",
  "intent": "unclear",
  "emotion": "complaint_risk",
  "sources": [],
  "decision_latency_ms": 5264
}
```

- 黑盒运行指标：`total_requests=2`、`total_errors=0`，路径为 `faq_match=1`、`human_escalation=1`

因此当前完整编排链路对投诉风险能够正确转人工；前述 P1 是引擎层结果契约缺口，而不是本次路由黑盒漏转。

## 6. 会话提炼和数据隔离

使用临时输出路径和 `SESSION_END_SECONDS=1` 等效配置进行真实 Qwen 提炼，不写入项目的 `data/sdwan-real.md`：

- 静默后成功生成一条问答，编号为 `60`
- 生成内容包含问题、答案和来源行：`（来源：会话acceptance-real-qwen 2026-10-09 23:17 引擎rule）`
- `data/sdwan.md` 提炼前后 SHA-256 均为 `3757e194100527d10d93581d7157595b2c0d9825d106853748658e26ce36b0e1`
- 新消息取消计时后，临时输出文件不存在
- `data/sdwan-real.md` 未被运行时知识库加载

Phase 5 单元测试还验证了编号续接、提炼失败只记日志和同一会话只提炼一次。

## 7. 通过项、限制和整改建议

### 已通过

- 全量测试满足任务书数量和失败数硬指标
- Qwen 引擎工厂、JSON 解析、超时和异常降级
- Qwen 和规则引擎的投诉风险识别及完整路由转人工
- 四引擎工厂和 Qwen WebSocket 切换
- response 决策元数据和前端展示/颜色
- 会话静默提炼、编号、来源、取消计时、失败隔离
- 原始知识库不变且提炼文件不进入运行时知识库

### 仍需整改

1. 为 Jev/Kev 抽取统一的情绪校验与升级逻辑：非法情绪应降级或按契约拒绝；`complaint_risk` 必须强制 `escalate_to_human=True`。
2. 为 Jev/Kev 增加解析级单元测试，覆盖 `complaint_risk + false` 和非法 `negative` 两类反例。
3. 将四引擎切换和四类响应元数据加入自动化 E2E；同步配置、注释和错误提示中的四引擎文案。
4. 会话提炼编号读取与追加目前没有跨会话并发锁，多会话同时结束时存在编号竞争风险；本次单会话验收不阻断，但建议后续处理。

## 8. 验收判定

Phase 5 的 Qwen 主流程、情绪展示、投诉路由、会话提炼和数据隔离已经可以运行，自动化及隔离黑盒证据充分；但任务书对“四个引擎都输出五级情绪”和“`complaint_risk` 一律转人工”的引擎层契约仍未完全满足。

**最终判定：Phase 5 部分通过，需完成 Jev/Kev 情绪契约整改后再闭环验收。**

机器可读复跑记录：`docs/acceptance-evidence/20261009-phase5-acceptance.json`
