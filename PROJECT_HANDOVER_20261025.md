# kf-xiaoli 阶段性工作说明（2026-10-25）

> 用途：团队接手期间的进度快照 + 欠账清单 + 下一步优先级。数据均为 2026-10-25 14:5x 实测，命令见 §7。
> 配套文档在 turtleschool 仓库：`docs/PRD-kf攻防与竞品学习.md`（v3.1，378 行）、`docs/TDD-kf攻防与竞品学习.md`（v2.6）、`docs/任务书-057-蓝队风格池.md`。

## 1. 当前状态（一句话：问答与对抗框架都能跑，数据与运维还没收口）

| 项目 | 现状 | 证据 |
|---|---|---|
| 客服服务 | 在跑，`/healthz` 200 | pid 1588531，`{"app":"sdwan-kf-xiaoli","status":"ok","qa_pairs":69}` |
| 知识库 | 生产指向新库 69 条 | `.env` `KNOWLEDGE_BASE_PATH=./data/sdwan-new.md`；旧库快照 `data/sdwan-20261024.md` |
| 单元/集成测试 | 全绿 | `212 passed`（2026-10-25 复跑） |
| 对抗校准 | 两道门禁都过 | `G0 PASS`、`G1 PASS` |
| 内网模型 API | 在线（此前阻塞已消失） | `172.28.50.9:30030`、`172.28.29.24:30031` 均返回 401（需 key，服务在） |
| kev 引擎 | 停用 | 内存不足，默认引擎 = rule；恢复条件见 §5 |
| 服务常驻 | **未做** | nohup 启动（2026-10-24 10:38），无 systemd 单元，重启不自启 |

## 2. 接手后做了什么

按提交顺序（`git log` 可查）：

| 提交 | 内容 |
|---|---|
| `99b2c38` | 知识库热更新 + 检索层 query 优先（含跨章节同号修复） |
| `250c977` | 红蓝对抗最小闭环：D1–D4 场景、判定器、隔离实例、D4 客户可读化、隔离门禁三判 |
| `3b8442f` | D1 口径改为「记录项」（用户 2026-10-24 拍板：三处绝对表述业务确认保留原样） |
| `3c82b75` | D1 双向校准 + 门禁小修（Reviewer 遗留 P2/P3 五条） |
| `bc6fce7` | 切库到 `sdwan-new.md`（69 条）+ 旧库快照 `sdwan-20261024.md` |
| `11f94de` | 蓝队风格池：5 档风格卡 + 采样器 + 质检门（verifier）+ 「意图 × 风格」失分率矩阵 + 单测 16 项 |
| `c3f4286` | 切库连带修复：phase4 两条 sources 断言改为按当前库解析，不再写死题号 |

文档侧（turtleschool 仓库）：

- PRD 升到 v3.1：单轨 + 范围制（蓝队知范围不知库）、范围外题三段式、难度不降权、重复只记录、激励按检索结论收敛（见 `a436a98`、`e0fffb3`、`5c786b2`）。
- TDD 到 v2.6：D3 拆 D3a/D3b、G1 的 D1 分支修正、§5.6 切库分级验证标准（草案）。
- 两份待审清单：`docs/kf-知识库风险条目待审清单.md`（10 条）、`docs/竞品话术待审清单.md`（44 行，实质空表）。
- 调研：`docs/agent激励与对抗自进化-调研.md`、`docs/agent激励与惩罚-补充检索.md`（5 条一手来源，逐条 fetch 核实）。
- 审查报告：`docs/审查报告-057-蓝队风格池.md`（含三轮结论）。

对抗校准的关键结论：判官自校准 14/14；真实 WS 26/26 分母、0 error/timeout、泄漏 0、分支 `{not_faq:21, query_first:4, fallback:1}`；16 格矩阵与 `calibration.py` 期望值全部对上。

## 3. 还差哪些工作

### 3.1 数据与基线（阻塞下游）

