# Phase 1 第三轮验收报告

**验收对象**：`1e1efa8`（第三轮整改）
**验收日期**：2026-10-08
**依据**：`docs/20261008-phase1-infrastructure-taskbook.md`、上一轮报告 `docs/20261008-phase1-acceptance-report.md`
**结论**：上轮提出的六项缺口已整改并有测试或人工记录支撑。本次复跑通过；未发现阻断验收的功能问题。最大等待时间单测对运行调度有一定敏感性，见“残余注意事项”。

## 执行结果

在项目 Python 3.11 虚拟环境中运行：

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe tests/run_all.py` | 通过；单元验收 13/13、端到端验收 5/5 |
| `.venv/Scripts/python.exe -m pytest -q tests/test_phase1_units.py` | 通过；13 passed |
| `git diff --check 3b04f53..1e1efa8` | 通过；无空白错误 |

## 上轮问题复核

1. **等待层边界覆盖：已整改。**新增最大等待封顶、倒计时推送、取消和断开清理用例，见 `tests/test_phase1_units.py`。
2. **启动失败暗卷：已整改。**E2E 分别验证知识库路径不存在时启动报错，以及仅缺少 `KNOWLEDGE_BASE_PATH` 时配置校验失败，见 `tests/e2e_test.py`。
3. **重复回复检查窗口：已整改。**连续消息场景将检查持续到首条消息后的 `MAX_WAIT + 5` 秒，覆盖最大等待封顶并留有缓冲。
4. **浏览器手动验收证据：已补充。**`docs/acceptance-evidence/20261008-phase1-ui-checklist.md` 记录任务书 §3.3 的 6 项检查均通过，并附 3 张截图。本次未重新操作浏览器；该记录及截图作为本提交提供的执行证据。
5. **E2E 误连其他服务：已整改。**测试默认动态选择空闲端口，通过 `/healthz` 校验应用标识，并在等待服务就绪时监测被测子进程是否提前退出。
6. **pytest 环境污染及临时目录清理：已整改。**测试模块保存并恢复原 `ENV_FILE`，通过模块 teardown、`atexit` 和脚本 `finally` 进行幂等清理。

## 残余注意事项

- `test_wait_aggregator_max_seconds_cap` 以约 3 秒定时行为为基准，并将通过窗口限制在 2.7–3.6 秒（`tests/test_phase1_units.py`）。严重系统负载或 CI 调度停顿可能令测试偶发失败；若 CI 环境较慢，建议改用更宽的上界，同时保持“不得早于最大等待、最终只触发一次”的断言。
- E2E 启动正常服务与启动失败场景分别构造 Uvicorn 命令（`tests/e2e_test.py`）。这是轻微重复维护点，不影响本次通过结果。

## 最终判定

第三轮整改通过本次复核：本地全量验收及 pytest 均通过，上轮指出的自动化覆盖、启动失败场景、重复回复检查窗口、E2E 服务身份识别和 pytest 清理问题均已处理；浏览器检查也有清单与截图作为人工验收证据。Phase 1 验收通过，保留上述测试调度敏感性作为维护注意事项。
