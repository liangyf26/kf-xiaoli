"""微信真实会话四引擎对比评测：同一批脱敏会话跑 rule/kev/qwen（jev默认关），输出Markdown报告。

用法:
    .venv/Scripts/python.exe tests/compare_real_sessions.py [--sessions 30] [--include-jev]
    # --sessions N：取前N个会话（kev本机慢，拍板默认30个对齐队列）；0=全部（慎用，qwen很慢）
    # --include-jev：对脱敏后文本追加Jev引擎（真实消息绝不发Jev——数据出境红线）

输入: data/wx_real/sessions.json（已脱敏）
输出: tests/results/real_compare_<时间戳>.md，含：引擎对比总表（意图/情绪分布、决策耗时
P50/P95、澄清比例、无法回答比例）、意图不一致问题清单、情绪专项命中率、新旧对比与重复回答比例。

运行前置: kev引擎需本地kev.serve已启动（scripts/kev_gpu_serve.py），否则kev列全为kev_failed降级。
"""
import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from backend.orchestrator.orchestrator import Orchestrator  # noqa: E402
from batch_test import run_session  # noqa: E402  复用batch_test的会话运行逻辑
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from extract_wx_sessions import is_non_customer_text  # noqa: E402  非客户判据的唯一来源

RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
SESSIONS_JSON = PROJECT_ROOT / "data" / "wx_real" / "sessions.json"

# 情绪专项关键词（任务6）：含这些词的消息单独统计dissatisfied/complaint_risk命中率
EMOTION_KEYWORDS = ("卡", "慢", "掉线", "断", "退", "垃圾", "骗", "投诉", "搞不定",
                    "不行", "失望", "不满", "举报", "差评", "坑")

INTENT_ORDER = ["price_inquiry", "usage_guide", "technical_support", "troubleshooting",
                "product_comparison", "purchase_process", "unclear"]
INTENT_LABELS = {"price_inquiry": "价格咨询", "usage_guide": "使用指导",
                 "technical_support": "技术支持", "troubleshooting": "故障排查",
                 "product_comparison": "产品对比", "purchase_process": "购买流程",
                 "unclear": "未识别"}
EMOTION_LABELS = {"neutral": "中性", "positive": "积极", "urgent": "紧急",
                  "dissatisfied": "不满", "complaint_risk": "投诉风险"}


def percentile(values: list[int], p: float) -> int:
    """P50/P95：升序后取 round(p*(n-1)) 那个值（nearest-rank，不是 numpy linear）。"""
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[idx]


# 显式排除清单：模式匹配覆盖不了的整段坐席/物业对话，按 (session_id, q_index) 点名。
# q_index 是该会话 speaker=customer 轮的序号（与逐题 JSON 一致），不是 turn 下标。
# 2026-10-22 Chief 裁定：S021 整段是物业/租赁坐席话术，S022 是物业费群发。
EXPLICIT_EXCLUSIONS = {
    ("S021", i): "物业/租赁坐席话术" for i in range(11)
}
EXPLICIT_EXCLUSIONS[("S022", 0)] = "物业费群发通知"


def exclusion_reason(session_id: str, q_index: int, text: str) -> str | None:
    """非客户的唯一出口：模式命中，或在显式清单里。返回理由；客户题返回 None。"""
    listed = EXPLICIT_EXCLUSIONS.get((session_id, q_index))
    if listed:
        return listed
    if is_non_customer_text(text):
        return "命中 is_non_customer_text"
    return None


def customer_questions(session: dict) -> list[str]:
    """一个会话里进入评测的客户题：speaker=customer，且模式与显式清单都没判为非客户。"""
    out = []
    q_index = 0
    for t in session["turns"]:
        if t["speaker"] != "customer":
            continue
        if exclusion_reason(session["session_id"], q_index, t["text"]) is None:
            out.append(t["text"])
        q_index += 1
    return out


def cohort_exclusions(sessions: list[dict]) -> list[dict]:
    """选队列阶段被判为非客户、不进分母的原始记录（sessions.json 不改）。"""
    out = []
    for s in sessions:
        q_index = 0
        for i, t in enumerate(s["turns"]):
            if t["speaker"] != "customer":
                continue
            reason = exclusion_reason(s["session_id"], q_index, t["text"])
            if reason:
                out.append({"session_id": s["session_id"], "q_index": q_index,
                            "turn": i, "reason": reason, "text": t["text"]})
            q_index += 1
    return out


def pct(part: int, total: int) -> str:
    return f"{part / total * 100:.1f}%" if total else "-"