1. **`data/wx_real/labeled.json` 没有任何标注**。100 条 case 的 `reviewed` 全为 `false`、`expected_keywords` 与 `expected_sources` 全空、`should_answer` 全为 `true`。此前记录「97 条待补 5 条」与实际不符——现状是 100 条都还没标。补标脚本 `scripts/prelabel_labeled.py` 在库，可跑。
2. **四引擎正式基线报告未重跑**。`tests/compare_real_sessions.py --sessions 30 --include-jev` 上次因内网 API 不可达中断，现 API 已通；`tests/results/` 里最新仍是 10-23 的 `real_compare_20261023-154646`。

### 3.2 对抗赛制

3. **收敛阈值未定**：PRD `:209`/`:238` 仍是「连续 N 局蓝方得分率 < 阈值（N=5、20% 待用户确认）」。
4. **第一局演习未开**：样式池、判定器都就位，但还没有在真实链路上跑过一整局（现有验收全部是 dry-run + 假应答）。
5. **风格池 3 项 P2**（Reviewer 第三审判定不阻断）：
   - `resolve_expect_hit` 同词命中多条时把多个 label 都塞进 `expect_hit`，判据偏宽（当前 D3 在新库只命中 1 条，不影响本轮）；
   - 矩阵里「真实 + 模拟」混格时，任一 simulated 行会把整格跳过，会丢真实行；
   - `test_window_zero` 只覆盖 `_agg_by_run`，没覆盖 `style_matrix.compute(window=0)`。
6. **安全题组未建**：越权/索要内部数据/竞品机密这类题按 PRD 应单独成组、单出安全报告，目前只有设计文字没有实现。

### 3.3 运维

7. **8000 未做常驻**：现在是 nohup，机器重启即失联。建议做 systemd 单元 + 开机自启。
8. **`ADMIN_TOKEN` 未设**：热更新接口 fail-closed，`/admin/kb/stats` 返回 401，只能靠 `/healthz` 判断存活。
9. **kev 引擎停用**：内存不足（物理 97% 占用）时预热即崩，恢复需先释放内存再 `.venv-kev\Scripts\python.exe scripts\kev_gpu_serve.py --model 0.8b --port 8009 --warmup`（预检含提交内存检查）。

### 3.4 文档与口径

10. **风险清单 10 条待业务审**（`:25` 包稳不卡、`:29` 高合规无风险、`:51` 不会封号降权、`:286` 12 小时一定满意、`:283` 1% 增值税普通专用发票 等）。
11. **发票票种未定**：`:283` 现写「1% 增值税普通专用发票」自相矛盾，需业务确认是普票还是专票。
12. **竞品话术清单是空表**：44 行只有格式示例，一条真实取证都没填。
13. **§5.6 分级验证标准仍是草案**，切库按改动大小分档（高/中/低）的阈值未经用户确认。
14. **4 个本地未入库文件待决定**：`tests/results/real_compare_20261022-*`（2 组 4 个文件）、`docs/kf-攻防与竞品-头脑风暴汇总-20261023.md`。

## 4. 下一步按优先级排序

### P0 — 不做会卡住后面所有事

| # | 事项 | 建议执行 | 验收标准 |
|---|---|---|---|
| P0-1 | labeled 补标 100 条 | Coder 跑 `scripts/prelabel_labeled.py`，Writer/业务抽检 | 100 条 `reviewed=true`；`expected_sources` 非空 ≥90%；人工抽 20 条一致率 ≥85% |
| P0-2 | 四引擎正式基线报告 | Coder 跑 `tests/compare_real_sessions.py --sessions 30 --include-jev`，Reviewer 复核 | 报告含逐题落盘、分母可对账（有效题数剔除回退）、四引擎同口径 |
| P0-3 | 8000 常驻 + `ADMIN_TOKEN` | 运维/Chief | 重启机器后 `/healthz` 自动 200；`/admin/kb/stats` 带 token 返回 200，无 token 401 |

