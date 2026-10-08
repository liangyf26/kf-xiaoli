# Phase 1 进度记录

## 2026-10-08
- 任务0: 环境验证通过（Python 3.11.0，依赖安装成功，知识库存在）

## Phase 1 完成情况

### 已实现功能
- ✅ FastAPI + WebSocket基础框架
- ✅ 配置管理（.env加载，缺失必需字段启动报错）
- ✅ 数据模型（Message, ConversationContext, WaitingQueue, SessionState）
- ✅ WebSocket连接管理器（独立SessionState，断开清理定时器）
- ✅ 等待汇总层（滑动窗口15秒，30秒封顶，倒计时推送）
- ✅ 知识库加载和解析（58个问答对，回退纯文本模式）
- ✅ 自动分类（7个类别：price/usage/troubleshooting/product/purchase/technical/general）
- ✅ Web界面（HTML + CSS + WebSocket客户端，倒计时/清空/断线重连）

### 技术指标
- 知识库问答对数量: 58（sdwan.md编号条目1-33、35-59，编号34原文缺失）
- 代码文件数: 后端6个模块 + 前端3个文件
- 单元/验收测试: 配置加载2项、数据模型1项、连接管理1项、等待汇总1项、知识库4项，全部通过
- 端到端测试: 通过（发消息→等待15秒→汇总→Echo回复）
- 浏览器实测: 倒计时显示、回复渲染、清空对话、空消息拦截、回车发送均正常
- 隐藏暗卷3项: 3条连续消息仅1次回复 ✓ / 删知识库启动报错退出 ✓ / 缺配置字段报错 ✓

### 待Phase 2实现
- 规则引擎决策层
- Jev API集成
- Kev模型部署
- LLM客户端（Qwen27B）
- 真实回复生成（目前是Echo mock）

### 遗留问题
- requirements.txt中transformers/torch/sentencepiece（Phase 2 Kev用）未安装，避免demo阶段拉取2GB+依赖
- 知识库第59条为空标题空内容条目，分类归入general
- 分类基于关键词规则，个别条目归类可进一步优化（如38"个别设备不能用"归入usage）
- 前端空消息拦截为前端+服务端双重校验，服务端静默丢弃空消息

### 环境说明
- 系统默认Python为3.9（不满足要求），虚拟环境使用 py -3.11 创建（.venv，Python 3.11.0）

## 2026-10-08 验收证据整改（审查意见：验收证据缺失）

**问题**：初版验收命令以临时脚本执行，未随提交落库（tests/仅有.gitkeep），PROGRESS.md记录的测试/端到端/浏览器实测结果无法从提交独立复核。

**整改**：验收命令固化为可重复运行的测试用例，已随提交入库：
- `tests/test_phase1_units.py` — 任务书任务1（1.2-1.5）与任务2（2.1-2.2）全部验收命令，共9项，兼容pytest
- `tests/e2e_test.py` — 自动启停服务的3个端到端场景：单条消息Echo回复、3条连续消息仅1次回复（滑动窗口）、清空对话后会话重置
- `tests/run_all.py` — 一键全量验收入口
- `tests/README.md` — 运行说明与覆盖范围对照表

**复跑结果**（本次提交前实测）：
- `python tests/run_all.py` → 单元验收 9/9 通过，端到端验收 3/3 通过，退出码0
- `pytest tests/test_phase1_units.py` → 9 passed

**说明**：浏览器UI人工检查清单（任务3.3）按任务书决策5为手动验收项，不纳入自动化；自动化行为验证以 tests/e2e_test.py 为准。

## 2026-10-08 第二轮验收整改（验收报告：docs/20261008-phase1-acceptance-report.md）

针对报告6项发现逐条整改：

