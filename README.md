# SDWAN智能客服机器人 Demo

基于大语言模型的智能客服系统，专为SDWAN专线产品咨询场景优化。支持4种决策引擎（规则/Jev API/Kev本地模型/Qwen主LLM）即时切换对比，答案全部基于知识库生成并标注来源，知识库外问题不编造、正确拒答。

**状态**：Demo开发完成（Phase 1-5全部交付），验收指标实测达标——批量测试通过率100%、答案准确率≥90%（严格口径）、平均响应3.2秒、4引擎可切换、五级情绪识别、会话自动提炼。

## 📋 功能特性

- **4种决策引擎对比**：规则引擎（<1ms关键词匹配）/ Jev API（OpenRouter结构化决策）/ Kev本地模型（GPU推理）/ **Qwen引擎（主LLM直接做意图识别，Few-shot JSON输出）**，页面点击即时切换，当前引擎各自专属色高亮
- **五级情绪识别**：中性 / 积极 / 紧急 / 不满（橙色标记）/ 投诉风险（红色标记，一律转人工），每条回复下方显示引擎、决策耗时、意图+置信度、情绪
- **会话自动提炼**：会话静默20秒判定结束后，用Qwen把整段对话提炼成一问一答追加到 `data/sdwan-real.md`（带来源标注），持续积累真实问答供知识库迭代；新消息到来自动取消计时，提炼失败不影响聊天
- **智能等待汇总**：连续发送多条消息自动汇总（滑动3秒窗口，30秒封顶），带倒计时推送
- **四路处理编排**：转人工 > 澄清 > 知识库FAQ直连 > LLM生成，按意图/置信度/复杂度路由
- **知识库接地生成**：LLM只基于知识库相关度Top-6条目作答，来源标注可折叠展示，未提及对象一律拒答不编造
- **多轮上下文**：短问题接续上文理解（"多少钱"接"直播线路"后正确报价）
- **运行时监控**：统一格式日志按日期轮转 + `/metrics` 指标端点（请求数/引擎分布/延迟/错误）

## 🚀 快速开始

### 1. 环境要求

- Python 3.10+（开发验证用3.11）
- 一个OpenAI兼容的LLM API（如本地Ollama跑 `qwen2.5:27b`，或内网Qwen服务）
- （可选）OpenRouter API Key（Jev引擎）；GPU + kev本地服务（Kev引擎）

### 2. 安装

```bash
# 创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/Mac

pip install -r requirements.txt
```

> Windows若系统默认Python版本过低，用 `py -3.11 -m venv .venv` 指定版本创建。
> 完整安装含Kev所需的torch/transformers（约2.5GB）；仅用rule/jev引擎时可先装核心依赖：
> `pip install fastapi "uvicorn[standard]" websockets httpx pydantic pydantic-settings python-dotenv`

### 3. 配置

```bash
cp .env.example .env
# 编辑 .env，至少填写：
#   MODEL_API_BASE=http://localhost:11434/v1   （LLM API地址）
#   MODEL_NAME=qwen2.5:27b                     （模型名）
#   DECISION_ENGINE=rule                       （决策引擎，先用rule最简单）
```

所有配置项及注释见 [.env.example](.env.example)；接口协议与决策引擎契约见 [docs/api.md](docs/api.md)。

### 4. 启动

```bash
python -m backend.main
# 浏览器打开 http://localhost:8000
```

### 5. 对话验证

发送"直播线路多少钱"→ 返回四档线路价格与来源标注；发送"支持YouTube吗"→ 正确拒答（知识库外不编造）。

## 📖 使用说明

### Web界面

1. **发送消息**：输入框输入问题，点击"发送"或回车；连续发送会等待3秒汇总后统一回复（倒计时可见）
2. **查看来源**：回复下方"查看来源"折叠展示知识库问题编号
3. **清空对话**：点击"清空对话"重置会话（服务端同步重置上下文）
4. **断线重连**：连接断开自动提示并重连

### 切换决策引擎

**方式一（推荐）**：页面顶部引擎选择器点击"规则引擎 / Jev引擎 / Kev引擎 / Qwen引擎"，**立即生效无需重启**；当前引擎按钮以专属色高亮（规则=蓝 / Jev=紫 / Kev=橙 / Qwen=绿），每条回复下方显示 ⚙ 决策结果行（引擎·耗时·意图+置信度·情绪）便于确认。

