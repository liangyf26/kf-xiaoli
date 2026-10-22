# kf-xiaoli 模型配置改造验收报告

- **审查对象**：`/home/lyf/project/kf-xiaoli` 未提交改动（6 文件，+174/-31）
- **范围**：config.py（DECISION/GEN 两路 max_tokens + 思考开关、JEV_CHANNELS + 每渠道 4 项）、llm/client.py（chat_template_kwargs + thinking<512 守卫）、qwen_engine.py（意图快判走 DECISION_*）、jev_client.py（四渠道链 + 降级 + channel_stats）、decision_layer/__init__.py（工厂无参）、main.py（/metrics 接线）
- **方法**：读 diff + 在 `/tmp` 的可写副本跑全量 pytest（HEAD 与改动各一份，避免沙箱写权限干扰）+ 真机调用 Qwen 决策/生成与 Jev 渠道链（合成问题，不含真实客户数据）
- **日期**：2026-10-22

## 0. 结论

**不通过。** 代码主路径可用、无回归，但有两个阻断项：

1. **配置模板未同步，按 README 新部署会让 jev 变成空渠道链、永久 `jev_failed`**（旧 `JEV_API_KEY` 被新工厂忽略；`MAX_TOKENS` 也静默失效）。
2. **「jev 每渠道 1 条连通」不成立**：`.env` 里 opencode_free / opencode_pay 两个 key 已过期（HTTP 401 `api_key_expired`），4 渠道实际只有 typesafe + openrouter 可用。

另有 3 条 P2、5 条 P3。

## 1. 已验证通过的部分

| 项 | 实测 | 结论 |
|---|---|---|
| 配置值 | `DECISION_MAX_TOKENS=256`、`GEN_MAX_TOKENS=1024`、`DECISION_ENABLE_THINKING=false`、`GEN_ENABLE_THINKING=true` | ✅ |
| 生成客户端 | `QwenClient()` → `max_tokens=1024`、`enable_thinking=True`、payload `chat_template_kwargs={"enable_thinking": true}` | ✅ |
| 决策客户端 | `QwenEngine().llm_client` → `max_tokens=256`、`enable_thinking=False`、payload 对应 false | ✅ |
| 512 守卫 | `QwenClient(enable_thinking=True, max_tokens=256)` → 抬到 512 并打 WARNING | ✅ |
| Qwen 决策实测 | `QwenEngine().decide("直播线路多少钱")` → `price_inquiry`、`engine=qwen`、3385ms（<10s，无回退） | ✅ |
| Qwen 生成实测 | `QwenClient().generate(...)` → 89 字非空、4512ms | ✅ |
| Jev 渠道顺序 | `typesafe → opencode_free → opencode_pay → openrouter`，URL/模型正确 | ✅ |
| Jev 降级 | 首渠道强制 401 后：typesafe fail、opencode_free/pay fail（401 expired）、openrouter ok → 最终 `engine=jev`、`price_inquiry`、721ms | ✅ 链路降级正确 |
| Jev 健康计数 | `/metrics` 的 `jev_channel_counts/fails` 正确输出 0/1 计数 | ✅ |
| 全量回归 | `/tmp` 可写副本：HEAD **107 passed + 1 failed**；改动后同为 **107 passed + 1 failed**，失败都是 `test_config_load`（预先存在的 ENV_FILE 隔离问题） | ✅ 无回归 |
| 范围 | 6 文件；handlers.py/orchestrator.py 已还原，FAQ 改动未混入 | ✅ |

> 注：直接在 `/home/lyf/project/kf-xiaoli` 跑 pytest 会因沙箱禁止写 `logs/`、`.pytest_cache`、`engine_comparison.json` 出现 7 个 PermissionError；在 `/tmp` 可写副本里恢复为 107+1，说明那 6 个是环境问题，不是回归。

## 2. P1（阻断）

### P1-1 `.env.example` / README 未同步，新部署 jev 直接空渠道链

**证据**：按 README 的 `cp .env.example .env` 建配置，用 `ENV_FILE` 指向 `.env.example` 实测：

```
JEV_CHANNELS: typesafe,opencode_free,opencode_pay,openrouter
channels built: []
engine channels: 0
decide: jev_failed unclear | reason: Jev 无可用渠道
Jev 渠道 typesafe 配置不全…（×4，全部跳过）
```

原因：

