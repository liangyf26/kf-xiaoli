# Phase 1 验收测试

对应 `docs/20261008-phase1-Infrastructure-taskbook.md` 的"完成条件"与各任务验收命令，可独立复跑。

## 运行方式（使用项目虚拟环境）

```bash
# 激活虚拟环境
# Windows: .venv\Scripts\activate

# 全量验收（单元 + 端到端，端到端自动启停服务）
python tests/run_all.py

# 仅单元验收（两种方式等价）
python tests/test_phase1_units.py
pytest tests/test_phase1_units.py

# 仅端到端验收（自动在8001端口启停服务，可用环境变量E2E_PORT改端口）
python tests/e2e_test.py
```

## 覆盖范围

| 测试文件 | 对应任务书 | 内容 |
|---------|-----------|------|
| test_phase1_units.py | 任务1（1.2-1.5）、任务2（2.1-2.2） | 配置加载/缺失字段报错、数据模型、连接管理器、等待汇总（滑动窗口/max_seconds封顶/倒计时推送/取消/断开清理）、知识库加载/分类/意图检索/错误处理 |
| e2e_test.py | 任务1.6、任务4.2、暗卷第1-3项 | 单条消息Echo回复、3条连续消息仅1次回复（检查窗口覆盖max_seconds封顶+5秒）、清空对话后会话重置、启动时知识库缺失报错退出、仅缺KNOWLEDGE_BASE_PATH报错退出 |

预期结果：单元验收 13/13 通过，端到端验收 5/5 通过，`run_all.py` 退出码为 0。

浏览器UI人工检查清单（任务3.3）不在自动化范围内，最近一次执行证据见
`docs/acceptance-evidence/20261008-phase1-ui-checklist.md`（含截图）。