**方式二**：编辑 `.env` 的 `DECISION_ENGINE`（设置启动默认引擎）后重启服务：

| 引擎 | 说明 | 实测延迟 |
|---|---|---|
| `rule` | 关键词词典匹配，无需外部依赖 | <1ms |
| `jev` | OpenRouter Jev结构化决策API，需 `JEV_API_KEY` | ~1s/次 |
| `kev` | 本地kev.serve GPU服务，需先启动（见FAQ第3条） | ~1s/次 |
| `qwen` | 主LLM（Qwen3.8）Few-shot JSON意图识别，10秒超时/解析失败自动降级qwen_failed | ~5s/次 |

各引擎对同一评估集的准确率对比见 `tests/results/engine_accuracy_report_*.md`。

### 会话提炼（真实问答积累）

机器人回复后静默 `SESSION_END_SECONDS`（默认20秒）无新消息即判定会话结束，Qwen自动把整段对话提炼成一问一答，按知识库格式追加到 `data/sdwan-real.md`（含来源行"会话id 时间 引擎"，编号接已有最大编号）。该文件仅作知识库迭代素材，**不会**被加载进运行时知识库。提炼失败只记日志，不影响聊天。

### 运行监控

```bash
curl http://localhost:8000/metrics        # 请求数/引擎与路径分布/平均延迟/错误数
curl -X POST http://localhost:8000/metrics/reset   # 重置指标
```

日志文件 `logs/app.log` 按日期轮转（保留14天），关键日志点：用户消息接收、决策结果、路由路径、LLM调用、回复发送、异常。

### 批量测试与评估（全部脚本在 tests/ 下）

```bash
# 全量回归（单元 + 端到端，自动启停服务）
python tests/run_all.py

# 批量测试（20问，经完整编排流程；--engine 可指定引擎）
python tests/batch_test.py tests/test_questions_final.txt
python tests/generate_report.py tests/results/batch_test_*.json    # 生成Markdown报告

# 准确率评估（10个知识库标注用例，严格口径：澄清不计作答）
python tests/accuracy_evaluation.py tests/accuracy_test_cases.json --engine rule

# 多引擎准确率对比报告（rule/jev/kev/qwen任选）
python tests/accuracy_evaluation.py tests/accuracy_test_cases.json --engine rule
python tests/accuracy_evaluation.py tests/accuracy_test_cases.json --engine jev
python tests/accuracy_evaluation.py tests/accuracy_test_cases.json --engine kev
python tests/compare_engine_accuracy.py

# 性能分析（组件级耗时画像）
python tests/performance_profile.py
```

## 📊 测试结果摘要（2026-10-09实测）

| 指标 | 要求 | 实测 | 结论 |
|---|---|---|---|
| 批量测试通过率 | ≥90%（20+问） | **20/20 = 100%** | ✅ |
| 答案准确率（严格口径） | ≥85%（10标注用例） | rule **100%** / jev **100%** / kev **100%** | ✅ |
| 来源标注准确率 | — | 100%（9/9考核题） | ✅ |
| 平均响应时间 | <10秒 | **3.16秒**（优化前4.40秒，-28%） | ✅ |
| LLM路径延迟 | — | 6.3秒/次（优化前9.8秒，-35%） | ✅ |
| 单元+端到端回归 | — | pytest 84项（83 passed + 1环境跳过，含Phase 5的17项），E2E 8/8 | ✅ |

完整数据：`tests/results/`（batch_test_*.json、accuracy_eval_*.json、engine_accuracy_report_*.md、performance_profile_*.json、test_report_*.md）。阶段验收报告见 `docs/`。

> 准确率口径说明：10个标注用例的预期关键词客观取自知识库原文；"澄清反问"不计作答（澄清话术常罗列具体选项，宽松口径会被关键词误判）。

## 🏗️ 项目结构

