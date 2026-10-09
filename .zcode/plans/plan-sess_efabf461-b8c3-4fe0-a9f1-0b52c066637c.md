1. 固定验收基线
   - 核对当前 HEAD、origin/master 与 `cb06dce` 一致，记录工作区初始状态。
   - 只把本次验收报告和必要的复跑证据纳入后续提交，忽略运行日志、缓存和临时服务产物。

2. 按任务书执行自动化测试
   - 运行 `tests/test_phase4_units.py`，确认指标收集、报告生成、批量问题解析、严格准确率评分、JSON 残缺重试、全库检索、连接复用和性能分析器测试。
   - 运行 `tests/run_all.py` 及三阶段 pytest，记录 Phase 1/2/3/4 单测、既有 E2E 和 skip 数量，检查 `pytest.ini` 是否避免脚本式测试污染。
   - 运行 Phase 4 批量测试 `tests/batch_test.py tests/test_questions_final.txt`，生成 Markdown 报告并验证 20 个问题、19 个会话、结果字段和异常数量。

3. 复核准确率和三引擎对比
   - 使用 `tests/accuracy_evaluation.py tests/accuracy_test_cases.json` 分别运行 rule、jev、kev，确认 10 个标注用例的答案、来源、拒答和澄清严格评分口径。
   - 运行 `tests/compare_engine_accuracy.py`，核对三引擎结果文件、平均延迟、路径/意图差异及报告是否与原始 JSON 一致。
   - 明确报告限制：10 题小样本、关键词代理评分；同时检查 `可以试用吗` 的标注是否与知识库事实一致，而不是沿用任务书旧的错误预期。

4. 复核性能优化
   - 运行 `tests/performance_profile.py`，确认决策、知识库、Prompt、LLM、JSON 解析五类组件均有样本，瓶颈识别正确。
   - 对比提交内优化前后批量/性能结果，核对平均延迟、LLM 路径延迟和改进百分比是否可由结果文件计算得出，并记录模型推理占主要耗时这一限制。

5. 复核日志、指标和运行接口
   - 启动本地服务，调用 `/healthz`、`/metrics`、`POST /metrics/reset`，通过 WebSocket 发送至少一条消息并验证 waiting/thinking/response 流程。
   - 检查 `logs/app.log` 是否包含用户消息、决策、路由、LLM 调用、回复和异常相关日志；检查 `logs/metrics.json` 的请求数、引擎/路径分布、平均延迟和错误计数是否更新。
   - 服务验证完成后正常停止临时进程，不提交日志或缓存产物。

6. 复核文档与交付清单
   - 按任务书静态检查 README 必需章节、长度、启动/配置/测试命令、`.env.example` 配置和注释、关键模块 docstring、`docs/api.md`、`DELIVERY_CHECKLIST.md`、`PROGRESS.md` 和测试结果链接。
   - 检查提交内历史数字是否自洽，特别是批量通过率、准确率、平均响应、pytest 总数、三引擎延迟以及已知限制。

7. 重点回归与最终判定
   - 使用真实编排器直接验证 `支持YouTube吗`，再通过真实 WebSocket 验证 Phase 3 暗卷核心场景；确认是否稳定返回“暂时无法回答，需要人工介入”，并检查是否错误携带来源。
   - 将任何复现的知识库外幻觉、服务不可达、指标未接入或文档数字不一致列为验收遗留项，按严重度排序。
   - 基于所有新旧证据形成 `docs/20261009-phase4-acceptance-report.md`，明确通过项、限制、失败项和最终“通过/部分通过”结论。
   - 运行 `git diff --check`，只提交验收报告和必要的复跑证据，推送到 `origin/master`，最后确认远端提交和工作区状态。