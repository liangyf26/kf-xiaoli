# Phase 6 微信真实数据验收报告

## 1. 验收结论

**最终判定：不通过（核心交付部分完成，但未满足任务书完成条件）。**

`fb479d3` 已交付提取脚本、脱敏产物、标注集、对比脚本和 Phase 6 单元测试，自动化测试数量达到要求；但提交的数据产物仍含未脱敏的真实昵称/别名，违反任务书“不得提交真实微信号、昵称或手机号”的数据安全红线。对比报告默认只包含 `rule/kev/qwen`，没有四引擎结果；报告统计还存在表格转义和重复问题覆盖等有效性缺口；当前工作区也不是干净状态。因此不能按“已完成并通过”验收。

本报告只记录验收证据，不修复实现、不删除用户文件、不修改 `data/sdwan.md`。

## 2. 验收对象与环境

- 验收提交：`fb479d3fbfdf43d8f25b9beefa9ebc7789a9afc1`
- 父提交：`c48c13f19054a19994e0e9d304c17397b67be003`
- 验收时 `HEAD`：`fb479d3fbfdf43d8f25b9beefa9ebc7789a9afc1`
- 验收时 `origin/master`：`fb479d3fbfdf43d8f25b9beefa9ebc7789a9afc1`
- Python：项目 `.venv/Scripts/python.exe`
- 任务书：[docs/20261010-phase6-wechat-real-data-taskbook.md](20261010-phase6-wechat-real-data-taskbook.md)
- 工作区状态：存在未跟踪文件 `data/sdwan-new.md`；该文件未由本次验收删除或覆盖。

## 3. 自动化测试

### 3.1 Phase 6 专项

命令：

```text
.venv/Scripts/python.exe -m pytest tests/test_phase6_units.py -q
```

结果：`14 passed in 0.07s`。

覆盖内容包括 SQL 转义解析、文本/群前缀提取、XML 和 `direction=NULL` 丢弃、噪声、60 秒去重、30 分钟会话切分、业务关键词、旧回复挂接和常见 PII 正则脱敏。

### 3.2 全量回归

命令：

```text
.venv/Scripts/python.exe -m pytest tests -q
```

结果：`97 passed, 1 skipped in 69.06s`，退出码 0。

该结果满足任务书“至少 88 passed、skipped ≤ 1、0 failed”的数量要求。Phase 6 新增 14 项，测试总数由 Phase 5 的 84 项增加到 98 项；本次实际通过 97 项、跳过 1 项。

### 3.3 差异检查

命令：

```text
git diff --check fb479d3^ fb479d3
```

结果：无输出，提交差异无空白错误。

## 4. 产物硬指标

离线核对提交产物：

| 项目 | 结果 | 判定 |
|---|---:|---|
| `sessions.json` 会话数 | 461 | 达标（≥40） |
| 客户消息数 | 1784 | 记录 |
| 有旧机器人回复的问题数 | 54 | 记录 |
| `labeled.json` 条数 | 100 | 达标（恰好100） |
| `labeled.json` 人工复核条数 | 0 | 仅为 Qwen 预标，不能作为人工准确率基准 |
| 会话切分/去重参数 | 30分钟/60秒 | 脚本参数存在，达标 |

`data/wx_real/stats.md` 记录的漏斗为：

```text
INSERT 26232 → 文本消息 23718 → 去噪 13534 → 去重 8514 → 原始会话 3953 → 业务会话 461
```

`PROGRESS.md` 对同一漏斗写成 `26232 → 23719 → 13535 → 8507 → 4010 → 461`，中间数字不一致，无法确认哪一次提取是最终复跑口径。

## 5. 数据安全验收

### 5.1 任务书指定 Git 扫描

命令：

```text
git ls-files data/wx_real | xargs grep -lE "wxid_|1[3-9][0-9]{9}"
git ls-files | grep -E "\.sql$|mapping"
```

两条命令均无输出；两个原始 SQL 和 `mapping.json` 未被 Git 跟踪，形式上的 wxid/手机号/SQL/mapping 检查通过。

### 5.2 语义脱敏检查失败

对已跟踪文件执行额外的真实身份残留复核，发现以下文件存在未脱敏的昵称/别名文本：

- [data/wx_real/sessions.json](../data/wx_real/sessions.json):1730、1774、1934、1960、3218
- [data/wx_real/sessions.txt](../data/wx_real/sessions.txt):205、210、228、231、373

这些文本来自非标准群消息前缀（如 `昵称|wxid|时间|正文`）和状态类消息。当前实现只解析数字序号群前缀，解析失败后将原文作为客户文本；`Desensitizer.text()` 只替换已注册身份，不能覆盖这些未被解析/注册的昵称。专项单测只检查 `wxid_` 和手机号，没有覆盖昵称残留。

因此任务书第 25 行“不得提交任何含真实微信号、昵称、手机号的文件”不满足，数据安全红线失败。报告不重复列出原始身份内容，避免扩大泄露。

### 5.3 知识库完整性

`data/sdwan.md` 当前 SHA-256：

```text
3757e194100527d10d93581d7157595b2c0d9825d106853748658e26ce36b0e1
```

与 `fb479d3^` 中的 SHA-256 一致，现役知识库未被本提交修改。

## 6. 提取与参考回复缺口

静态审查发现以下会影响真实数据完整性的问题：

