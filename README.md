# SDWAN智能客服机器人 Demo

基于大语言模型的智能客服系统，支持多种决策引擎对比测试，专为SDWAN产品咨询场景优化。

## 📋 项目特点

- **3种决策方案对比**：规则引擎、Jev API（OpenRouter）、Kev本地模型（GPU）
- **智能等待汇总**：自动汇总用户连续发送的多条消息
- **澄清式对话**：遇到模糊问题主动澄清，避免盲目猜测
- **多轮上下文理解**：基于对话历史理解短问题和代词指代
- **答案来源标注**：每次回复标注知识库来源，方便验证
- **实时WebSocket通信**：支持倒计时、思考状态等实时推送

## 🚀 快速开始

### 1. 环境要求

- Python 3.10+
- 本地部署的Qwen2.5-27B模型（开发）或9B（生产）
- （可选）GPU用于Kev决策模型加速

### 2. 安装依赖

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 3. 配置

```bash
# 复制配置模板
cp .env.example .env

# 编辑 .env 文件，填入实际配置
# 必填项：
# - MODEL_API_BASE: Qwen模型API地址
# - MODEL_NAME: 模型名称
# - DECISION_ENGINE: 选择决策引擎 (rule/jev/kev)
```

**重要配置项说明**：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `DECISION_ENGINE` | 决策引擎选择 | `rule` |
| `CONTEXT_TURNS` | 保留对话轮数 | `10` |
| `WAIT_SLIDE_SECONDS` | 滑动窗口等待（秒） | `15` |
| `JEV_API_KEY` | Jev API密钥（选择jev时必填） | - |
| `KEV_DEVICE` | Kev运行设备（cpu/cuda:0） | `cpu` |

### 4. 启动服务

```bash
# 启动FastAPI服务
python -m backend.main

# 或使用uvicorn
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. 访问界面

浏览器打开：http://localhost:8000

## 📖 使用说明

### Web界面使用

1. **发送消息**：在输入框输入问题，点击"发送"或按回车
2. **等待汇总**：连续发送多条消息时，系统会等待15秒汇总后统一回复
3. **查看来源**：每条回复下方显示答案来源（如"问题3, 问题12"）
4. **清空对话**：点击"清空对话"按钮重新开始

### 切换决策引擎

编辑`.env`文件，修改`DECISION_ENGINE`参数：

```env
# 规则引擎（最快，60-70%准确率）
DECISION_ENGINE=rule

# Jev API（需要API key，85%+准确率）
DECISION_ENGINE=jev

# Kev本地模型（GPU部署，见下方"Kev模型加载失败"一节）
DECISION_ENGINE=kev
```

修改后**重启服务**生效。

### 批量测试

```bash
# 准备测试文件 tests/test_questions.txt
# 格式：每行一个问题，空行分隔不同会话

# 运行批量测试
python tests/batch_test.py

# 查看结果
cat test_results.json
```

## 🏗️ 项目结构

```
707-kf-xiaoli/
├── docs/                     # 项目文档
│   ├── 产品需求文档-20261008.md
│   ├── 技术设计文档-20261008.md
│   └── 20261008-phase1-Infrastructure-taskbook.md  # Phase 1任务书
├── backend/                    # 后端代码
│   ├── main.py                # FastAPI入口
│   ├── config.py              # 配置管理
│   ├── models.py              # 数据模型
│   ├── connection_manager.py  # WebSocket管理
│   ├── wait_aggregator.py     # 等待汇总层
│   ├── knowledge.py           # 知识库模块
│   ├── decision_layer/        # 决策层（3种引擎）
│   ├── orchestrator/          # 路由和编排
│   └── llm/                   # LLM客户端
├── static/                    # 前端静态文件
│   ├── index.html
│   ├── app.js
│   └── style.css
├── data/
│   └── sdwan.md              # 知识库（59个问答）
├── tests/                    # 测试脚本
├── logs/                     # 日志目录
├── .env                      # 配置文件（不提交）
├── .env.example              # 配置模板
└── requirements.txt          # 依赖列表
```

## 🧪 测试

### 运行验收测试

```bash
# 全量验收（单元 + 端到端，端到端自动启停服务）
python tests/run_all.py

# 仅单元验收（也兼容pytest）
python tests/test_phase1_units.py
pytest tests/test_phase1_units.py

