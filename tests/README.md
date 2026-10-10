# 测试说明

对应各阶段任务书的"完成条件"与验收命令，可独立复跑。

## 运行方式（使用项目虚拟环境）

```bash
# 激活虚拟环境
# Windows: .venv\Scripts\activate

# 全量单元验收（pytest自动收集test_*.py，共84项）
pytest tests/

# 各阶段单元验收（两种方式等价）
python tests/test_phase1_units.py
pytest tests/test_phase1_units.py

# 端到端验收（单元 + E2E，E2E自动启停服务）
python tests/run_all.py

# 仅端到端验收（自动在8001端口启停服务，可用环境变量E2E_PORT改端口）
python tests/e2e_test.py
```

> 收集范围由根目录 `pytest.ini` 约定为 `test_*.py`：`batch_test.py`/`e2e_test.py` 是
> 脚本式验收工具，禁止被pytest收集时import（会以真实.env实例化settings污染测试配置）。

## 覆盖范围

| 测试文件 | 对应任务书 | 内容 |
|---------|-----------|------|
| test_phase1_units.py | Phase 1任务1（1.2-1.5）、任务2（2.1-2.2） | 配置加载/缺失字段报错、数据模型、连接管理器、等待汇总（滑动窗口/max_seconds封顶/倒计时推送/取消/断开清理）、知识库加载/分类/意图检索/错误处理 |
| test_phase2_units.py | Phase 2任务1-5 | 决策层接口（创建/缺失字段/抽象类）、规则引擎6项测试+边界、Jev结构/超时回退/noul校准阈值、Kev配置/优雅回退/JSON容错/noul校准阈值、工厂切换与无效值、对比脚本（10+问题）、决策引擎集成main.py |
| test_phase3_units.py | Phase 3任务1-4 | LLM客户端结构/真实调用（API不可达时标准skip）/超时语义、JSON解析器4例、Few-shot结构+每意图示例数量(3-5)、示例选择器、Prompt构建器、路由决策5例、处理路径3例 |
| test_phase4_units.py | Phase 4任务1-4 | 指标收集（记录/快照/重置/落盘）、报告生成器、批量问题文件解析、准确率严格评分口径（澄清不计作答）、LLM生成JSON残缺重试、知识库全库检索、QwenClient连接复用、性能分析器 |
| test_phase5_units.py | Phase 5任务1/2/5 | Qwen引擎（工厂创建/合法JSON解析/乱码降级/超时降级/complaint_risk强制转人工）、五级情绪（rule输出/Jev与Kev选项/**Jev与Kev两条解析路径契约**：complaint_risk+escalate=false强制升级、非法negative降级neutral/路由complaint_risk保底）、会话提炼（默认20秒配置/计时被取消/编号60起/编号接续+sdwan.md不变/失败只记日志/同会话仅一次；Qwen一律用假客户端） |
| test_phase6_units.py | Phase 6任务1/2/8 | SQL行解析（转义引号/换行/双写）、文本提取（角色判定/群前缀/XML丢弃/早期AI遗留行）、去噪、去重（窗口内/角色与键区分）、会话切分（30分钟间隔）、业务筛选、轮次组装、脱敏（PII打码/身份映射稳定/无wxid与手机号残留）——全部用假SQL行，不读真实文件 |
| run_e2e_tests.py | Phase 3任务5-7 | 8用例端到端（WebSocket全流程，顺序/连续两种模式）+ --perf性能模式（FAQ 10次+LLM路径3次采样） |
| e2e_test.py | Phase 1任务1.6/4.2、暗卷1-3项；Phase 2任务5.3；Phase 4/5引擎切换与情绪契约 | 单条消息回复（真实LLM）、3条连续消息仅1次回复、清空对话重置、回复携带编排元数据、**四引擎逐一运行时切换**（ack+/engine核对）、**投诉风险黑盒转人工+七字段元数据完整性**、启动失败暗卷2项 |

预期结果：pytest六套合计98项（97 passed + 1 skipped，P1:13/P2:23/P3:20/P4:11/P5:17/P6:14），
E2E 8/8，`run_all.py` 退出码为 0。

## Phase 4 批量测试与评估工具

| 脚本 | 用法 | 输出 |
|---|---|---|
| batch_test.py | `python tests/batch_test.py tests/test_questions_final.txt [--engine rule\|jev\|kev]` | tests/results/batch_test_*.json |
| generate_report.py | `python tests/generate_report.py tests/results/batch_test_*.json` | tests/results/test_report_*.md |
| accuracy_evaluation.py | `python tests/accuracy_evaluation.py tests/accuracy_test_cases.json [--engine ...]` | tests/results/accuracy_eval_<engine>_*.json |
| compare_engine_accuracy.py | `python tests/compare_engine_accuracy.py`（自动取各引擎最新结果） | tests/results/engine_accuracy_report_*.md |
| performance_profile.py | `python tests/performance_profile.py [--repeats 2]` | tests/results/performance_profile_*.json |
| eval_accuracy.py | Phase 3评估集（20题kb_refs口径）复核用 | docs/acceptance-evidence/ |
| compare_engines.py | `python tests/compare_engines.py`（决策层引擎对比，输出engine_comparison.json） | 项目根 |
| extract_wx_sessions.py | `python scripts/extract_wx_sessions.py [--gap-minutes 30] [--dedup-seconds 60]`（解析微信SQL→去噪→去重→会话切分→业务筛选→脱敏；真实SQL被gitignore） | data/wx_real/sessions.txt / sessions.json / stats.md；mapping.json不入库 |
| prelabel_labeled.py | `python scripts/prelabel_labeled.py [--count 100]`（从脱敏会话抽100条单句，qwen预标意图+情绪，reviewed=false待人工复核） | data/wx_real/labeled.json |
| compare_real_sessions.py | `python tests/compare_real_sessions.py [--sessions 30] [--include-jev]`（同一批脱敏会话跑rule/kev/qwen；jev默认关闭——真实数据出境红线，--include-jev仅对脱敏文本） | tests/results/real_compare_*.md（含情绪专项、意图不一致清单、新旧对比与重复率） |

评估口径：accuracy_test_cases.json 的 expected_keywords 客观取自知识库原文；
**严格口径**——澄清反问不计作答（澄清话术常罗列具体选项，会被关键词误判），
澄清单列 clarification_rate 指标。

浏览器UI人工检查清单（Phase 1任务3.3）不在自动化范围内，最近一次执行证据见
`docs/acceptance-evidence/20261009-phase3-ui-checklist.md`（含截图）。