- `.env.example` 只有旧的 `JEV_API_KEY / JEV_API_BASE / JEV_MODEL`，没有 `JEV_CHANNELS` 和 4×4 渠道变量；
- 新工厂 `create_decision_engine("jev")` 调 `JevEngine()`，只按 `JEV_<CHANNEL>_API_KEY` 组链，**完全忽略旧的 `JEV_API_KEY`**；
- 于是所有渠道因 key 为空被跳过 → `channels=[]` → 每次决策直接回退 `jev_failed`，不会发起任何请求。

同一份模板里 `MAX_TOKENS=512` 也被静默忽略：`QwenClient().max_tokens` 实测为 **1024**（`GEN_MAX_TOKENS` 默认），`.env.example`/README 没有任何迁移说明。

**改法（二选一或都做）**：
1. 更新 `.env.example`（和 README 第 82 行「jev 需 JEV_API_KEY」）为 `JEV_CHANNELS` + 4 组 `JEV_<NAME>_{BASE_URL,ENDPOINT,MODEL,API_KEY}`，并补 `DECISION_*/GEN_*`、注明 `MAX_TOKENS` 已废弃；
2. 在 `_build_channels()` 末尾加向后兼容：若组不出任何渠道且 `settings.JEV_API_KEY` 非空，则构造 legacy OpenRouter 渠道（`JEV_API_BASE + /alpha/decisions`、`JEV_MODEL`），打一条 deprecation 日志。这样老 `.env` 不会静默失去 jev。

### P1-2 两个 OpenCode 渠道 key 已过期，「每渠道连通」不成立

**证据**：真机调 `JevEngine().decide()`（typesafe key 有效时第一次 133ms 成功；把 typesafe 强制成无效 key 后观察降级）：

```
typesafe: fail 1 | HTTP 401 authentication_error
opencode_free: fail 1 | HTTP 401 api_key_expired
opencode_pay: fail 1 | HTTP 401 api_key_expired
openrouter: ok 1  → 最终 engine=jev, price_inquiry, 721ms
```

也就是说 `.env` 里 `JEV_OPENCODE_FREE_API_KEY` / `JEV_OPENCODE_PAY_API_KEY` 两个 key 已过期，链路只是靠 typesafe 或 openrouter 兜底。Coder 交付说明里的「jev 每渠道 1 条连通」与实测不符；如果 typesafe 也挂了，就只剩 openrouter 一条，四渠道冗余名存实亡。

**改法**：重新申请/刷新两个 OpenCode key，或暂时从 `JEV_CHANNELS` 移除它们并在 `.env`/交接说明里注明「当前仅 typesafe + openrouter」；不要在验收里宣称 4 渠道连通。

## 3. P2

1. **新代码没有单测。** `tests/` 里没有任何 `DECISION_MAX_TOKENS / GEN_MAX_TOKENS / chat_template_kwargs / channel_stats / JEV_CHANNELS` 引用；现有 107 条测试没有一条覆盖这次改动。Coder 的「4 条对话」是手工证据，不能防回归。至少补：payload 思考开关、256/1024 路径拆分、512 守卫、渠道顺序/跳过/降级/计数、`/metrics` 字段。
2. **`/metrics` 看不到被跳过的渠道。** `_build_channels()` 因配置不全跳过的渠道不会进入 `_CHANNEL_STATS`，健康面板里直接缺项，运维会误以为「没有这个渠道」而不是「配置缺失/未启用」。建议输出 `configured: false` 或把跳过计为 fail。
3. **`MAX_TOKENS` 静默失效。** `.env` 和 `.env.example` 都还写着 `MAX_TOKENS=512`，实际生成走 `GEN_MAX_TOKENS=1024`；没有迁移说明，用户改 `MAX_TOKENS` 不会生效。至少从 `.env.example` 删除并注明「用 GEN_MAX_TOKENS」。
4. **`tests/compare_engines.py` 仍走旧路径。** 它用 `JevEngine(settings.JEV_API_KEY)`（legacy 单渠道），不覆盖生产工厂的渠道链；对比脚本和线上路径不一致，后续评测会再次绕过新链路。

## 4. P3