| 报告发现 | 整改措施 | 结果 |
|---------|---------|------|
| 1.等待层边界未覆盖（max_seconds封顶/倒计时内容/取消） | 新增test_wait_aggregator_max_seconds_cap（2.7-3.6秒窗口断言封顶触发）、test_wait_aggregator_countdown_push（remaining_seconds/message_count字段）、test_wait_aggregator_cancel（取消后不触发+缓冲回收） | ✅ |
| 2.断开清理未覆盖 | 新增test_connection_manager_disconnect_cancels_timer（连接管理器+等待汇总联动） | ✅ |
| 3.启动失败暗卷未自动化 | e2e新增scenario_startup_missing_knowledge_file（OS环境变量覆盖知识库路径指向不存在文件，断言FileNotFoundError退出）和scenario_startup_missing_knowledge_config（临时ENV_FILE仅缺KNOWLEDGE_BASE_PATH，断言ValidationError+字段名，证明未使用默认值） | ✅ |
| 4.重复回复检查窗口过短（2秒） | 检查窗口延长到首条消息起max_seconds封顶+5秒缓冲（时长从服务端配置读取，非硬编码） | ✅ |
| 5.E2E可能误连其他进程 | 端口默认自动选择空闲端口（E2E_PORT仍可固定）；就绪检查改为/healthz并校验应用标识sdwan-kf-xiaoli；轮询期间监测子进程存活，提前退出即失败并输出服务日志尾部 | ✅ |
| 6.pytest模式残留临时目录和ENV_FILE | 模块导入时保存原ENV_FILE；teardown_module（pytest自动调用）+atexit+脚本finally三重清理，幂等；实测pytest/脚本两种模式运行后临时目录残留0个 | ✅ |
| 报告"浏览器手动项未执行" | 重跑§3.3全部6项检查（真实浏览器），6/6通过，截图与结果表留存于docs/acceptance-evidence/ | ✅ |

**整改后复跑结果**：
- `python tests/run_all.py` → 单元验收 13/13，端到端验收 5/5，退出码0
- `pytest tests/test_phase1_units.py` → 13 passed
- 临时目录残留：pytest模式0个、脚本模式0个
- 代码变更：backend/main.py新增GET /healthz健康检查端点（返回应用标识与问答对数量，供E2E身份校验）

**遗留说明**：报告第4项（浏览器项）已由本次执行留证，但按任务书决策5仍属人工验收范畴，
后续版本需复跑清单（docs/acceptance-evidence/20261008-phase1-ui-checklist.md）。

## Phase 2 开始（2026-10-08）
- 任务0: 环境验证通过，开始决策层开发（规则引擎/Jev API/Kev-0.5B）

## Phase 2 完成情况（2026-10-08）

### 已实现功能
- ✅ 决策层抽象接口（DecisionResult/DecisionEngine，backend/decision_layer/base.py）
- ✅ 规则引擎（关键词匹配6意图+unclear，<100ms，backend/decision_layer/rule_engine.py）
- ✅ Jev API集成（TypeSafe问题定义6项，5秒超时回退，backend/decision_layer/jev_client.py）
- ✅ Kev-0.5B本地模型（懒加载/CPU-GPU/JSON解析容错，backend/decision_layer/kev_client.py）
- ✅ 决策引擎工厂（create_decision_engine，配置/环境变量切换，无效值报错）
- ✅ 决策引擎集成到main.py（回复payload携带intent/engine元数据，澄清计数与会话上下文联动）
- ✅ 3引擎对比测试脚本（tests/compare_engines.py，10个问题，输出engine_comparison.json）

### 技术指标
- 规则引擎响应时间: 0-1ms（要求<100ms）✅
- 意图识别类别: 7种（price_inquiry/product_comparison/technical_support/usage_guide/troubleshooting/purchase_process/unclear）
- 对比测试: 10个问题，规则引擎全部输出合理意图，engine_comparison.json已生成
- 单元验收: Phase 2共21项全过（接口3+规则9+Jev3+Kev3+工厂2+对比1）；pytest两套合计34 passed
- e2e: 6/6（新增决策元数据场景，验证WebSocket回复携带intent/engine字段）

