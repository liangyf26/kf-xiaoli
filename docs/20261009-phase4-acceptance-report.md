# Phase 4 验收报告：测试和文档

**验收对象**：`cb06dcee0c26f93e31067db23c2eeeb9facb9424`
**验收日期**：2026-10-09
**依据**：`docs/20261008-phase4-testing-documentation-taskbook.md`
**验收环境**：Windows，Python 3.11，项目 `.venv`，本地 Qwen 兼容 API；Jev 使用配置的远程决策服务，Kev 使用配置的本地 GPU 服务

## 1. 最终结论

`cb06dce` 已完成 Phase 4 的主要交付：批量测试、自动报告、标注准确率评估、三引擎对比、性能画像与优化、日志轮转、运行时指标、README/API/配置/交付文档，以及对应的单元和回归测试。

本次独立复验结果满足任务书硬指标：

- Phase 4 专项测试：`11 passed`
- 四阶段 pytest：`66 passed, 1 skipped`
- Phase 1/2/既有 E2E：`13/13`、`23/23`、`6/6`
- 批量测试：`20/20` 正常回复，平均 `3.335s`，最大 `10.50s`
- 当前三引擎严格准确率：rule `100%`、Jev `100%`、Kev `90%`
- 当前性能画像 LLM 平均 `6195.5ms`，为主要瓶颈
- `/healthz`、`/metrics`、`POST /metrics/reset` 和 WebSocket 三态均正常
- YouTube 知识库外拒答通过 WebSocket 验证，并经独立编排器连续 3 次稳定复现

**最终判定：Phase 4 通过，附带模型输出波动和评估集规模有限的质量限制。**

“通过”指任务书规定的 Phase 4 交付与硬指标已满足；并不表示真实 LLM 在所有知识库外问题、所有引擎和所有重复运行中都具有确定性答案质量。

## 2. 提交内容复核

`cb06dce` 新增或修改了以下主要范围：

- 批量测试和报告：`tests/batch_test.py`、`tests/generate_report.py`、`tests/test_questions_final.txt`
- 准确率和引擎对比：`tests/accuracy_evaluation.py`、`tests/accuracy_test_cases.json`、`tests/compare_engine_accuracy.py`
- 性能画像：`tests/performance_profile.py`
- 单元测试：`tests/test_phase4_units.py`
- 日志和指标：`backend/metrics.py`、`backend/main.py`
- 质量修复：知识库相关度检索、来源白名单、JSON 残片重试、规则/Jev/Kev 阈值校准、知识库外对象拒答 Prompt 约束
- 文档与配置：`README.md`、`docs/api.md`、`.env.example`、`DELIVERY_CHECKLIST.md`、`PROGRESS.md`、`tests/README.md`、`pytest.ini`

提交文件已通过 `git diff --check`，未发现提交自身的空白格式错误。

## 3. 自动化测试

### 3.1 Phase 4 专项测试

