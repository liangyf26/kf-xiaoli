# kf-xiaoli 知识库热更新验证（任务书 v1.2，2026-10-23）

执行：Coder · 环境：隔离实例 `127.0.0.1:8011`（未动 8000 生产实例与 `.env`）
实现改动：`backend/config.py`（ADMIN_TOKEN）、`backend/knowledge.py`（清洗 + reload/preview/stats）、`backend/main.py`（实例归一 + 端点）、`scripts/kb_reload.py`（CLI）、`tests/test_knowledge_hotswap.py`（13 条单测）

## 0. 结论

**8 条验收中除压测（第 3 条）与质量 A/B（任务书已划给 Reviewer）外全部实测通过；切库全程零 LLM 调用（LLM 端点全程不可达仍成功）。** 未 commit，等 Chief 提交；`.env` 未动，正式切换由 Chief 执行。

## 1. 单实例归一（验收前置）

```
$ .venv/bin/python -c "from backend import main; print(main.orchestrator.knowledge_base is main.knowledge_base)"
same object: True
init kb pairs: 58
```

`main.py:137` 改为 `orchestrator = Orchestrator(knowledge_base=knowledge_base)`；reload 换这一个实例，回答路径（orchestrator/router/handlers）即时生效。

## 2. kb-stats 三次实测（正向 1/2/4 + 验收 8 完整 JSON）

**初始（58 条，来源 sdwan.md）：**
```json
{"current":{"source":"sdwan.md","source_path":"/home/lyf/project/kf-xiaoli/data/sdwan.md","qa_pairs":58,
"categories":{"general":2,"price":2,"product":4,"purchase":3,"technical":8,"troubleshooting":17,"usage":22}},"previous":null}
```

**切到 sdwan-new.md 后（69 条，CLI 输出，分类与任务书 §输入逐项一致）：**
```
切换成功 · 新库: /home/lyf/project/kf-xiaoli/data/sdwan-new.md（sdwan-new.md）
  条数: 69
  分类分布: general 1、price 8、product 5、purchase 4、technical 13、troubleshooting 16、usage 22
上一个库: /home/lyf/project/kf-xiaoli/data/sdwan.md（sdwan.md）  条数: 58
  与上一个库的条数差: +11
```

**回滚 sdwan.md 后：58 条**（分布同初始；delta -11；`previous` 记录了 sdwan-new.md 69 条）。

日志证据（logs/app.log）：`19:04:07 知识库热更新: …/sdwan-new.md，共69个问答对` → `19:04:07 知识库热更新: …/sdwan.md，共58个问答对`。

## 3. 反向验证（5/6/7）

- **坏库拒换（验收 5）**：空文件 POST → HTTP 400 `知识库未替换：知识库文件为空: /tmp/kf-sdwan-bad.md`，CLI 退出码 **1**；随后 kb-stats 仍是 `sdwan.md 58`。
- **鉴权 fail-closed（验收 6）**：无 token → **401**；错误 token → **401**（服务已配置 token 时错 token 也 401；`.env` 未配 ADMIN_TOKEN 时连正确头也 401——fail-closed，无 localhost 豁免）。
- **旧库独有题（验收 7）**：切新库后，旧 Q56「sdwan的软件客户端下载不了。」答案片段（「需要注册美区（外国）apple ID苹果账号。」）与旧 Q46「iphone直播有点卡怎么办？」答案片段（「一是确保iphone是13标准版以上…」）**在新库 69 条全文中不存在**；按原问题对新库检索，命中 6/7 条新库内容，**均不含旧答案**——旧答案不可达，查询走新库/兜底。

## 4. 零 LLM 证明（用户要求：换库不依赖模型）

1. **实跑**：隔离实例以 `MODEL_API_BASE=http://127.0.0.1:9`（不可达地址）启动，期间完成 dry-run → 58→69→58 全部成功——换库路径未碰任何模型端点。
2. **结构**：单测 `test_reload_is_zero_network_by_imports` 断言 `backend.knowledge` 不 import urllib/requests/httpx/http/socket；`reload()` 只做 读文件→解析→校验→换引用→返回统计。
3. CLI（`scripts/kb_reload.py`）只打本地管理 HTTP，无模型调用。

## 5. 单测与反向验证

`pytest` 全量 **136 passed**（原 123 + 新 13：正常 reload、四种拒换、preview 不换、零网络 import、清洗三类、stats 字段、往返一致）。

反向两条（改坏→红→还原，`knowledge.py` sha256 `d91abe78…` 前后一致）：
1. **注释 reload 三行赋值** → `test_reload_swaps_content_and_is_observable` 红：`assert result["source"] == "lib-b.md"` 失败（reload 未生效即被抓住）；
2. **clean_placeholders 改原样返回** → 清洗三用例齐红（media/escalation/direct）。

## 6. 占位清洗口径（Chief 裁定 2026-10-23）

- `{{{发送图片:…}}}` / `{{{发送文件:…}}}`：**整段删除**，本机路径（C:\code\...）绝不进 prompt；
- `{{{人工介入：发通知到管理群}}}`：去壳保留语义 → `（需人工介入：发通知到管理群）`。**真正触发转人工是 Phase 2**，本阶段只保证标记不漏、语义不丢，验证到此为止。

## 7. 边界与移交

- **压测（验收 3）**：50 QPS×30s 未跑（需要压测工具与固定题集，随 Reviewer 验收一起做）；并发安全依据：asyncio 单线程内 reload 三次赋值在同一同步段，无 await 间隙。
- **质量 A/B**：留给 Reviewer 分层题集（Chief 已验 LLM 可达）。
- **内容门禁**：任务书 §边界 5 处措辞（「包稳不卡」等）未改动，切换前需业务确认——不在本次范围。
- **切换动作**：`.env` 的 `KNOWLEDGE_BASE_PATH` 由 Chief 改并提交；reload 不持久化配置（重启回 .env 指向的库）。
- 运维操作步骤见 `docs/kf-知识库更新手册.md`。