### P1 — 红蓝对抗上线前必须完成

| # | 事项 | 建议执行 | 验收标准 |
|---|---|---|---|
| P1-1 | 第一局小规模演习（25 题 + 变异，用隔离实例） | Coder 跑、Chief 组织、Reviewer 看图/看数 | 出首份对局报告：题量、过检率、失分分布、风格矩阵，全部可复算 |
| P1-2 | 拍板收敛阈值（N 与蓝方得分率） | 用户定，Architect 写进 PRD | PRD `:209`/`:238` 从「待用户确认」改为定值 |
| P1-3 | 风格池 3 项 P2 收口 | Coder 改、Reviewer 复审 | 多命中有唯一性规则；真实/模拟分行统计；`style_matrix` window=0 有测试 |
| P1-4 | 安全题组设计与实现 | Architect 出任务书、Coder 实现 | 越权类题单独成组、单出安全报告、不进积分赛 |

### P2 — 收尾与业务确认

| # | 事项 | 建议执行 | 验收标准 |
|---|---|---|---|
| P2-1 | 风险清单 10 条业务审核 + 发票票种 | 业务/用户 | 每条给「保留 / 改写 / 删除」结论，`:283` 票种定死 |
| P2-2 | 竞品话术清单填实项 | Writer 检索、业务确认 | 至少一条带 URL + 时间 + 原文的可用记录 |
| P2-3 | §5.6 分级验证标准定稿 | Architect + 用户 | 高/中/低三档阈值写死并进 TDD |
| P2-4 | kev 引擎恢复（有显存/内存时） | 运维 | 预热成功、GPU <500ms 指标恢复 |
| P2-5 | 4 个未入库文件定性 | Chief | 该入库的提交、快照类按 `tests/results` 约定处理 |

## 5. 当前阻塞与风险

- **补标是人的活**：`prelabel_labeled.py` 只能出机器初标，`expected_sources` 必须在当前库口径下人工核，否则基线报告的分母不可信。
- **dry-run 与真机差异未验证**：风格池与判定器的全部验收都是 dry-run + 假应答；真机上模型会不会按风格出题、判官会不会被长上下文带偏，都还没测过。
- **旧库/新库标签不一致的余波**：新库来源标签是「章节 / 问题N」，旧库是「问题N」。凡沿用旧库写死的断言都会红（phase4 已修；其余用例若再出现同类红，先按此口径判断）。
- **服务无守护**：8000 挂了没人拉，看门狗也不管它（只管 Bot）。

## 6. 待用户决定（3 项）

1. 收敛阈值：连续 N 局、蓝方得分率低于多少算收敛（PRD 现留 N=5、20%）。
2. 8000 是否现在就做 systemd 常驻（做的话需要写 unit + 配 `ADMIN_TOKEN`）。
3. 4 个本地未入库文件：`real_compare_20261022` 两套快照与头脑风暴汇总，入库还是按临时件处理。

## 7. 复跑命令

```bash
cd /home/lyf/project/kf-xiaoli

# 全量测试
.venv/bin/python -m pytest -q -p no:cacheprovider        # 期望 212 passed

# 对抗校准两道门禁
.venv/bin/python tests/adversarial/calibration.py --g0    # 期望 G0: PASS
.venv/bin/python tests/adversarial/calibration.py --g1    # 期望 G1: PASS

# 风格池验收（A1 单测）
.venv/bin/python -m pytest tests/adversarial/test_style_pool.py -q

# 服务存活
curl -s http://127.0.0.1:8000/healthz

# 补标（P0-1）
.venv/bin/python scripts/prelabel_labeled.py

# 四引擎正式基线（P0-2）
.venv/bin/python tests/compare_real_sessions.py --sessions 30 --include-jev
```

---

维护约定：本文件是阶段性快照，状态变化后请更新对应小节并注明日期，不要在此堆叠流水账；日常进度仍记在 `PROGRESS.md`。
