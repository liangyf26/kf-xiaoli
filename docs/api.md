# API文档

SDWAN智能客服机器人对外接口：HTTP管理端点 + WebSocket对话通道。

## HTTP端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web对话界面（static/index.html） |
| GET | `/healthz` | 健康检查：`{"app": "sdwan-kf-xiaoli", "status": "ok", "qa_pairs": 58}` |
| GET | `/metrics` | 运行时指标快照（请求数/引擎分布/路径分布/平均延迟/错误数） |
| POST | `/metrics/reset` | 重置运行时指标 |

### GET /metrics 返回示例

```json
{
  "started_at": "2026-10-09 16:55:15",
  "total_requests": 1,
  "total_errors": 0,
  "avg_latency_ms": 2.0,
  "engine_counts": {"rule": 1},
  "path_counts": {"faq_match": 1}
}
```

指标同时落盘 `logs/metrics.json`（每次请求后刷新）。

## WebSocket对话协议

连接地址：`ws://localhost:8000/ws`

### 客户端 → 服务端

```json
{"type": "user_message", "content": "直播线路多少钱"}
{"type": "clear_conversation"}
```

- 裸文本消息兼容：非JSON消息按 `user_message` 处理。
- 空消息服务端静默丢弃。
- `clear_conversation`：清空历史、取消等待中的汇总、重置澄清计数与会话首问状态。

### 服务端 → 客户端（4种消息类型）

```json
{"type": "waiting", "data": {"remaining_seconds": 15, "message_count": 2}}
{"type": "countdown", "data": {"remaining_seconds": 8}}
{"type": "thinking"}
{"type": "response", "data": {"answer": "...", "sources": ["问题1"], "intent": "price_inquiry", "engine": "rule", "path": "faq_match", "need_clarification": false}}
{"type": "error", "data": {"message": "服务暂时不可用，请稍后重试"}}
```

- `waiting`/`countdown`：等待汇总窗口（滑动15秒，30秒封顶）的倒计时推送。
- `thinking`：汇总结束、开始编排生成。
- `response.data.sources`：知识库来源编号；`path` ∈ `clarification` / `faq_match` / `llm_generation` / `human_escalation`。
- `error`：服务端异常兜底提示。

### 处理路径说明

| path | 触发条件 | 行为 |
|---|---|---|
| `human_escalation` | 决策层要求转人工（校准条件） | 安抚话术+告知转人工 |
| `clarification` | 意图不明确且消息短（<8字，未超2次） | 返回按意图的澄清问句 |
| `faq_match` | 低复杂度（≤30）+高置信度（≥0.9） | 直接返回知识库原文与来源 |
| `llm_generation` | 其余情况 | LLM基于知识库Top-6相关条目生成JSON回复 |

## 配置项

见 `.env.example`（全部配置项带注释）。核心项：

- `MODEL_API_BASE`/`MODEL_NAME`：主LLM（OpenAI兼容chat/completions），60秒超时、失败重试1次。
- `DECISION_ENGINE`：`rule` / `jev` / `kev`，修改后重启生效；也可被环境变量覆盖（batch/eval脚本用）。
- `JEV_API_KEY`：OpenRouter密钥（sk-or-v1-开头），Jev经 `{JEV_API_BASE}/alpha/decisions` 结构化决策端点访问。
- `KEV_SERVE_URL`：本地kev.serve服务地址；不可达时Kev决策降级（kev_failed），不阻塞对话。
- `WAIT_SLIDE_SECONDS`/`WAIT_MAX_SECONDS`：等待汇总窗口。
- `LOG_LEVEL`：日志级别；文件日志按日期轮转（`logs/app.log`，保留14天）。

## 决策引擎接口（内部扩展点）

新增引擎实现 `backend/decision_layer/base.py` 的抽象接口并在工厂注册：

```python
class DecisionEngine(ABC):
    @abstractmethod
    async def decide(self, message: str, context: Dict[str, Any]) -> DecisionResult: ...
```

`DecisionResult` 核心字段：`intent`、`intent_confidence`、`needs_clarification`、`technical_complexity`、`user_emotion`、`escalate_to_human`。Jev/Kev遵循TypeSafe System One契约（questions判别键`type`：noul/choice/score，score最多10档）。