### 对比结果（tests/compare_engines.py tests/test_questions_phase2.txt）
- 规则引擎: 平均延迟0.0ms；10个问题中9个识别出具体意图，"要直播的线路"无关键词命中归为unclear
- Jev: 未配置真实API_KEY，按任务书决策3跳过真实调用（记录skipped原因）
- Kev: 模型不可用，全部回退kev_failed（详见下方遗留问题）

### 验收官暗卷3项实测
1. 口语"咋整": 规则引擎按任务书关键词字典（咋/咋整在usage_guide列表）识别为usage_guide并触发澄清（长度<5字）；
   Jev/Kev不可用回退unclear。注：任务书暗卷预期troubleshooting与其自身关键词字典（咋整→usage_guide）存在矛盾，
   已按任务书字典实现并如实记录，待真实案例评估阶段（任务书决策5）修正词表
2. 超长文本1000字: 三引擎均正常返回DecisionResult，无崩溃（规则0ms/Jev回退1046ms/Kev回退0ms）
3. 快速切换DECISION_ENGINE三次（rule/jev/kev）: 每次工厂均创建正确引擎类型

### 待Phase 3实现
- LLM客户端（Qwen27B）
- Prompt构建器和Few-shot示例
- 路由器和编排层（按决策结果路由：澄清/FAQ匹配/LLM生成/转人工）
- 真实回复生成（替换Echo mock）

### 遗留问题
- **Kev模型依赖问题（任务书止损规则2）**: 本机网络无法访问huggingface.co（对照组已知模型同样返回000），
  tt-hous/kev-0.5b无法下载；transformers/torch未安装（避免拉取2GB+依赖）。引擎代码完整
  （懒加载+JSON容错+优雅回退），依赖安装且模型可用后即可启用真实推理
- **Jev真实调用未验证**: 无真实API key（任务书决策3允许），结构/超时回退已验证，接入真实key后
  运行 tests/compare_engines.py 即可对比
- "网速慢怎么办"被规则引擎归为usage_guide（"怎么"与"慢"同分时按字典序优先usage_guide），
  词表优先级待真实案例调优

## 2026-10-08 Jev接入OpenRouter decisions端点（真实调用打通）

**背景**: api.typesafe.com域名公网DNS不存在（NXDOMAIN）；改走OpenRouter后确认
typesafe/jev-1.13为System One结构化决策模型，须使用专用/api/alpha/decisions端点（不支持chat/completions）。

**实测确认的decisions契约**（通过zod校验错误逐轮探测）:
- POST {JEV_API_BASE}/alpha/decisions，body: {model, state, questions}
- state: string | record | array（传record含message/context/previous_intent等）
- questions判别键为**type**（非kind），值noul/choice/score
- choice/score问题需**instructions+criteria**（criteria: choice=record选项→说明，score=数组分档）
- noul问题仅需instructions；score分档**最多10档**（超限报"Too many score levels"）
- score返回criteria**分档索引值**（非0-100原值），按档数归一化映射回0-1/0-100
- 响应answers.{字段}.{choice|noul|score}+probabilities+confidence，附usage与成本

**JevEngine适配**: questions定义含instructions/criteria（score各10档）；noul按>=0.5判定；
score按(档数-1)归一化；解析失败/HTTP错误均回退jev_failed且错误信息携带响应体；真实调用实测:
- "多少钱"→price_inquiry(conf 0.85)、"tiktok登不上怎么办"→technical_support、
  "你们这个垃圾产品不行"→negative情绪+escalate_to_human=True
- 延迟约1秒/次（TDD假设200ms，实测偏高，记录）；单次成本约$0.000036

**对比测试（10问题，Jev列启用真实调用）**: rule与Jev在8/10问题上判断一致；
Jev对模糊消息（"咋整啊"/"能不能直播"）返回unclear+澄清（更稳健）；
"网速慢怎么办"Jev判troubleshooting（比规则引擎的usage_guide更贴切）。

**验证**: pytest两套35 passed；e2e 6/6；配置更新（JEV_API_BASE=https://openrouter.ai/api、新增JEV_MODEL）。