1. `scripts/extract_wx_sessions.py:131-132` 对所有 `direction=NULL` 直接丢弃，但原始数据中存在 `msg_id` 以 `bot_` 开头的正常旧机器人回复，可能造成参考回复丢失。
2. `scripts/extract_wx_sessions.py:140-155` 识别群出站消息时主要检查 `sender_id`，而真实出站群目标位于 `to_wxid`，会使机器人/人工群回复无法与客户群会话归并。
3. `scripts/extract_wx_sessions.py:141-147` 只支持数字序号群前缀，非标准前缀会把昵称和状态正文保留下来，既造成泄露，也污染业务会话。
4. `NOISE_PATTERNS` 主要匹配文本开头；含“掉线”但属于系统状态汇报的变体进入了业务会话和评测报告。已提交报告第 137-146 行出现被拆坏的状态文本样例。

这些问题不是仅凭测试数量可以抵消的；现有 14 项单测未覆盖 NULL bot 回复、群出站 `to_wxid`、非标准群前缀、昵称泄露和状态噪声。

## 7. 四引擎评测与报告有效性

现有报告：[tests/results/real_compare_20261010-130459.md](../tests/results/real_compare_20261010-130459.md)

报告包含：

- 引擎对比总表
- 意图/情绪分布
- P50/P95 延迟、澄清比例、无法回答比例
- 情绪专项
- 新旧回复并列和重复回答比例

但存在以下阻断性证据缺口：

1. 报告实际只运行 `rule`、`kev`、`qwen`，第 4-5 行明确写明 Jev 未参与；`--include-jev` 仅为可选参数，没有已提交的四引擎结果。任务书正文要求检验四个引擎，因此只能认定为三引擎条件评测，不能宣称四引擎已完成。
2. 报告“情绪专项命中率”以负面关键词作为正例代理，没有人工真值。`labeled.json` 的 100 条记录全部 `reviewed:false`，且 `expected_keywords`/`expected_sources` 为空，不能解释为意图或情绪准确率。
3. 报告第 137-146 行的问题文本含 `|`，未转义导致 Markdown 表格列被拆开；报告的清单不可可靠阅读。
4. `tests/compare_real_sessions.py:247` 使用 `(session_id, question)` 聚合结果，同一会话重复问题会互相覆盖，而总问题数仍按全部结果计算。已提交报告第 173-174 行出现同一会话同题重复，导致不一致数与分母口径不一致。
5. `tests/compare_real_sessions.py:201` 将 `None` 视为已识别，而分布表使用 `or "unclear"`，意图指标之间存在归一化不一致。
6. 当前报告写“前30会话/150条问题”，`PROGRESS.md:501` 写“30会话/117问”，提交文档之间存在事实口径冲突。

报告中的 Kev/Qwen 运行结果还依赖本机 Kev 服务和 Qwen API 配置；本次验收没有将真实微信消息发送给 Jev，也没有把未运行的 Jev 结果伪装为通过。

## 8. 通过项与未通过项

### 已通过

- Phase 6 专项单测 14/14 通过。
- 全量 pytest 97 passed、1 skipped、0 failed。
- 会话数 461、标注集 100 条，满足数量硬指标。
- 脱敏 SQL 行解析、PII 正则和 Git 跟踪范围的形式检查通过。
- `data/sdwan.md` 未被修改。
- 报告具备任务书要求的主要章节，且记录了三引擎实际评测结果和新旧回复统计。

### 未通过/阻断

- 已跟踪的 `sessions.json` 和 `sessions.txt` 含真实昵称/别名，违反数据安全红线。
- 提交工作区存在未跟踪文件 `data/sdwan-new.md`，不满足当前验收时的 `git status` 干净条件。
- 未提供 Jev 参与的四引擎结果，真实数据四引擎验收证据不完整。
- 对比报告存在 Markdown 破坏、重复问题覆盖、None/unclear 口径和数据污染问题，统计结果不能作为严谨准确率结论。
- 预标集尚未人工复核，不能作为准确率基准。

## 9. 整改建议

1. 扩展群前缀解析，统一处理数字序号和“昵称|wxid|时间|正文”变体；在写出任何产物前对解析得到的昵称、微信号和群标识做统一编号。
2. 对 `direction=NULL` 按 `msg_id`/业务类型区分旧 bot 回复与非业务遗留行；群出站消息同时根据 `to_wxid` 识别目标群并归并到正确会话。
3. 增加状态消息和非标准状态格式的噪声规则，并补充昵称泄露、群出站归并、NULL bot、重复问题和报告 Markdown 转义单测。
4. 修复对比脚本按消息序号对齐结果，统一 `None`/`unclear` 归一化，并对 Markdown 单元格转义。
5. 在数据安全整改后重新生成全部脱敏产物，重新核对漏斗数字；如运行 Jev，只能使用脱敏文本并在报告中明确外部数据出境范围和实际结果。
6. 处理或明确 `data/sdwan-new.md` 的归属后，再以干净工作区重新验收；本次验收未替用户删除该文件。

## 10. 验收证据

机器可读证据见 [docs/acceptance-evidence/20261010-phase6-acceptance.json](acceptance-evidence/20261010-phase6-acceptance.json)。

验收报告生成时没有提交原始 SQL、`mapping.json`、`.env`、日志、缓存、模型权重或 `data/sdwan-real.md`。