# 仅端到端验收（自动在8001端口启停服务，E2E_PORT可改端口）
python tests/e2e_test.py
```

测试内容与任务书验收命令的对应关系见 `tests/README.md`。

### 测试用例说明

| 测试场景 | 输入示例 | 预期行为 |
|---------|---------|---------|
| 首次对话 | "你好" | 包含机器人身份告知 |
| 短问题+上下文 | "直播线路"→"多少钱" | 无需澄清，直接回答价格 |
| 短问题无上下文 | "多少钱" | 澄清：提供选项让用户选择 |
| 知识库外问题 | "支持YouTube吗" | 返回"暂时无法回答，需要人工介入" |
| 连续发送 | "多少钱"→(3秒)→"直播的" | 等待15秒后汇总回复 |

## 📊 性能指标

### 响应时间（目标）

| 组件 | 响应时间 |
|------|---------|
| 规则引擎 | <1ms |
| Jev API | 1-2秒（OpenRouter） |
| Kev本地模型（GPU） | 约1秒（RTX 2050实测；无CUDA graphs约束下官方数据百毫秒级） |
| LLM生成 | <5秒 |

### 准确率（目标）

| 阶段 | 意图识别准确率 | 答案准确率 |
|------|---------------|-----------|
| MVP | >70% | >90% |
| 生产 | >85% | >95% |

## 🔧 常见问题

### 1. 启动报错：知识库文件不存在

**原因**：`data/sdwan.md`文件缺失或路径配置错误

**解决**：
- 检查`data/sdwan.md`是否存在
- 检查`.env`中`KNOWLEDGE_BASE_PATH`配置

### 2. Qwen模型连接失败

**原因**：模型未启动或API地址错误

**解决**：
```bash
# 确认Qwen模型已启动（以Ollama为例）
ollama run qwen2.5:27b

# 测试API
curl http://localhost:11434/v1/models
```

### 3. Kev模型加载失败

Kev经本地服务运行（真实模型 jaredpalmer/kev-0.5b / kev-0.8b，非 tthous）：

```bash
# 一次性环境准备（Python 3.12+，详见 PROGRESS.md 部署说明）
py -3.13 -m venv .venv-kev
.venv-kev\Scripts\python.exe -m pip install torch "kev[serve] @ git+https://github.com/jaredpalmer/kev"

# 启动GPU服务（自动预检显存；4GB卡建议 0.8b，Kev-4B需≥10GB显存）
.venv-kev\Scripts\python.exe scripts\kev_gpu_serve.py --model 0.8b --port 8009 --warmup
```

- `.env` 中 `KEV_SERVE_URL=http://127.0.0.1:8009`（已默认配置），KevEngine自动对接
- HuggingFace 直连不可用时脚本自动走 hf-mirror 镜像；模型缓存位于 `HF_HOME` 指向目录
- 服务未启动时 Kev 决策自动降级为低置信度结果（不阻塞对话）

### 4. WebSocket连接断开

**原因**：网络不稳定或服务器重启

**解决**：刷新页面，前端会自动重连

## 📝 开发计划

### 已完成
- [x] PRD和TDD文档
- [x] 项目结构搭建
- [x] 配置文件和README
- [x] Phase 1：FastAPI + WebSocket基础框架、知识库、Web界面
- [x] Phase 2：决策层（规则引擎/Jev/Kev）+ 工厂切换 + 对比测试

### 进行中（Phase 3）

任务书详见 `docs/20261008-phase3-taskbook.md`（待编写）。

- [ ] LLM客户端（Qwen27B集成）
- [ ] Prompt构建器和Few-shot示例
- [ ] 路由和编排逻辑（澄清/FAQ/LLM生成/转人工）
- [ ] 真实回复生成（替换Echo mock）

### 待开始
- [ ] 批量测试脚本
- [ ] 准确率评估

详见《技术设计文档-20261008.md》（`docs/技术设计文档-20261008.md`）第十章实施路线图。

## 🤝 贡献

本项目为内部Demo项目，暂不接受外部贡献。

## 📄 许可

内部项目，保留所有权利。

## 📮 联系

如有问题，请联系项目负责人。

---

**注意事项**：
1. `.env`文件包含敏感信息，已添加到`.gitignore`，不会提交到Git
2. 知识库`data/sdwan.md`包含产品信息，注意保密
3. 生产环境部署前请充分测试
4. Jev API会将数据发送到国外，使用前评估数据安全风险