```
707-kf-xiaoli/
├── backend/                    # 后端
│   ├── main.py                 # FastAPI入口（WebSocket + /healthz + /metrics）
│   ├── config.py               # pydantic-settings配置（缺失必需字段启动报错）
│   ├── models.py               # 数据模型
│   ├── connection_manager.py   # WebSocket连接管理
│   ├── wait_aggregator.py      # 等待汇总层（滑动窗口+倒计时）
│   ├── knowledge.py            # 知识库解析/分类/相关度检索
│   ├── metrics.py              # 运行时指标收集
│   ├── decision_layer/         # 决策层：base/rule_engine/jev_client/kev_client
│   ├── orchestrator/           # 编排层：router/handlers/prompt_builder/few_shot_examples
│   └── llm/                    # Qwen客户端（持久连接）+ 容错JSON解析
├── static/                     # 前端（深色主题Web界面）
├── data/sdwan.md               # 知识库（58个问答对，7个分类）
├── tests/                      # 测试脚本与结果（tests/results/为报告产物）
├── docs/                       # PRD/TDD/各阶段任务书与验收报告/api.md
├── scripts/                    # Kev GPU部署辅助脚本
├── logs/                       # 运行日志（按日期轮转）与metrics.json
├── .env.example                # 配置模板（全项注释）
└── requirements.txt
```

## 🔧 常见问题

### 1. 启动报错：知识库文件不存在 / 配置缺失

`data/sdwan.md` 缺失或 `.env` 必填项（MODEL_API_BASE/MODEL_NAME/CONTEXT_TURNS/WAIT_SLIDE_SECONDS/WAIT_MAX_SECONDS/KNOWLEDGE_BASE_PATH/DECISION_ENGINE）缺失时，服务**启动即报错退出**（设计行为，禁止静默回退）。按报错字段名补齐即可。

### 2. Qwen模型连接失败

```bash
# 确认LLM API可达（以Ollama为例）
curl http://localhost:11434/v1/models
```

LLM调用超时60秒、失败自动重试1次；持续失败请检查 `MODEL_API_BASE` 与模型服务。

### 3. Kev引擎如何启用

Kev经本地kev.serve服务运行（真实模型 jaredpalmer/kev-0.5b / kev-0.8b）：

```bash
# 一次性环境准备（Python 3.12+，独立venv避免污染主环境）
py -3.13 -m venv .venv-kev
.venv-kev\Scripts\python.exe -m pip install torch "kev[serve] @ git+https://github.com/jaredpalmer/kev"

# 启动GPU服务（自动显存预检+预热；4GB卡建议0.8b）
.venv-kev\Scripts\python.exe scripts\kev_gpu_serve.py --model 0.8b --port 8009 --warmup
```

`.env` 中 `KEV_SERVE_URL=http://127.0.0.1:8009`（默认已配）。服务未启动时Kev决策自动降级（不阻塞对话）。HuggingFace直连不可用时脚本自动走hf-mirror镜像。**已知限制**：kev-0.8b为英文训练原型，noul判定头对中文无区分度，已按评估实测校准阈值（见 `backend/decision_layer/kev_client.py` 注释）；4GB显存卡无法开启CUDA graphs，Kev-4B需≥10GB显存。

### 4. WebSocket连接断开

刷新页面即可，前端自动重连；服务端会话状态随连接断开清理。

### 5. 为什么有的问题会先反问澄清

意图不明确的短消息（<8字、无上下文、未超2次）会返回带具体选项的澄清问句（如"多少钱"→列出四档线路价格供选择），这是防止盲目猜测的产品设计；长而具体的问题直接进LLM生成。

## 📝 开发计划

- [x] Phase 1：FastAPI + WebSocket基础框架、知识库、Web界面
- [x] Phase 2：决策层（规则/Jev/Kev真实可用）+ 工厂切换
- [x] Phase 3：LLM生成层（Few-shot/Prompt/路由/编排器）+ 深色主题前端
- [x] Phase 4：批量测试、准确率评估、性能优化、日志监控、文档交付
- [x] Phase 5：Qwen引擎（第4种）+ 五级情绪风险 + 决策结果展示 + 会话自动提炼

### 后续建议

1. 收集真实用户对话，扩充标注集与Few-shot示例（当前标注集10例为知识库推导）
2. 接入中文校准的决策模型（Kev英文原型对中文noul无区分度，见FAQ第3条）
3. 知识库热更新（当前重启生效）
4. 对接微信/飞书等平台渠道

## 📄 许可与联系

内部Demo项目，保留所有权利。注意：`.env` 含密钥已gitignore；Jev API数据出境需评估合规。

---

**注意事项**：
1. 知识库`data/sdwan.md`包含产品信息，注意保密
2. 生产环境部署前请充分测试
3. 接口协议与配置详情见 [docs/api.md](docs/api.md)