1. `POST /metrics/reset` 只重置 `metrics`，不重置 `_CHANNEL_STATS`；实测 reset 后渠道计数仍在（`openrouter: ok 1`）。要么一起重置，要么在文档写明「渠道计数进程内累计、不随 reset」。
2. `get_metrics()` 里调了两次 `channel_stats()`（counts/fails 各一次），两次快照可能不一致；合并成一次读取。
3. `channel_stats()` 有 `last_error` 但 `/metrics` 没暴露；排障时只能看 fail 数。可加 `last_error`（注意截断/脱敏）。
4. `backend/llm/client.py` 顶部模块 docstring 仍写「配置从 settings 读取 …/MAX_TOKENS」；已不准确。
5. `session_distiller` 也用 `QwenClient()`，现在吃 `GEN_ENABLE_THINKING=True`；提炼 JSON 的稳定性没测过，建议至少跑一次会话提炼回归。

## 5. 最小返工单

1. 更新 `.env.example` + README（或加 legacy `JEV_API_KEY` 回退）——P1-1。
2. 刷新/移除两个过期 OpenCode key，并修正「每渠道连通」的表述——P1-2。
3. 补 §3-1 的 5 组单测（可全用 mock，不依赖网络）。
4. `.env.example` 删除 `MAX_TOKENS` 或注明废弃；README 同步 `GEN_MAX_TOKENS`。
5. `/metrics` 对跳过渠道给可见状态；reset 语义二选一写清。

## 6. 评分

- **Coder：3/5。** 代码主路径和降级都实测可用、无回归、范围干净；扣分给两个 P1（配置模板让新部署直接失去 jev、渠道连通声明与实测不符）和新代码零单测。
- **Chief：4/5。** 范围修正（叫停 FAQ 业务改动）到位、交付边界清楚；扣分给验收要求里没有覆盖「新部署从 `.env.example` 起」和「逐渠道真实连通」两项。

## 附：复现命令（可写副本）

```bash
cd /home/lyf/project/kf-xiaoli
mkdir -p /tmp/kf_test && git archive HEAD | tar -x -C /tmp/kf_test
git diff > /tmp/kf_model.patch && (cd /tmp/kf_test && git apply /tmp/kf_model.patch)
cp .env /tmp/kf_test/.env
cd /tmp/kf_test && /home/lyf/project/kf-xiaoli/.venv/bin/python -m pytest -q
# → 107 passed, 1 failed（test_config_load，HEAD 同样失败）

# 新部署模拟（只读 .env.example）
cp .env.example /tmp/env_example.env
ENV_FILE=/tmp/env_example.env /home/lyf/project/kf-xiaoli/.venv/bin/python -c "
from backend.decision_layer.jev_client import JevEngine, _build_channels
print([c.name for c in _build_channels()], len(JevEngine().channels))"
# → [] 0

# 真机决策/生成/渠道链见 §1（合成问题，无真实客户数据）
```

---

# 第二轮复验：模型配置返工（2026-10-22 18:4x）

**结论：通过（P2×2 + P3×4），同意 commit。** 上轮两个 P1 的主路径已闭环；这轮没有新的功能阻断，但 legacy 占位 key 与 OpenCode 过期 key 的交接口径需要写清。

## 已核实通过

| 项 | 实测 |
|---|---|
| 范围 | 9 个 tracked 文件 + 新增 `tests/test_model_config.py`；`handlers.py`/`orchestrator.py` **不在 diff**（`git diff --stat -- backend/orchestrator/` 为空） |
| 全量 | `/tmp` 可写副本 **119 passed + 1 failed**；唯一失败仍是预先存在的 `test_config_load`（HEAD 也失败），无回归 |
| 新增 12 条 | `pytest tests/test_model_config.py` 在可写副本 **12/12**；原仓直接跑因沙箱禁止写 `logs/` 会让 2 条 metrics 用例 PermissionError，不是代码 |
| 反验 1 | 抹掉 legacy 回退分支 → `test_legacy_fallback_when_no_channel_configured` **红**（`[] != ['legacy']`）；恢复后哈希一致 |
| 反验 2 | 抹掉 512 守卫 → `test_thinking_on_low_max_tokens_raised_to_512` **红**（`128 != 512`）；恢复后哈希一致 |
| 从零 8010 | `cp .env.example` + `DECISION_ENGINE=jev` 起服务：`/healthz` ok、`/metrics` 的 4 渠道 `configured:false` 可见；kill 后 8010 无残留 |
| 8000 现状 | 进程 18:39:17 启动，晚于代码 mtime（18:29）；`/metrics` 已含新字段 `jev_channels`，确认是返工后代码 |
| `/metrics/reset` | 渠道 `ok/fail` 归零，`configured` 保留（实测 4 渠道 reset 后仍 `configured:false`）；`get_metrics` 单次快照 |
| 真实知识文件 | `data/sdwan.md` SHA-256 仍为 `3757e194…`；`git status data/` 干净，无 `sdwan-real.md` 改动 |
| distiller | 输出指到 `/tmp` 是正确处理；真实知识文件未被碰；但该回归只有手工证据（P3） |

