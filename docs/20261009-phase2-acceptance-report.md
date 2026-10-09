# Phase 2 验收报告

**验收对象**：`a238974`（阶段 2 复查：三引擎真实可用，硬指标重审与对比测试更新）  
**验收日期**：2026-10-09  
**依据**：`docs/20261008-phase2-decision-layer-taskbook.md`  
**验收环境**：Windows，项目 `.venv`，Python 3.11  
**结论**：Phase 2 的决策层接口、规则引擎、Jev/Kev 客户端结构、工厂切换、主流程集成和自动化测试均可复核通过；但 Kev GPU 推理未达到任务书规定的 `<500ms` 硬指标，且本次验收环境未安装 `torch`，无法在当前环境重跑真实 Kev GPU。综合判定为 **Phase 2 部分通过，功能交付通过，性能硬指标未完全通过**。

## 1. 自动化验收结果

使用项目虚拟环境执行：

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe tests/run_all.py` | 通过：Phase 1 单元 `13/13`、Phase 2 单元 `23/23`、端到端 `6/6` |
| `.venv/Scripts/python.exe -m pytest -q tests/test_phase2_units.py` | 通过：`23 passed in 2.31s` |
| `.venv/Scripts/python.exe tests/compare_engines.py tests/test_questions_phase2.txt` | 通过：处理 10 个问题并生成 `engine_comparison.json`；该文件由 `.gitignore` 忽略 |

Phase 2 单元测试覆盖统一 `DecisionResult`/`DecisionEngine` 接口、规则引擎核心场景及边界、Jev 请求结构/解析/失败回退、Kev 配置/解析/失败回退、工厂切换和 `main.py` 集成。端到端测试验证 WebSocket 回复中的决策元数据。

## 2. 暗卷复测

本次使用独立命令复核任务书 §“验收官暗卷”：

| 场景 | 实际结果 | 判定 |
|---|---|---|
| 中文口语“咋整” | 规则引擎返回 `usage_guide`，并因消息过短触发澄清 | 按当前关键词表通过；与任务书暗卷期望 `troubleshooting` 不一致 |
| 1000 字输入 | 规则引擎正常返回 `DecisionResult`，无崩溃 | 通过 |
| 快速切换 `DECISION_ENGINE` 三次 | `rule`、`jev`、`kev` 分别创建 `RuleBasedEngine`、`JevEngine`、`KevEngine` | 通过 |

“咋整”的差异来自任务书规则词表同时将“咋整”列在 `usage_guide`，而暗卷又要求观察其是否识别为 `troubleshooting`。当前实现与词表一致，因此该项属于任务书内部预期冲突，不能将其记为代码测试失败；后续应由真实案例校准词表或明确暗卷期望。

## 3. 三引擎和对比结果

### 3.1 规则引擎

- 工厂可以创建并调用 `RuleBasedEngine`。
- 本地复测中规则引擎响应为 0ms 量级，满足平均 `<100ms` 要求。
- 10 个问题的对比脚本正常输出规则结果和延迟字段。

### 3.2 Jev

- `JevEngine` 已接入 OpenRouter `alpha/decisions` 端点，支持结构化 `choice`、`noul`、`score` 结果解析和失败回退。
- `PROGRESS.md` 记录了真实 OpenRouter 调用及约 1 秒级响应；本次 `.env` 未提供真实 Jev key，因此自动对比脚本按任务书决策 3 将 Jev 标记为 `skipped`，未宣称本次实时复测通过。

### 3.3 Kev

- `KevEngine` 支持本地 `kev.serve` HTTP 模式，并保留懒加载/失败回退路径。
- 本次项目 `.venv` 未安装 `torch`，对比脚本中的 Kev 记录为 `kev_failed` 回退；这是依赖缺失下的预期容错，不是实时模型推理证据。
- `PROGRESS.md` 保存了此前 RTX 2050 上 Kev-0.8B 的真实 GPU 证据：10 个问题均完成分类，实测延迟约 `719–1484ms`，另一批次记录约 `750–1110ms`，均值约 `0.94–1.1s`。
- 任务书硬指标为 GPU `<500ms`，因此 Kev GPU 性能 **未达标**。功能可用不能替代该性能指标的通过结论。

## 4. 硬指标判定

| 指标 | 任务书要求 | 验收证据 | 判定 |
|---|---|---|---|
| 三种引擎可用 | Rule、Jev、Kev 均能返回 `DecisionResult` | 代码、结构测试、历史 Jev/Kev 真实运行记录；本次本地 Kev 因依赖缺失回退 | 功能通过，当前环境未实时复跑 |
| 配置切换 | 修改 `DECISION_ENGINE` 后使用对应引擎 | 工厂独立复测三次通过 | 通过 |
| 规则引擎性能 | 平均 `<100ms` | 本地规则复测 0ms 量级 | 通过 |
| Kev 推理性能 | CPU `<1000ms` 或 GPU `<500ms` | RTX 2050 GPU 约 `719–1484ms`，未达到 `<500ms` | **未通过** |
| 规则测试 | 6 个核心测试通过 | Phase 2 单元测试及全量入口通过 | 通过 |
| 对比测试 | 10+ 问题并输出 JSON | 10 个问题，`engine_comparison.json` 成功生成 | 通过 |

## 5. 配置和文档遗留项

1. `.env.example` 的 `KEV_MODEL_PATH` 仍为 `tt-hous/kev-0.5b`。`PROGRESS.md` 已记录该模型 ID 不存在，实际可用模型/服务路径为 `jaredpalmer/kev-0.5b` 或后续的 Kev-0.8B 本地服务。模板应在后续变更中同步，避免按模板配置得到不可用模型路径。
2. 任务书标题和部分段落仍称 Kev-0.5B，而当前真实 GPU 复测使用 Kev-0.8B；本报告按实际运行证据记录，不把模型升级视为性能指标豁免。
3. 本报告不把 `engine_comparison.json` 作为提交交付物：它是测试生成物，已被 `.gitignore` 忽略；报告只引用本次运行结果和已保存的历史真实服务证据。

## 6. 最终判定

`a238974` 已完成 Phase 2 决策层的主要功能交付，并且本地自动化验收可重复通过：全量入口 `13 + 23 + 6` 项通过，Phase 2 单测 `23/23` 通过，对比脚本和三次工厂切换均可复核。由于任务书明确要求 Kev GPU `<500ms`，而当前 RTX 2050 实测约 `0.72–1.48s`，本阶段不能判定为全部硬指标通过。

**最终结论：Phase 2 部分通过；功能与自动化测试通过，Kev GPU 性能硬指标未通过，建议后续使用更高显存/支持 CUDA graphs 的硬件或重新调整经确认的性能目标后复验。**