async def run_engine(engine: str, sessions: list[dict]) -> tuple[list[dict], str]:
    """对一个引擎跑全部对齐会话；返回 (逐题结果含会话号与题内序号, 引擎类名)。"""
    os.environ["DECISION_ENGINE"] = engine
    orchestrator = Orchestrator()
    engine_class = type(orchestrator.decision_engine).__name__
    results: list[dict] = []
    start = time.monotonic()
    for s in sessions:
        questions = customer_questions(s)
        batch = []
        await run_session(orchestrator, questions, batch)
        for i, r in enumerate(batch):
            r["session_id"] = s["session_id"]
            r["q_index"] = i  # 题内序号：同一会话重复问题靠(session_id, q_index)区分，不靠问题文本
        results.extend(batch)
        print(f"  [{engine}] {s['session_id']} 完成（{len(questions)}问，累计{len(results)}）")
    print(f"[{engine}] 全部完成，墙钟{time.monotonic() - start:.0f}秒")
    return results, engine_class


def md_cell(text: str) -> str:
    """Markdown表格单元格转义：竖线与换行会拆列/断行。"""
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def norm_intent(raw: str | None) -> str:
    """意图归一化：None/空串统一为unclear（全报告单一口径）。"""
    return raw or "unclear"


def dist_table(results: list[dict], field: str, labels: dict, order: list[str] | None = None) -> list[str]:
    """分布Markdown行：| 取值 | 题数 | 占比 |。"""
    counter = Counter(r.get(field) or "(空)" for r in results)
    total = len(results)
    keys = [k for k in (order or []) if k in counter] + \
           [k for k in counter if k not in (order or [])]
    lines = []
    for k in keys:
        lines.append(f"| {labels.get(k, k)} | {counter[k]} | {pct(counter[k], total)} |")
    return lines


def repeat_rate(session_answers: dict[str, list[str]]) -> tuple[int, int]:
    """相邻两次回复内容相同的比例：返回 (相同对数, 总相邻对数)。"""
    same = total = 0
    for answers in session_answers.values():
        for a, b in zip(answers, answers[1:]):
            if a.strip() and b.strip():
                total += 1
                if a.strip() == b.strip():
                    same += 1
    return same, total


def compute_old_repeat(all_sessions: list[dict]) -> tuple[tuple[int, int], dict[str, list[dict]]]:
    """全量会话的旧机器人相邻重复率（纯数据统计，无需跑引擎）。

    返回 ((旧相同, 旧总对), refs_by_sid)——refs_by_sid为带旧回复的问题清单。
    """
    refs_by_sid: dict[str, list[dict]] = {}
    for s in all_sessions:
        for t in s["turns"]:
            if t["speaker"] == "customer" and t.get("bot_reply"):
                refs_by_sid.setdefault(s["session_id"], []).append(
                    {"session_id": s["session_id"], "question": t["text"], "old": t["bot_reply"]})
    same = total = 0
    for refs in refs_by_sid.values():
        for a, b in zip(refs, refs[1:]):
            if a["old"].strip() and b["old"].strip():
                total += 1
                if a["old"].strip() == b["old"].strip():
                    same += 1
    return (same, total), refs_by_sid


async def run_new_answers(refs_by_sid: dict[str, list[dict]], engine: str = "rule") -> dict[str, list[str]]:
    """用当前系统重答所有带旧回复的问题（rule引擎，确定性最高）。"""
    os.environ["DECISION_ENGINE"] = engine
    orchestrator = Orchestrator()
    new_by_sid: dict[str, list[str]] = {}
    for sid, refs in refs_by_sid.items():
        batch: list[dict] = []
        await run_session(orchestrator, [r["question"] for r in refs], batch)
        new_by_sid[sid] = [r["answer"] for r in batch]
        print(f"  [new-old] {sid} 重答{len(refs)}问")
    return new_by_sid


