# AGENTS.md — SDWAN智能客服机器人 Demo

一句话定位：基于知识库接地生成的 SDWAN 专线客服机器人 demo——四引擎决策（rule/jev/kev/qwen）+ 四路编排（转人工>澄清>FAQ直连>LLM生成）+ WebSocket 实时前端，答案标注来源、知识库外必拒答。

## 怎么跑

```bash
# 环境：Python 3.11（系统默认 3.9 不达标，用 py -3.11 建 venv）
.venv\Scripts\python.exe -m backend.main   # 无需激活 venv
# 浏览器 http://localhost:8000 ；/healthz 健康检查、/metrics 指标、GET /engine 当前引擎
.venv\Scripts\python.exe -m pytest tests -q   # 全量单测，当前 84 项=83 passed+1 skipped
python tests/run_all.py                        # 单元+E2E（自动启停服务，E2E 用 8001 端口）
```

## 技术栈

FastAPI + WebSocket + pydantic-settings（配置缺失必需字段启动即报错）；主 LLM 走 OpenAI 兼容 API（.env 的 MODEL_API_BASE，内网 Qwen3.8-27B）；Jev 经 OpenRouter decisions 端点；Kev 经独立 .venv-kev 的 kev.serve GPU 服务（端口 8009）。

## 目录与关键约定

- `backend/decision_layer/` 决策层四引擎，统一 `DecisionResult`；情绪五级 neutral/positive/urgent/dissatisfied/complaint_risk，**complaint_risk 一律转人工**（引擎解析层 `base.enforce_emotion_contract` 强制 + 路由器保底，双保险缺一不可）
- `backend/orchestrator/` 路由与生成；`backend/session_distiller.py` 会话静默 20 秒提炼对话追加 `data/sdwan-real.md`
- `data/sdwan.md` 现役知识库（**禁止改动**）；`data/sdwan-real.md` 提炼积累物（gitignore，不进知识库）
- `tests/results/` 测试报告产物入库；`logs/`、runs/ 模型权重不入库
- 准确率评估口径：澄清反问不计作答（严格口径）；预期关键词客观取自知识库
- 任务书/验收报告命名：`docs/yyyymmdd-phaseN-*-taskbook.md`、`docs/yyyymmdd-phaseN-*-acceptance-report.md`

## 测试基线与硬约束

- 基线命令：`pytest tests -q`（详见 tests/README.md 覆盖对照表）；E2E 自动选空闲端口，固定用 `E2E_PORT`
- 不许跳过/放松已有测试凑绿；不许在测试里调真 Qwen（用 FakeLLM 替身）
- pytest.ini 限定收集 `test_*.py`——batch_test.py 等脚本式验收工具被 pytest 误收集会以真实 .env 实例化 settings 污染测试配置，勿删该限制

## 当前状态与下一步（2026-10-10）

- Phase 1-5 全部交付并复验通过（最新：`aa45db4` 整改 + `3574dac` 复验报告）；PROGRESS.md 是完整过程台账
- 下一步候选：真实案例扩容标注集/Few-shot、中文校准决策模型（Kev 英文原型 noul 对中文无区分度，已按实测校准阈值）、知识库热更新（sdwan-real.md 人工筛选回灌）、微信/飞书对接