## 回答你的三个重点

### 1. legacy 回退条件：引擎是「非空」，不是「真实 key」

- `_build_channels()` 的条件是 `if not channels and settings.JEV_API_KEY:`；它**不排除占位符**。
- 用 `.env.example` 实测：`JEV_API_KEY=your-jev-api-key-here`（非空）→ `channels=[('legacy','placeholder')]` → `decide` 真的带假 key 请求 OpenRouter，返回 401，最后 `jev_failed`、不抛异常。
- 「检查真实 key」的逻辑在 `tests/compare_engines.py` 的 skip 判定里（`PLACEHOLDER_KEYS`），**不在引擎**。
- 这与你描述的 8010 验收「4 渠道 configured:false + legacy 401 降级不抛异常」是**一致**的，所以不算阻断；但「真实 key」这句话只对 compare 脚本成立。若想避免每次 jev 决策都带占位 key 发一次 401，二选一：把 `.env.example` 的 `JEV_API_KEY` 留空，或在 legacy 回退条件里排除 `("", "your-jev-api-key-here")` 并补一条占位符用例（P2）。

### 2. 去重键（沿用上轮结论）

`record_unknown` 按 `(code, message)` 去重、内容只有 `code/message/at`，符合 §4.2；None 与有名 Bot 同 signature 合并 1 条是规格内行为。要追查「谁出错」，需要任务书加 `bot_name/session_id/turn`（内容和/或 key），本轮不动。

### 3. 12 条新单测成色

- 不是空测：渠道顺序用例断言了 URL 序列与 `Bearer key-a/key-b`；legacy 用例断言 endpoint 和 key；512 用例断言值 + WARNING。
- 两条反验都真红（见上表）。
- 缺口：没有覆盖「`.env.example` 占位符 legacy」「401/403 ERROR 与普通 WARN 的分流」「legacy 条目的 configured 字段」；建议补 2–3 条（P3）。

## P2 / P3

**P2**
1. 占位符 legacy 会带假 key 请求（见上），建议空模板 key 或排除占位符。
2. 当前 `.env` 里 opencode_free/pay 两个 key 仍过期（401 `api_key_expired`）；代码已保留在链上并单列 ERROR，但「实际可用只有 typesafe + openrouter」这句交接说明不在 README/.env.example/PROGRESS 里，建议落到持久位置。

**P3**
1. `legacy` 渠道被实际调用后，`/metrics` 的 `legacy` 条目没有 `configured` 字段（`_record_channel` 的 setdefault 没带）；与「configured 标记可见」不完全一致。
2. 12 条测试缺上述三个边界（占位符/401 分流/legacy configured）。
3. `tests/compare_engines.py` 先 `_build_channels()` 再 `JevEngine()`，会重复组链并重复打 4 条 WARNING；改用一次实例或复用 channels。
4. `session_distiller` 吃 GEN thinking 只有手工回归，没有测试；建议补一条 mock 或至少记录证据。
5. `.env.example` 里 `JEV_API_KEY=your-jev-api-key-here` 和「回退 legacy」注释并列，容易被误读为「模板自带可用的 legacy key」；建议明确写「占位符，必须替换」。

## 评分与提交

- **Coder：4/5。** 返工覆盖了 4 条单 + 12 条测试，反验能真红，从零部署/8000/8010/知识文件都实测过；扣 1 分给占位符 legacy 与过期 key 交接口径。
- **Chief：4/5。** 验收点（从零部署、反验、reset、distiller）提得准；扣 1 分给「真实 key」表述与「交接写清」没有落到仓库文件。

**同意 commit。** 范围：上轮 9 个 tracked 文件 + 新增 `tests/test_model_config.py`；`handlers.py`/`orchestrator.py` 不在 diff，`docs/审查报告-*`（我的报告）和 `projects/` 不要带进这次 commit。`.env` 未跟踪，含真实 key，不提交。