def build_section6(refs_by_sid: dict[str, list[dict]], new_by_sid: dict[str, list[str]],
                   old_stat: tuple[int, int], new_stat: tuple[int, int],
                   anchor_engine: str, max_rows: int = 40) -> str:
    """新旧对比节（任务7）：并排列出问题/旧回复/新回复 + 重复回答比例。"""
    lines: list[str] = []
    lines.append("## 6. 新旧对比：旧机器人回复 vs 当前系统重答")
    lines.append("")
    pairs = []
    for sid, refs in refs_by_sid.items():
        news = new_by_sid.get(sid, [])
        for i, r in enumerate(refs):
            pairs.append((sid, r["question"], r["old"], news[i] if i < len(news) else "（重答缺失）"))
    lines.append(f"全量会话中带旧机器人回复的问题共 **{len(pairs)}** 条"
                 f"（新回复取自 {anchor_engine} 引擎，下表最多展示{max_rows}行）。")
    lines.append("")
    lines.append("| 会话 | 问题 | 旧机器人回复 | 当前系统回复 |")
    lines.append("|---|---|---|---|")
    for sid, q, old, new in pairs[:max_rows]:
        esc = lambda s: md_cell(s[:60])
        lines.append(f"| {sid} | {esc(q)} | {esc(old)} | {esc(new)} |")
    lines.append("")
    lines.append("### 重复回答比例（相邻两次回复内容完全相同）")
    lines.append("")
    lines.append(f"- 旧机器人（全量会话）：{old_stat[0]}/{old_stat[1]} = "
                 f"**{pct(old_stat[0], old_stat[1])}**")
    lines.append(f"- 当前系统（{anchor_engine}，同题重答）：{new_stat[0]}/{new_stat[1]} = "
                 f"**{pct(new_stat[0], new_stat[1])}**")
    lines.append("")
    lines.append("> 口径说明：相邻回复完全相同即计一次“重复”。旧机器人大量使用模板话术；")
    lines.append("> 当前系统对同类问题返回相同知识库原文也会计入，该口径衡量的是“相邻雷同”而非答非所问。")
    lines.append("")
    return "\n".join(lines)