执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/test_phase4_units.py
```

结果：

```text
11 passed in 0.40s
```

覆盖指标记录、重置和落盘，批量问题文件的多轮会话解析，Markdown 报告生成，严格准确率评分，澄清不计作答，拒答判定，JSON 残缺重试，全库检索，Qwen 持久连接复用和性能分析器。

### 3.2 四阶段 pytest 回归

执行四套测试文件的结果：

```text
66 passed, 1 skipped in 44.54s
```

其中真实 LLM 探测按设计在外部 API 不可用时跳过。`pytest.ini` 仅收集 `test_*.py`，避免将脚本式的 `batch_test.py`、`e2e_test.py` 自动收集并污染 Phase 1 测试的环境配置。

### 3.3 全量验收入口

`tests/run_all.py` 结果：

```text
Phase 1 单元：13/13
Phase 2 单元：23/23
既有端到端：6/6
全部验收通过
```

## 4. 批量测试和报告

使用 `tests/test_questions_final.txt` 运行 `tests/batch_test.py`，最新结果文件为：

`tests/results/batch_test_20261009-175507.json`

最新实测：

| 指标 | 结果 | 任务书要求 |
|---|---:|---:|
| 问题数 | 20 | 20+ |
| 会话数 | 19 | 支持多轮 |
| 正常回复 | 20/20 | 通过率 ≥90% |
| 平均编排延迟 | 3335ms | 平均 <10s |
| 最大编排延迟 | 10500ms | 任务书批量目标为平均 <10s；最大值需关注 |
| 墙钟时间 | 66.7s | 未单独设硬阈值 |

自动报告为：

`tests/results/test_report_20261009-175610.md`

报告包含总览、平均/最大延迟、引擎统计、路径统计、意图分布、逐题结果和失败列表。需要明确：批量脚本的“通过”定义是单题未抛异常并得到正常流程结果，不是逐题人工语义正确率。最新报告中虽然 `20/20`，但仍有若干题返回澄清或标准拒答，这一指标不能替代标注准确率。

## 5. 准确率与三引擎复验

### 5.1 评估集和评分口径

`tests/accuracy_test_cases.json` 包含 10 个标注用例，覆盖价格、技术支持、客户端下载、知识库外 YouTube、网速、试用、路由器、使用和套餐等场景。

严格评分规则为：

- `should_answer=true`：预期关键词至少命中一个，且不能是拒答或澄清。
- `should_answer=false`：必须正确拒答。
- 预期来源非空时，至少命中一个来源编号。
- 澄清问题单独统计，不计作答正确。

“可以试用吗”标注为可回答且来源为问题16，符合当前知识库中“可以试用的”事实；任务书原始示例将其作为拒答案例，与知识库内容冲突，提交中已按真实知识库修正。

该评估仍是 10 题小样本，关键词命中是代理指标，不是人工语义评分；报告中的 100% 或 90% 不能外推到所有用户问题。

### 5.2 当前复跑结果

本次分别执行 rule、jev、kev 后，最新三引擎对比报告为：

`tests/results/engine_accuracy_report_20261009-175949.md`

| 引擎 | 答案准确率 | 来源准确率 | 拒答准确率 | 平均延迟 |
|---|---:|---:|---:|---:|
| Rule | 100%（10/10） | 100%（9/9） | 100%（1/1） | 5926ms |
| Jev | 100%（10/10） | 100%（9/9） | 100%（1/1） | 7854ms |
| Kev | 90%（9/10） | 88.9%（8/9） | 100%（1/1） | 7987ms |

Kev 唯一失败题为“怎么使用”：本次模型生成标准拒答，未返回预期的客户端/路由器/安装等关键词，来源也为空。该失败不是脚本异常，而是严格准确率评估明确记录的真实质量失败；不过 Kev 当前仍达到任务书答案准确率 `≥85%` 的门槛。

提交内历史快照记录三引擎均为 `100%`。本次复跑显示 Kev 结果下降到 `90%`，说明模型输出存在随机性或服务状态波动。验收以当前复跑结果为准，并保留历史快照作为提交时证据。

## 6. 性能画像与优化

最新性能画像：

`tests/results/performance_profile_20261009-180131.json`

本次代表问题集运行 1 轮，组件平均耗时为：

| 组件 | 平均耗时 |
|---|---:|
| decision | 0.0ms |
| kb_retrieval | 0.0ms |
| prompt_build | 0.0ms |
| json_parse | 0.0ms |
| llm_generate | 6195.5ms |

瓶颈识别为 `llm_generate`，与提交内历史画像一致。提交历史记录的优化前后批量平均延迟为 `4395ms → 3164ms`，约 `28.0%` 改善；本次独立批量复跑为 `3335ms`，相对 `4395ms` 仍改善约 `24.1%`。由于 LLM 生成耗时具有波动性，不将单次差异视为严格性能基准。

代码层可复核的优化包括知识库相关度 Top-6 注入和 Qwen `AsyncClient` 持久连接复用。性能画像确认决策、检索、Prompt 组装和 JSON 解析相对模型推理可忽略，模型服务本身仍是主要瓶颈。

## 7. 日志、指标和服务黑盒验证

为避免干扰已有 `8000` 端口进程，本次使用当前代码在 `8001` 启动隔离服务验证。已有 `8000` 服务的 `/healthz` 可访问，但 `/metrics` 返回 404，确认其不是当前 `cb06dce` 版本；未终止该进程。

当前版本 `8001` 验证结果：

- `GET /healthz`：HTTP 200，返回 `qa_pairs=58`
- `GET /metrics`：HTTP 200，初始计数为 0
- `POST /metrics/reset`：HTTP 200，计数清零并刷新启动时间
- WebSocket 消息状态顺序：`waiting → thinking → response`
- `支持YouTube吗`：返回“暂时无法回答，需要人工介入”，来源为空
- 请求完成后指标：`total_requests=1`、`total_errors=0`、平均延迟 `6888ms`、路径为 `llm_generation`

应用日志 `logs/app.log` 已包含用户消息、编排决策、路由路径、LLM 调用和回复发送等关键事件；日志文件共有 1151 行。实现使用按日期轮转的 `TimedRotatingFileHandler`，保留 14 天。

## 8. YouTube 知识库外回归

这是上一阶段的阻断项，本次重点复验：

1. 当前代码通过真实 WebSocket 发送 `支持YouTube吗`，收到标准拒答且 `sources=[]`。
2. 独立 `Orchestrator.process("支持YouTube吗", {})` 连续调用 3 次，均返回：
   `暂时无法回答，需要人工介入`
3. `tests/accuracy_evaluation.py` 的 rule、Jev、Kev 当前复跑均将该题判定为正确拒答。

因此，`cb06dce` 已有效改善并稳定复现该场景；上一阶段报告中的 YouTube 回归不再阻断 Phase 4 验收。

## 9. 文档与交付清单

静态检查结果：

- README 长度 `6352` 字符，包含“快速开始”“使用说明”“项目结构”“测试”“常见问题”等必需章节。
- `.env.example` 包含任务书要求的配置项：`MODEL_API_BASE`、`MODEL_NAME`、`DECISION_ENGINE`、`CONTEXT_TURNS`、`WAIT_SLIDE_SECONDS`、`WAIT_MAX_SECONDS`、`JEV_API_KEY`、`KEV_MODEL_PATH`、`NO_ANSWER_MESSAGE`，共 29 行注释。
- 关键模块 `orchestrator.py`、`router.py`、`rule_engine.py`、`llm/client.py` 的类和公开函数 docstring 检查通过。
- `docs/api.md` 存在并覆盖 `/ws`、`/healthz`、`/metrics`、`DECISION_ENGINE` 等接口和配置说明。
- `DELIVERY_CHECKLIST.md` 已列出代码、文档、测试、指标、Git、运行可用性和已知限制。
- `PROGRESS.md` 已记录 Phase 4 实施范围和历史结果。

### 文档一致性限制

`PROGRESS.md` 和 `DELIVERY_CHECKLIST.md` 记录的是提交时历史快照：三引擎均为 `100%`、平均响应 `3164ms`。本次复跑得到 Kev `90%`、平均 `3335ms`。两者不矛盾，但应理解为不同时间点的模型运行结果；文档没有明确把这些数字标注为“历史快照”，建议后续在结果摘要中增加运行时间和随机性说明。

## 10. 遗留项与建议

1. Kev 运行结果存在波动：当前“怎么使用”一次标准拒答，准确率仍为 `90%`，但来源准确率降为 `88.9%`。建议增加重复运行或提高模型输出确定性，并把失败样本纳入回归集。
2. 评估集只有 10 题，且答案采用关键词代理评分；建议扩充人工标注的真实用户案例，尤其覆盖知识库外问题和多轮上下文。
3. 批量测试当前只统计异常/正常，不统计语义正确率；建议增加逐题期望标签或引用准确率判定。
4. 运行黑盒服务时如果默认 `8000` 已被旧版本占用，任务书命令可能验证到错误版本；建议启动脚本增加端口探测或 `/healthz` 版本校验。
5. 本次未重新执行浏览器截图级 UI 黑盒验收；Phase 3 提交已有 UI 清单和截图，Phase 4 主要验收测试、文档、日志和指标。

## 11. 验收判定

Phase 4 的核心目标是测试和文档交付。本次已独立验证：

- 测试脚本可运行，四阶段回归通过；
- 批量测试和 Markdown 报告可生成；
- 三引擎准确率评估可运行，当前结果达到 `≥85%`；
- 性能瓶颈可定位，优化前后有可复核数据；
- 日志、指标接口和 WebSocket 核心流程可用；
- README、API、配置模板、交付清单和代码注释齐全；
- 上一阶段 YouTube 知识库外拒答回归已修复并稳定通过。

**结论：Phase 4 通过。**

附带限制：Kev 单次复跑低于历史 100% 但仍达标；准确率评估集规模小且是关键词代理；批量 100% 仅代表流程无异常；本次未重复浏览器截图验收。
