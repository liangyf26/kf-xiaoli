# 任务书：第4种引擎（Qwen意图识别）+ 情绪风险 + 结果展示 + 会话提炼

这活为什么干：前三种引擎中文效果都不够好，要试一下直接用已接入的 Qwen3.8-27b 做意图识别；同时要能看出哪些客户不满、可能投诉；真实对话自动整理成问答，后续补充知识库。

## 现状（已实测，不用再查）
- 引擎工厂 `backend/decision_layer/__init__.py`，`SUPPORTED_ENGINES=("rule","jev","kev")`；结果模型 `backend/decision_layer/base.py` 的 `DecisionResult`，`user_emotion` 现有取值 neutral/positive/negative/urgent
- Qwen 客户端 `backend/llm/client.py` 的 `QwenClient.generate(messages)`；Few-shot 示例在 `backend/orchestrator/few_shot_examples.py`
- 网页切换：`static/index.html` 的 `.engine-btn`，`static/app.js` 发 `switch_engine`；`backend/main.py` 处理 `switch_engine` 和 `GET /engine`
- 测试基线：`.venv/Scripts/python.exe -m pytest tests -q` → **66 passed, 1 skipped**

## 任务
0. 跑一遍上面的基线命令，数字对不上就写 `BLOCKED.md` 停下。先读 `PROGRESS.md`，写不超过 10 行的开工说明再动手。
1. **Qwen 引擎**：新建 `backend/decision_layer/qwen_engine.py`，类 `QwenEngine(DecisionEngine)`。用 Few-shot 提示词让 Qwen 只回 JSON，字段和 `DecisionResult` 一致；工厂和 `SUPPORTED_ENGINES` 加上 `"qwen"`。JSON 解析失败或超时（10 秒）返回 intent=unclear、置信度 0、engine="qwen_failed"，不许抛异常。
2. **情绪风险**：`user_emotion` 取值改为 neutral / positive / urgent / dissatisfied（不满）/ complaint_risk（投诉风险）。四个引擎都要能输出这五个值；原来用到 `negative` 的地方（转人工规则等）全部改掉，`grep -rn '"negative"' backend` 结果必须为空。complaint_risk 一律转人工。
3. **网页四引擎切换**：加「Qwen引擎」按钮；当前引擎按钮用不同底色高亮（四个引擎各一种颜色），其余按钮灰色；刷新页面后高亮与 `GET /engine` 一致。
4. **决策结果展示**：每条机器人回复下方显示一行：引擎、决策耗时（毫秒）、意图＋置信度、情绪。dissatisfied 标橙色，complaint_risk 标红色。数据从后端 `response` 消息里带过去。
5. **会话提炼**：机器人回复后 20 秒内同一会话没有新消息即判定会话结束（时长写成配置项 `SESSION_END_SECONDS=20`，进 `.env.example` 和 `backend/config.py`）。结束后用 Qwen 把整段对话提炼成一问一答，按 `data/sdwan.md` 的格式（`编号. 问题` 换行写答案，空行分隔，编号接着文件里已有的最大编号）追加到 `data/sdwan-real.md`，文件不存在就新建。每条答案后加一行 `（来源：会话<id> <时间> 引擎<名>）`。同一会话只提炼一次；新消息到来要取消计时；提炼失败只记日志，不影响聊天。
6. 新增 `tests/test_phase5_units.py`，Qwen 调用一律用假客户端代替（测试不能依赖真模型在线），至少覆盖：工厂能建 qwen；Qwen 返回乱码时降级；complaint_risk 触发转人工；20 秒计时被新消息取消；提炼结果追加格式和编号正确、`data/sdwan.md` 未变。
7. 更新 `README.md`、`docs/api.md`、`PROGRESS.md`，提交并推送 master（中文提交说明）。

## 不许
- 不许改 `data/sdwan.md`，不许把 `sdwan-real.md` 加载进知识库
- 不许删、跳过、放松已有测试；不许用 `|| true` 或 `.skip` 凑绿
- 不许在测试里调用真 Qwen 服务

## 建议（可以有更好的做法，改了在 PROGRESS.md 写原因）
- 提炼和 Qwen 意图识别共用一个 QwenClient
- 计时器复用 `backend/wait_aggregator.py` 的取消思路

## 我替领导拍的板
- 会话提炼固定用 Qwen，不管当前选的是哪个引擎（另外三个引擎只会分类，不会写文字）
- 「不满」和「投诉风险」做成情绪的两个新取值，不另加字段
- 四个按钮的颜色由执行者选，只要能一眼区分
- 测试里 20 秒计时可以通过配置改成 1 秒来跑

## 止损
- 同一个验收连续失败 3 次：写进 `BLOCKED.md`，跳到下一项
- 改完后测试数比基线少或出现新失败：回滚该项，如实写进 PROGRESS.md

## 完成条件
1. `.venv/Scripts/python.exe -m pytest tests -q` → 通过数 ≥ 72（66＋至少 6 个新测试），skipped ≤ 1，0 failed
2. 反向验证：临时把 `SESSION_END_SECONDS` 设为 1，发一句「直播线路多少钱」后等 3 秒，`data/sdwan-real.md` 多出一条带来源行的问答；再发一句不等就继续发，确认不会提前提炼。把两次的文件内容贴进 PROGRESS.md
3. `grep -rn '"negative"' backend` 无输出；`git status` 干净且已推送
