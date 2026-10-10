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
    """P50/P95：升序取分位（最接近的分位值）。"""
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[idx]


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
        questions = [t["text"] for t in s["turns"] if t["speaker"] == "customer"]
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
                    sessions: list[dict], args,
                    new_old: tuple[tuple[int, int], dict[str, list[dict]], dict[str, list[str]], tuple[int, int]]) -> str:
    """汇总为Markdown报告。new_old: (old_stat, refs_by_sid, new_by_sid, new_stat)。"""
    lines: list[str] = []
    lines.append("# 微信真实会话四引擎对比报告")
    lines.append("")
    lines.append(f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **数据**: data/wx_real/sessions.json（已脱敏；前{len(sessions)}个会话为对齐队列）")
    lines.append(f"- **引擎**: {', '.join(f'{k}({engine_classes[k]})' for k in runs)}")
    if not args.include_jev:
        lines.append("- **Jev未参与**（数据出境红线，默认关闭；如需评估请用--include-jev，仅限脱敏文本）")
    lines.append("- **kev限速说明**: kev本机推理慢（4GB卡禁用CUDA graphs），按拍板取前30个会话对齐队列")
    lines.append("")
    total_q = len(next(iter(runs.values())))
    lines.append(f"对齐队列共 **{len(sessions)}** 个会话 / **{total_q}** 条客户问题。")
    lines.append("")

    # 1. 引擎对比总表
    lines.append("## 1. 引擎对比总表")
    lines.append("")
    lines.append("| 指标 | " + " | ".join(runs) + " |")
    lines.append("|---|" + "---|" * len(runs))
    rows = []
    for name, label in (("intent", "意图≠未识别"), ("need_clarification", "澄清比例"),
                        ("is_refusal", "无法回答比例")):
        row = [label]
        for engine in runs:
            vals = [r for r in runs[engine]]
            if name == "intent":
                count = sum(1 for r in vals if norm_intent(r.get("intent")) != "unclear")
            else:
                count = sum(1 for r in vals if r.get(name))
            row.append(f"{count}（{pct(count, len(vals))}）")
        rows.append("| " + " | ".join(row) + " |")
    lat_row = ["决策耗时P50/P95"]
    for engine in runs:
        lats = [r["decision_latency_ms"] for r in runs[engine] if r["status"] == "ok"]
        lat_row.append(f"{percentile(lats, 0.5)}ms / {percentile(lats, 0.95)}ms")
    rows.append("| " + " | ".join(lat_row) + " |")
    err_row = ["异常数"]
    rows.append("| " + " | ".join(err_row + [str(sum(1 for r in runs[e] if r['status'] != 'ok')) for e in runs]) + " |")
    lines.extend(rows)
    lines.append("")

    # 2. 意图分布
    lines.append("## 2. 意图分布（按引擎）")
    lines.append("")
    lines.append("| 意图 | " + " | ".join(runs) + " |")
    lines.append("|---|" + "---|" * len(runs))
    intent_counters = {e: Counter(r.get("intent") or "unclear" for r in runs[e]) for e in runs}
    for intent in INTENT_ORDER:
        cells = [f"{intent_counters[e].get(intent, 0)}（{pct(intent_counters[e].get(intent, 0), total_q)}）"
                 for e in runs]
        lines.append(f"| {INTENT_LABELS[intent]} | " + " | ".join(cells) + " |")
    lines.append("")

    # 3. 情绪分布
    lines.append("## 3. 情绪分布（按引擎）")
    lines.append("")
    lines.append("| 情绪 | " + " | ".join(runs) + " |")
    lines.append("|---|" + "---|" * len(runs))
    emotion_counters = {e: Counter(r.get("emotion") or "neutral" for r in runs[e]) for e in runs}
    for emotion in ("neutral", "positive", "urgent", "dissatisfied", "complaint_risk"):
        cells = [f"{emotion_counters[e].get(emotion, 0)}（{pct(emotion_counters[e].get(emotion, 0), total_q)}）"
                 for e in runs]
        lines.append(f"| {EMOTION_LABELS[emotion]} | " + " | ".join(cells) + " |")
    lines.append("")

    # 4. 意图不一致清单（按(session_id, q_index)序号对齐——同会话重复问题不互相覆盖）
    lines.append("## 4. 各引擎意图不一致的问题清单")
    lines.append("")
    mismatches = 0
    by_q: dict[tuple, dict] = {}
    question_of: dict[tuple, str] = {}
    for engine in runs:
        for r in runs[engine]:
            key = (r["session_id"], r.get("q_index", 0))
            by_q.setdefault(key, {})[engine] = norm_intent(r.get("intent"))
            question_of.setdefault(key, r.get("question", ""))
    lines.append("| 会话 | 问题 | " + " | ".join(runs) + " |")
    lines.append("|---|---|" + "---|" * len(runs))
    for (sid, qi), intents in by_q.items():
        if len(set(intents.values())) > 1:
            mismatches += 1
            cells = [INTENT_LABELS.get(intents.get(e, "?"), intents.get(e, "?")) for e in runs]
            lines.append(f"| {sid} | {md_cell(question_of[(sid, qi)][:40])} | " + " | ".join(cells) + " |")
    if mismatches == 0:
        lines.append("（无——所有问题各引擎意图一致）")
    lines.append("")
    lines.append(f"不一致问题共 **{mismatches}** 条（占 {pct(mismatches, total_q)}）。")
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


def main() -> None:
    parser = argparse.ArgumentParser(description="微信真实会话四引擎对比")
    parser.add_argument("--sessions", type=int, default=30, help="对齐会话数（默认30，kev拍板限制）")
    parser.add_argument("--include-jev", action="store_true",
                        help="追加Jev引擎（仅限脱敏文本，真实消息绝不发Jev）")
    parser.add_argument("--new-old-only", action="store_true",
                        help="只跑新旧对比pass（全量旧回复统计+rule重答），不跑四引擎对比")
    args = parser.parse_args()

    data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
    all_sessions = data["sessions"]
    sessions = all_sessions[:args.sessions] if args.sessions > 0 else all_sessions

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
    print(f"对齐队列: {len(sessions)}个会话，引擎: {engines}")

    runs: dict[str, list[dict]] = {}
    engine_classes: dict[str, str] = {}
    for engine in engines:
        print(f"=== 引擎 {engine} ===")
        results, engine_class = asyncio.run(run_engine(engine, sessions))
        runs[engine] = results
        engine_classes[engine] = engine_class

    print(f"=== 新旧对比pass（{ref_count}条带旧回复问题） ===")
    new_by_sid = asyncio.run(run_new_answers(refs_by_sid))
    new_stat = repeat_rate(new_by_sid)

    report = generate_report(runs, engine_classes, sessions, args,
                             (old_stat, refs_by_sid, new_by_sid, new_stat))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"real_compare_{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text(report, encoding="utf-8")
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