def generate_report(runs: dict[str, list[dict]], engine_classes: dict[str, str],
                    sessions: list[dict], args, anchor: dict,
                    new_old: tuple[tuple[int, int], dict[str, list[dict]], dict[str, list[str]], tuple[int, int]],
                    git_sha: str = "", sdwan_sha: str = "",
                    excluded: list[dict] | None = None,
                    sessions_sha: str = "", source_results: str = "",
                    source_results_sha: str = "") -> str:
    """汇总为Markdown报告。new_old: (old_stat, refs_by_sid, new_by_sid, new_stat)。"""
    excluded = excluded or []
    lines: list[str] = []
    n_engines = len(runs)
    title_n = {1: "一", 2: "两", 3: "三", 4: "四"}.get(n_engines, str(n_engines))
    lines.append(f"# 微信真实会话{title_n}引擎对比报告")
    lines.append("")
    lines.append(f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    newly_offline = [sid for sid in ("S021", "S022")
                     if any(item["session_id"] == sid for item in excluded)]
    already_empty = [sid for sid in ("S020", "S024", "S025", "S026")
                     if sid not in {s["session_id"] for s in sessions}]
    if newly_offline:
        data_note = (f"对齐队列 {len(sessions)} 个会话，不是前 {len(sessions)} 个——"
                     f"本版新下线 {'/'.join(newly_offline)}")
        if already_empty:
            data_note += (f"；{'/'.join(already_empty)} 上轮客户轮已为 0，本版不在队列里")
    else:
        data_note = f"前{len(sessions)}个会话为对齐队列"
    lines.append(f"- **数据**: data/wx_real/sessions.json（已脱敏；{data_note}）")
    lines.append(f"- **引擎**: {', '.join(f'{k}({engine_classes[k]})' for k in runs)}")
    if not args.include_jev:
        lines.append("- **Jev未参与**（数据出境红线，默认关闭；如需评估请用--include-jev，仅限脱敏文本）")
    if getattr(args, "skip_kev", False):
        lines.append("- **kev本次未参与**：当前 Linux 主机无 GPU、无 kev 运行环境（无 .venv-kev、无 nvidia-smi），"
                     "且用户 2026-10-11 已决定停用（内存不足，GPU 环境在原 Windows 机器上）。"
                     "默认命令仍含 kev；本次用 `--skip-kev` 显式跳过。")
    if newly_offline:
        align_note = (f"{title_n}引擎同一批 {len(sessions)} 个会话"
                      f"（本版新下线 {'/'.join(newly_offline)}")
        if already_empty:
            align_note += f"；{'/'.join(already_empty)} 上轮客户轮已为 0，本版不在队列里"
        align_note += "）"
    else:
        align_note = f"{title_n}引擎均取前{len(sessions)}个会话"
    lines.append(f"- **对齐口径**: {align_note}；"
                 "回答由共享生成层产生，各引擎只交换决策层")
    lines.append("")
    total_q = len(next(iter(runs.values())))
    lines.append(f"对齐队列共 **{len(sessions)}** 个会话 / **{total_q}** 条客户问题。")
    if excluded:
        pattern_n = sum(1 for item in excluded if item.get("reason") == "命中 is_non_customer_text")
        listed_n = len(excluded) - pattern_n
        lines.append(f"不进分母的共 **{len(excluded)}** 条：{pattern_n} 条命中 "
                     "`is_non_customer_text`，"
                     f"{listed_n} 条在显式清单 `EXPLICIT_EXCLUSIONS`（按 session_id + q_index）。"
                     "原始记录仍留在 `sessions.json`，未重新提取。")
    fallback_counts = {e: sum(1 for r in runs[e] if str(r.get("engine", "")).endswith("_failed")) for e in runs}
    fb_all = "、".join(f"{e} {fallback_counts[e]}题" for e in runs if fallback_counts[e] > 0)
    lines.append(f"- **结论**: {title_n}引擎只交换决策层，回答由共享生成层产生；意图/澄清/无法回答指标均按"
                 f"有效题（剔除服务回退{('：' + fb_all) if fb_all else '：本轮无回退'}）计算，"
                 "用于同批同口径下的引擎横向对比，不构成跨数据集的准确率结论。")
    if source_results:
        cmd = f"python tests/compare_real_sessions.py --from-results tests/results/{source_results}"
    else:
        cmd = "python tests/compare_real_sessions.py --sessions 30"
    if getattr(args, "include_jev", False):
        cmd += " --include-jev"
    if getattr(args, "skip_kev", False):
        cmd += " --skip-kev"
    kb = sdwan_sha if sdwan_sha else "（未取）"   # P3：打全 64 位 sha256，不截断
    meta = (f"- **复现元数据**: 代码提交 `{git_sha or '（未取）'}`；"
            f"知识库 SHA-256 `{kb}`；"
            f"sessions.json SHA-256 `{sessions_sha or '（未取）'}`；")
    if source_results:
        meta += (f"源结果 `{source_results}` SHA-256 `{source_results_sha}`；")
    meta += (f"对齐会话 {', '.join(s['session_id'] for s in sessions)}；"
             f"LLM端点 `{anchor.get('llm_base', '')}` 模型 `{anchor.get('llm_model', '')}`；"
             f"决策超时 qwen 10s / kev 5s / jev 5s；"
             f"命令 `{cmd}`。")
    lines.append(meta)
    lines.append("- **指标口径**: 澄清比例=最终path为clarification的题占比；"
                 "无法回答=回复文本匹配（含前20字出现人工，口径偏宽）；"
                 "决策耗时=decision_latency_ms（决策层，非端到端）；"
                 "P50/P95=`percentile()` 的 `round(p*(n-1))`（nearest-rank，不是 numpy linear）。")
    lines.append("")
    lines.append("> 队列口径：数据集经过会话重切和重编号，逐会话题数与旧版 200 题全变了"
                 "（例如 S030 旧 4→新 13、S011 旧 2→新 28、S002 旧 7→新 1）。")
    lines.append("> 这是重切，不是多出若干题。跨重切版本没有可加的桥，本版不与旧版 200 对账。")
    lines.append(f"> 本版口径：对齐会话 **{len(sessions)}** 个 / 客户题 **{total_q}** 条。"
                 "被剔的题见下面的排除清单；剔完客户轮为 0 的会话整段下线，不进分母。")
    if excluded:
        lines.append("")
        lines.append("选队列剔除清单（不进分母，原文仍在 sessions.json）：")
        lines.append("")
        lines.append("| 会话 | q_index | 理由 | 文本（前 40 字） |")
        lines.append("|---|---|---|---|")
        for item in excluded:
            lines.append(f"| {item['session_id']} | {item.get('q_index', '')} | "
                         f"{item.get('reason', '')} | {md_cell(item['text'][:40])} |")
        offline = []
        by_sid: dict[str, int] = {}
        for item in excluded:
            by_sid[item["session_id"]] = by_sid.get(item["session_id"], 0) + 1
        # 下线 = 该会话的客户轮全部被剔。这里只列显式清单点名的整段会话。
        for sid, reason in (("S021", "物业/租赁坐席话术"), ("S022", "物业费群发通知")):
            if by_sid.get(sid):
                offline.append(f"{sid}（{by_sid[sid]} 条，{reason}）")
        if offline:
            lines.append("")
            lines.append("整段下线（剔完客户轮为 0，不进对齐会话）：" + "、".join(offline) + "。")
            if any(x.startswith("S021") for x in offline):
                # P3（审查报告-kf基线20261022-v3 §7-4）：写清 S021 的判定依据，防止被当成漏判
                lines.append(
                    "S021 是人工判定整段为坐席语境（物业/租赁）：不是 `is_non_customer_text` "
                    "模式命中，依据是整段对话语境——整段是坐席在替业主查物业费/水电公摊、"
                    "报租赁方案、谈带看与佣金，属于坐席视角的连续工作记录，不是单条文本特征判定。")
                lines.append(
                    "S021 q0/q9/q10 是短句（「您好，我现在看下」「好的」"
                    "「嗯嗯，您看哪里合适就找哪里嘛，主要是划算嘛」），单看文本角色不够硬——"
                    "顾客也可能说类似的话；这三条归坐席的依据是整段语境（该会话从头到尾都是"
                    "坐席在办事），随整段一起下线，不是漏判。")
    lines.append("")

    # 回退分离：*_failed降级题不计入意图/澄清/无法回答指标（否则压低该引擎口径）
    valid = {e: [r for r in runs[e] if not str(r.get("engine", "")).endswith("_failed")] for e in runs}
    fallback_counts = {e: len(runs[e]) - len(valid[e]) for e in runs}

    # 1. 引擎对比总表
    lines.append("## 1. 引擎对比总表")
    lines.append("")
    lines.append("| 指标 | " + " | ".join(runs) + " |")
    lines.append("|---|" + "---|" * len(runs))
    rows = ["| 题数（含回退） | " + " | ".join(str(len(runs[e])) for e in runs) + " |",
            "| 有效题数（剔除回退） | " + " | ".join(str(len(valid[e])) for e in runs) + " |"]
    for name, label in (("intent", "意图≠未识别（有效题内）"), ("need_clarification", "澄清比例（有效题内）"),
                        ("is_refusal", "无法回答比例（有效题内）")):
        row = [label]
        for engine in runs:
            vals = valid[engine]
            if name == "intent":
                count = sum(1 for r in vals if norm_intent(r.get("intent")) != "unclear")
            else:
                count = sum(1 for r in vals if r.get(name))
            row.append(f"{count}（{pct(count, len(vals))}）")
        rows.append("| " + " | ".join(row) + " |")
    lat_row = ["决策耗时P50/P95"]
    for engine in runs:
        lats = [r["decision_latency_ms"] for r in valid[engine] if r["status"] == "ok"]
        lat_row.append(f"{percentile(lats, 0.5)}ms / {percentile(lats, 0.95)}ms")
    rows.append("| " + " | ".join(lat_row) + " |")
    fb_row = ["服务回退数(*_failed)"]
    rows.append("| " + " | ".join(fb_row + [str(fallback_counts[e]) for e in runs]) + " |")
    err_row = ["异常数"]
    rows.append("| " + " | ".join(err_row + [str(sum(1 for r in runs[e] if r['status'] != 'ok')) for e in runs]) + " |")
    lines.extend(rows)
    if source_results:
        subset_n = len(valid[next(iter(runs))])
        lines.append(f"- P50/P95 按本版 **{subset_n}** 条子集计算，不与上一轮分母的分位比较。")
    fb_engines = [e for e in runs if fallback_counts[e] > 0]
    if fb_engines:
        lines.append(f"- **检测到回退引擎: {', '.join(f'{e}（{e}_failed×{fallback_counts[e]}）' for e in fb_engines)}**——"
                     "对应题目的意图/情绪为降级结果（unclear/neutral），**已从有效题指标中剔除**，不代表引擎真实判定")
    lines.append("")
    lines.append("> 回退说明：服务回退数>0表示该引擎当时依赖的外部服务（kev.serve/LLM API）不可用或超时；")
    lines.append("> 异常数=单题抛异常。逐题原始数据见同目录 `real_compare_<时间戳>_results.json`。")
    lines.append("")

    # 2. 意图分布（有效题：剔除*_failed回退——回退题的unclear会压低引擎真实口径）
    lines.append("## 2. 意图分布（按引擎，有效题）")
    lines.append("")
    lines.append("| 意图 | " + " | ".join(runs) + " |")
    lines.append("|---|" + "---|" * len(runs))
    intent_counters = {e: Counter(norm_intent(r.get("intent")) for r in valid[e]) for e in runs}
    valid_totals = {e: len(valid[e]) for e in runs}
    for intent in INTENT_ORDER:
        cells = [f"{intent_counters[e].get(intent, 0)}（{pct(intent_counters[e].get(intent, 0), valid_totals[e])}）"
                 for e in runs]
        lines.append(f"| {INTENT_LABELS[intent]} | " + " | ".join(cells) + " |")
    lines.append("")

    # 3. 情绪分布（有效题）
    lines.append("## 3. 情绪分布（按引擎，有效题）")
    lines.append("")
    lines.append("| 情绪 | " + " | ".join(runs) + " |")
    lines.append("|---|" + "---|" * len(runs))
    emotion_counters = {e: Counter(r.get("emotion") or "neutral" for r in valid[e]) for e in runs}
    for emotion in ("neutral", "positive", "urgent", "dissatisfied", "complaint_risk"):
        cells = [f"{emotion_counters[e].get(emotion, 0)}（{pct(emotion_counters[e].get(emotion, 0), valid_totals[e])}）"
                 for e in runs]
        lines.append(f"| {EMOTION_LABELS[emotion]} | " + " | ".join(cells) + " |")
    lines.append("")

    # 3.1 处理路径分布（全部题，含回退）——无法回答比例的分解视图
    lines.append("## 3.1 处理路径分布（按引擎，全部题）")
    lines.append("")
    lines.append("| 路径 | " + " | ".join(runs) + " |")
    lines.append("|---|" + "---|" * len(runs))
    PATH_LABELS = {"clarification": "澄清", "faq_match": "FAQ直连", "llm_generation": "LLM生成",
                   "human_escalation": "转人工", "error": "异常", "unknown": "未知"}
    path_counters = {e: Counter(r.get("path") or "unknown" for r in runs[e]) for e in runs}
    for path in ("clarification", "faq_match", "llm_generation", "human_escalation", "error"):
        cells = [f"{path_counters[e].get(path, 0)}（{pct(path_counters[e].get(path, 0), total_q)}）"
                 for e in runs]
        lines.append(f"| {PATH_LABELS[path]} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("> 读法：无法回答比例=转人工+LLM生成内拒答；转人工高=引擎判升级多，")
    lines.append("> LLM生成内拒答高=知识库未覆盖而拒答——两者性质不同。")
    lines.append("> 转人工归因（逐题 JSON 的 escalate_to_human / clarification_count）：")
    for engine in runs:
        esc = [r for r in runs[engine] if r.get("path") == "human_escalation"]
        own = sum(1 for r in esc if r.get("escalate_to_human") and int(r.get("clarification_count") or 0) < 2)
        doubt = len(esc) - own
        lines.append(f"> - {engine}：转人工 {len(esc)} 条，其中 {own} 条是引擎自己判 escalate"
                     f"（clarification_count<2），{doubt} 条发生在澄清≥2，可能是澄清耗尽。")
    lines.append("")

    # 4. 意图不一致清单（仅四引擎全部有效的题参与比较；回退题不产生虚假不一致）
    lines.append(f"## 4. 各引擎意图不一致的问题清单（仅{title_n}引擎全部有效的题）")
    lines.append("")
    mismatches = 0
    comparable = 0
    by_q: dict[tuple, dict] = {}
    question_of: dict[tuple, str] = {}
    for engine in runs:
        for r in runs[engine]:
            if str(r.get("engine", "")).endswith("_failed"):
                continue
            key = (r["session_id"], r.get("q_index", 0))
            by_q.setdefault(key, {})[engine] = norm_intent(r.get("intent"))
            question_of.setdefault(key, r.get("question", ""))
    lines.append("| 会话 | 问题 | " + " | ".join(runs) + " |")
    lines.append("|---|---|" + "---|" * len(runs))
    for (sid, qi), intents in by_q.items():
        if len(intents) < len(runs):
            continue  # 有引擎回退的题不参与不一致比较
        comparable += 1
        if len(set(intents.values())) > 1:
            mismatches += 1
            cells = [INTENT_LABELS.get(intents.get(e, "?"), intents.get(e, "?")) for e in runs]
            lines.append(f"| {sid} | {md_cell(question_of[(sid, qi)][:40])} | " + " | ".join(cells) + " |")
    if mismatches == 0:
        lines.append("（无——可比较的题各引擎意图一致）")
    lines.append("")
    lines.append(f"可比较题（{title_n}引擎全部有效）{comparable} 条，不一致 **{mismatches}** 条（{pct(mismatches, comparable)}）。")
    lines.append("")
    lines.append("")

    # 5. 情绪专项（任务6）
    lines.append("## 5. 情绪专项：负面关键词消息的 dissatisfied/complaint_risk 命中率")
    lines.append("")
    lines.append(f"关键词：{'、'.join(EMOTION_KEYWORDS)}")
    lines.append("")
    emotion_subset = {e: [r for r in runs[e] if any(kw in r["question"] for kw in EMOTION_KEYWORDS)]
                      for e in runs}
    anchor = next(iter(emotion_subset.values()))
    lines.append(f"命中关键词的问题共 **{len(anchor)}** 条（按问题文本统计，与引擎无关）。")
    kw_detail = {kw: sum(1 for r in anchor if kw in r["question"]) for kw in EMOTION_KEYWORDS}
    lines.append("按关键词命中明细（单字'退'会命中退款/退货/退役等词，明细供判读）：")
    lines.append("".join(f"{kw}×{n}  " for kw, n in kw_detail.items() if n))
    lines.append("")
    lines.append("")
    lines.append("| 引擎 | 判为不满/投诉风险 | 命中率 | 误判为中性/其他 |")
    lines.append("|---|---|---|---|")
    for engine in runs:
        subset = emotion_subset[engine]
        hit = sum(1 for r in subset if r.get("emotion") in ("dissatisfied", "complaint_risk"))
        lines.append(f"| {engine} | {hit} | {pct(hit, len(subset))} | {len(subset) - hit} |")
    lines.append("")

    # 6. 新旧对比（任务7）：数据由main()预先计算（全量旧回复统计 + rule重答）
    old_stat, refs_by_sid, new_by_sid, new_stat = new_old
    anchor_engine = "rule" if "rule" in runs else next(iter(runs))
    lines.append(build_section6(refs_by_sid, new_by_sid, old_stat, new_stat, anchor_engine))
    return "\n".join(lines)


def recompute_from_results(source: Path, args) -> None:
    """同轮重算：套新排除清单筛上一轮逐题结果，不调用任何引擎。"""
    payload = json.loads(source.read_text(encoding="utf-8"))
    runs_in = payload["runs"]
    drop = set(EXPLICIT_EXCLUSIONS)
    runs = {}
    for engine, rows in runs_in.items():
        runs[engine] = [r for r in rows if (r["session_id"], r.get("q_index", 0)) not in drop]
    kept_ids = []
    for sid in dict.fromkeys(r["session_id"] for r in next(iter(runs.values()))):
        kept_ids.append(sid)
    data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
    by_id = {s["session_id"]: s for s in data["sessions"]}
    sessions = [by_id[sid] for sid in kept_ids if sid in by_id]
    # 排除清单 = 上一轮已在选队列剔掉的模式命中 + 本轮显式清单。
    # 模式命中的原文不在上一轮 JSON 里，按会话从 sessions.json 补。
    pattern_sids = ["S009", "S020", "S024", "S025", "S026"]
    excluded = cohort_exclusions([by_id[sid] for sid in pattern_sids + ["S021", "S022"] if sid in by_id])
    print(f"同轮重算：读 {source.name}，显式剔除 {len(drop)} 条，"
          f"剩余 {len(next(iter(runs.values())))} 题 / {len(sessions)} 会话，未调用引擎")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_path = RESULTS_DIR / f"real_compare_{stamp}_results.json"
    questions_unique = len(next(iter(runs.values())))
    results_path.write_text(json.dumps({
        "cohort": {
            "sessions": len(sessions),
            "questions_unique": questions_unique,
            "engine_rows": sum(len(r) for r in runs.values()),
            "excluded_non_customer": len(EXPLICIT_EXCLUSIONS) + int(
                (payload.get("cohort") or {}).get("excluded_non_customer") or 0),
            "recomputed_from": source.name,
            "engines_called": False,
        },
        "engine_classes": payload.get("engine_classes") or {},
        "fallback_counts": {e: sum(1 for r in runs[e] if str(r.get("engine", "")).endswith("_failed"))
                            for e in runs},
        "runs": runs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    import hashlib
    import subprocess
    anchor = {"llm_base": "http://172.28.50.9:30030/v1", "llm_model": "Qwen3.8-27B"}
    # 短哈希。报告文件自己也在这次提交里，所以取到的是父提交；
    # 入库前用最终 HEAD 回填报告里的「代码提交 `…`」一行，再 amend。
    git_sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, cwd=str(PROJECT_ROOT)).stdout.strip()
    sdwan_sha = hashlib.sha256((PROJECT_ROOT / "data" / "sdwan.md").read_bytes()).hexdigest()
    sessions_sha = hashlib.sha256(SESSIONS_JSON.read_bytes()).hexdigest()
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    report = generate_report(runs, payload.get("engine_classes") or {e: e for e in runs},
                             sessions, args, anchor,
                             ((0, 0), {}, {}, (0, 0)),
                             git_sha=git_sha, sdwan_sha=sdwan_sha,
                             excluded=excluded, sessions_sha=sessions_sha,
                             source_results=source.name, source_results_sha=source_sha)
    note = (f"- **同轮重算**: 读 `{source.name}` 套新排除清单重新出报告，"
            "未重新调用任何引擎。被剔题的原始记录仍在上一轮产物里。")
    report = report.replace("逐题原始数据见同目录 `real_compare_<时间戳>_results.json`",
                            f"逐题原始数据见同目录 `{results_path.name}`")
    report = report.replace("- **结论**:", note + "\n- **结论**:")
    out = RESULTS_DIR / f"real_compare_{stamp}.md"
    out.write_text(report, encoding="utf-8")
    print(f"逐题结果已落盘: {results_path}")
    print(f"报告已生成: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="微信真实会话四引擎对比")
    parser.add_argument("--sessions", type=int, default=30, help="对齐会话数（默认30，kev拍板限制）")
    parser.add_argument("--include-jev", action="store_true",
                        help="追加Jev引擎（仅限脱敏文本，真实消息绝不发Jev）")
    parser.add_argument("--skip-kev", action="store_true",
                        help="显式跳过kev（默认仍跑kev；当前Linux主机无GPU/无kev环境时用）")
    parser.add_argument("--new-old-only", action="store_true",
                        help="只跑新旧对比pass（全量旧回复统计+rule重答），不跑四引擎对比")
    parser.add_argument("--from-results", default="",
                        help="读上一轮逐题 JSON 重算，不调用任何引擎")
    args = parser.parse_args()
    if args.from_results:
        recompute_from_results(Path(args.from_results), args)
        return

    data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
    all_sessions = data["sessions"]
    sessions = all_sessions[:args.sessions] if args.sessions > 0 else all_sessions
    excluded = cohort_exclusions(sessions)
    kept = sum(len(customer_questions(s)) for s in sessions)
    print(f"选队列剔除非客户 {len(excluded)} 条，进入评测 {kept} 条")

    old_stat, refs_by_sid = compute_old_repeat(all_sessions)
    ref_count = sum(len(v) for v in refs_by_sid.values())

    if args.new_old_only:
        print(f"新旧对比pass：全量{ref_count}条带旧回复的问题，rule引擎重答")
        new_by_sid = asyncio.run(run_new_answers(refs_by_sid))
        new_stat = repeat_rate(new_by_sid)
        section = build_section6(refs_by_sid, new_by_sid, old_stat, new_stat, "rule")
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"real_compare_newold_{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        out.write_text(section, encoding="utf-8")
        print(f"新旧对比已生成: {out}")
        return

    engines = ["rule", "kev", "qwen"] + (["jev"] if args.include_jev else [])
    if args.skip_kev:
        engines = [e for e in engines if e != "kev"]
    print(f"对齐队列: {len(sessions)}个会话，引擎: {engines}")

    runs: dict[str, list[dict]] = {}
    engine_classes: dict[str, str] = {}
    anchor: dict = {}
    for engine in engines:
        print(f"=== 引擎 {engine} ===")
        results, engine_class = asyncio.run(run_engine(engine, sessions))
        runs[engine] = results
        engine_classes[engine] = engine_class
        if not anchor:
            os.environ["DECISION_ENGINE"] = engine
            _orch = Orchestrator()
            anchor = {"llm_base": _orch.llm_client.base_url, "llm_model": _orch.llm_client.model}

    print(f"=== 新旧对比pass（{ref_count}条带旧回复问题） ===")
    new_by_sid = asyncio.run(run_new_answers(refs_by_sid))
    new_stat = repeat_rate(new_by_sid)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # 逐题原始结果落盘：报告数字的可核对来源（验收要求）
    results_path = RESULTS_DIR / f"real_compare_{stamp}_results.json"
    engine_rows = sum(len(r) for r in runs.values())
    questions_unique = len(next(iter(runs.values()))) if runs else 0
    results_path.write_text(json.dumps({
        "cohort": {
            "sessions": len(sessions),
            "questions_unique": questions_unique,
            "engine_rows": engine_rows,
            "excluded_non_customer": len(excluded),
        },
        "engine_classes": engine_classes,
        "fallback_counts": {e: len(runs[e]) - len([r for r in runs[e] if not str(r.get("engine", "")).endswith("_failed")]) for e in runs},
        "runs": runs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    import hashlib
    import subprocess
    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                            cwd=str(PROJECT_ROOT)).stdout.strip()
    sdwan_sha = hashlib.sha256((PROJECT_ROOT / "data" / "sdwan.md").read_bytes()).hexdigest()
    report = generate_report(runs, engine_classes, sessions, args, anchor,
                             (old_stat, refs_by_sid, new_by_sid, new_stat),
                             git_sha=git_sha, sdwan_sha=sdwan_sha, excluded=excluded)
    out = RESULTS_DIR / f"real_compare_{stamp}.md"
    report = report.replace("逐题原始数据见同目录 `real_compare_<时间戳>_results.json`",
                            f"逐题原始数据见同目录 `{results_path.name}`")
    out.write_text(report, encoding="utf-8")
    print(f"逐题结果已落盘: {results_path}")
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
