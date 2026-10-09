# Demo交付物检查清单（2026-10-09核验，2026-10-10随Phase 5同步）

## 代码和配置
- [x] backend/ 目录完整（所有模块：main/config/models/connection_manager/wait_aggregator/knowledge/metrics + decision_layer/orchestrator/llm子包）
- [x] static/ 目录完整（index.html/app.js/style.css，深色主题）
- [x] data/sdwan.md 知识库文件（58个问答对）
- [x] .env.example 配置模板（全项注释，29行注释）
- [x] requirements.txt 依赖清单
- [x] .gitignore 正确配置（.env/日志/模型权重不入库）

## 文档
- [x] README.md 完整清晰（快速开始/使用说明/项目结构/测试/常见问题，6188字符）
- [x] docs/产品需求文档-20261008.md
- [x] docs/技术设计文档-20261008.md
- [x] docs/Phase 1-5 任务书（5个文件，命名yyyymmdd-phaseN-*-taskbook.md）
- [x] docs/api.md（WebSocket协议/HTTP端点/配置项/决策引擎接口）
- [x] docs/20261008~20261009-*.md 验收报告（P1两轮+第三轮/P2/P3两轮）

## 测试
- [x] tests/batch_test.py 批量测试脚本（多轮会话支持、引擎可选）
- [x] tests/accuracy_evaluation.py 准确率评估（严格口径）
- [x] tests/test_questions_final.txt 测试问题（20问19会话）
- [x] tests/accuracy_test_cases.json 标注数据（10例，预期客观取自知识库）
- [x] tests/generate_report.py / compare_engine_accuracy.py / performance_profile.py
- [x] tests/results/ 目录包含测试结果和报告（JSON+Markdown）
- [x] pytest五套单元验收 84项（83 passed + 1环境跳过，P1:13/P2:23/P3:20/P4:11/P5:17）；E2E 8/8

## 验收指标（实测）
- [x] 批量测试通过率 ≥90% → **100%**（20/20正常回复）
- [x] 答案准确率 ≥85% → **rule 100% / jev 100% / kev 100%**（严格口径：澄清不计作答）
- [x] 平均响应时间 <10秒 → **3.16秒**（优化前4.40秒；LLM路径9.77秒→6.33秒）
- [x] 3种决策引擎可切换 → rule/jev/kev实测评估均通过（kev经GPU kev.serve真实推理）
- [x] Web界面功能正常（倒计时、思考状态、答案来源，截图留证docs/acceptance-evidence/）

## Git仓库
- [x] 所有代码已提交
- [x] commit message清晰（中文、分阶段）
- [x] 已推送到GitHub（master）
- [x] 无敏感信息泄露（.env已忽略；.env.example无真实密钥）

## 可运行性
- [x] 按照README可成功安装依赖（py -3.11 -m venv .venv + pip install -r requirements.txt）
- [x] 配置.env后可成功启动（缺必需配置启动即报错，字段名明确）
- [x] Web界面可正常访问（http://localhost:8000，/healthz健康检查）
- [x] 可以正常对话并收到回复（WebSocket waiting/thinking/response三态）

## 已知限制（如实记录）
- kev-0.8b为英文训练原型：noul判定头对中文无区分度（评估实测），已按数据校准阈值；
  中文校准模型接入后应恢复0.5阈值（见backend/decision_layer/kev_client.py注释）
- Kev GPU延迟约1秒/次（4GB卡禁用CUDA graphs所致），任务书<500ms目标需≥8GB显存卡
- Kev-4B需≥10GB显存（RTX 2050 4GB物理不可行，kev包无量化支持）
- 标注评估集10例规模小（知识库推导），后续应扩充人工复核的真实案例
