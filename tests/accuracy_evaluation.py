"""准确率评估脚本：读取标注用例JSON，逐题运行完整编排流程并计算准确率指标。

用法:
    python tests/accuracy_evaluation.py tests/accuracy_test_cases.json [--engine rule|jev|kev]

标注格式（每条）:
    question             用户问题
    expected_keywords    预期答案关键词（任一命中即该题答案正确，允许LLM同义改写）
    expected_sources     预期知识库来源（任一命中即来源正确；空列表=不考核来源）
    should_answer        true=知识库应能作答；false=正确行为是拒答/转人工

指标（#059 定稿：三个数分开报，别用一个百分比兼着两种语义）:
    action_accuracy    处置正确率（主指标）：(P 答对 + handoff 转对) / (P 条数 + handoff 条数)
    answer_accuracy    P 答案准确率：P 题里「答得对」的比例（expected_keywords 任一命中且非拒答非澄清）
    handoff_accuracy   转人工命中率：handoff 题里「该找人时真找了」的比例（出现转人工动作标记）
    source_accuracy    来源准确率：P 题中 expected_sources 非空的，返回来源任一命中
    refusal_accuracy   负样本合计（含 handoff 与历史无 expected_action 的负样本），历史口径保留
    refuse_observed    纯拒答**观察项**：expected_action=refuse 的用例中只说不提供/答不了、
                       且没有转人工动作的比例；#059 裁决：不进主准确率分母（产品无纯拒答话术）

#059 补标数据额外口径:
    - 带 skip_reason 的题（含安全组 K）整条跳过，不进任何分母，结果 JSON 单列 skipped / security
    - 用例文件既可以是裸 list，也可以是 {"cases": [...]}（labeled.json 的格式）

输出: tests/results/accuracy_eval_<engine>_<时间戳>.json
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.orchestrator.orchestrator import Orchestrator  # noqa: E402

RESULTS_DIR = PROJECT_ROOT / "tests" / "results"

# ---------------------------------------------------------------- 动作标记
# #059 C2 硬原则（任务书 :176）：按**动作**判、两者互斥、**先判 handoff 再判 refuse**。
# 回答里出现转人工动作 = handoff；没有转人工动作、只说明不提供/答不了 = refuse；
# 不许把两类词并成一个集合判「是不是拒答」。
#
# 标记表已由 Chief 2026-10-25 定稿：**每条都必须有代码或真实回复的出处**，
# 拒绝凭空加词。命令行可用 --handoff-markers / --refuse-markers 临时覆盖。
HANDOFF_MARKERS = (
    # backend/orchestrator/handlers.py 固定转人工话术（真实回复里就是这个）；
    # #062 拆分后为 settings.HANDOFF_MESSAGE「这个问题需要同事确认，已为您转接人工客服。」
    "人工介入",
    "转接人工",
    "人工客服",
    "同事确认",
    "人工会尽快与您联系",
    # tests/results/batch_test_20261009-155725.json 真实回复：
    # 「…可能需要人工帮你看看线路状态和带宽情况。方便的话我帮你转人工处理？」
    "帮你转人工",
    "人工帮你看看",
    # tests/results/kb_ab_20261023-old.json 真实回复：
    # 「…具体免费几天我这边没有明确信息，需要人工帮你确认一下。」
    "人工帮你确认",
    # data/sdwan-new.md 问题3 的人工介入动作「（需人工介入：发通知到管理群）」
    "通知管理群",
)

REFUSE_MARKERS = (
    # #062 拆分后的固定纯拒答话术（settings.REFUSE_MESSAGE，不含「人工」二字）：
    # 「这个问题我暂时回答不了，您可以换个问法，或稍后再问我。」
    "回答不了",
    "换个问法",
    # 旧 canned / LLM 变体仍可能出现的纯拒答说法（先判 handoff，带「人工」的轮不到它）
    "无法回答",
    # 知识库里没有可直接作答的条目时的自然说法（真实回复见
    # tests/results/batch_test_20261009-155725.json「这个情况知识库里没有直接的排查方案」）
    "知识库里没有",
)

# 无真实样本、**默认关闭**的候选串（Chief 2026-10-25：不许写进默认值，否则误伤
# 会把正常回答判成拒答）。等真出现这类回复、且确认不误伤后再并入 REFUSE_MARKERS。
# （「回答不了」已随 #062 话术拆分提为正式——新纯拒答话术固定含它。）
REFUSE_MARKERS_CANDIDATES = ("答不了", "没有这方面的信息", "不清楚", "不知道")

# 旧口径：任一类拒答话术都算「拒答」（只给**没有** expected_action 的历史用例兜底，
# 新补标数据一律走 classify_reply_action 的互斥判定）
REFUSAL_MARKERS = HANDOFF_MARKERS + REFUSE_MARKERS

# 报告里固定带的一句口径说明（#059 Chief 定：refuse 是观察项；#062 拆分后产品已有
# 纯拒答话术，说明随事实更新——指标仍不进主分母，反映的是拒答/转人工的区分能力）
REFUSE_OBSERVED_NOTE = (
    "refuse 为观察项，不进主分母；#062 话术拆分后产品已有纯拒答话术"
    "（config.REFUSE_MESSAGE，不含『人工』），该指标反映模型区分拒答/转人工的能力"
)


def classify_reply_action(answer: str) -> str:
    """按动作给回答分类：'handoff' / 'refuse' / 'other'（互斥，先 handoff 后 refuse）。"""
    text = answer or ""
    if any(marker in text for marker in HANDOFF_MARKERS):
        return "handoff"
    if any(marker in text for marker in REFUSE_MARKERS):
        return "refuse"
    return "other"


def is_refusal(answer: str) -> bool:
    """历史口径：是否拒答/转人工类话术（新数据请用 classify_reply_action 区分两类）。"""
    return any(marker in answer for marker in REFUSAL_MARKERS)


async def evaluate_case(orchestrator: Orchestrator, case: dict) -> dict:
    """运行单个标注用例并逐项打分。

    严格口径（Phase 4评估驱动）：澄清反问不算作答——澄清话术常罗列价格选项等
    具体内容，宽松口径会被关键词命中误判为正确回答。澄清单列clarification_rate。
    """
    start = time.monotonic()
    try:
        result = await orchestrator.process(case["question"], {})
        answer = result.get("answer", "")
        sources = result.get("sources", [])
        path = result.get("path", "")
        need_clarification = bool(result.get("need_clarification"))
        error = ""
    except Exception as exc:  # noqa: BLE001 单题异常计为不通过，不中断评估
        answer, sources, path, need_clarification = "", [], "error", False
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.monotonic() - start) * 1000)

    keywords = case.get("expected_keywords", [])
    expected_sources = case.get("expected_sources", [])
    should_answer = case.get("should_answer", True)
    expected_action = (case.get("expected_action") or "").strip().lower()

    keyword_hit = [kw for kw in keywords if kw in answer]
    source_hit = [s for s in expected_sources if s in sources]
    refused = is_refusal(answer)
    reply_action = classify_reply_action(answer)   # handoff > refuse > other，互斥
    clarified = need_clarification or path == "clarification"

    if should_answer:
        answer_pass = bool(keyword_hit) and not refused and not clarified
    elif expected_action == "handoff":
        # 该转人工：回答里必须真有转人工动作
        answer_pass = reply_action == "handoff" and not clarified
    elif expected_action == "refuse":
        # 该纯拒答：只说不提供/答不了；出现转人工动作就算错（两者互斥）
        answer_pass = reply_action == "refuse" and not clarified
    else:
        # 历史用例没有 expected_action：沿用旧口径（任一类拒答话术都算）
        answer_pass = refused and not clarified
    source_pass = bool(source_hit) if (should_answer and expected_sources) else True
    if not should_answer:
        # 新数据用严格口径（互斥），历史用例保持原口径不变
        refusal_pass = answer_pass if expected_action in ("refuse", "handoff") else refused
    else:
        refusal_pass = True

    return {
        "question": case["question"],
        "answer": answer,
        "sources": sources,
        "path": path,
        "need_clarification": need_clarification,
        "latency_ms": latency_ms,
        "error": error,
        "expected_keywords": keywords,
        "keyword_hit": keyword_hit,
        "expected_sources": expected_sources,
        "source_hit": source_hit,
        "should_answer": should_answer,
        "expected_action": expected_action,
        "reply_action": reply_action,
        "handoff_pass": (reply_action == "handoff") if expected_action == "handoff" else None,
        "refuse_pass": (reply_action == "refuse") if expected_action == "refuse" else None,
        "answer_pass": answer_pass,
        "source_pass": source_pass,
        "refusal_pass": refusal_pass,
        "clarified": clarified,
    }


def load_cases(cases_file: Path) -> tuple[list[dict], dict]:
    """读标注用例：既认裸 list（历史 accuracy_test_cases.json），也认 {"cases": [...]}。

    #059 修：labeled.json 是 {description, cases}，旧代码直接把 dict 当 list 用，
    len() 得到 2、item 是字符串，逐题循环必然炸。
    """
    raw = json.loads(Path(cases_file).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw, {}
    if isinstance(raw, dict) and isinstance(raw.get("cases"), list):
        return raw["cases"], {k: v for k, v in raw.items() if k != "cases"}
    raise ValueError(
        f"标注用例格式不支持（要 list 或 {{'cases': [...]}}）: {cases_file}"
    )


def _rate(items: list[dict], key: str) -> float:
    return round(sum(1 for d in items if d[key]) / len(items), 4) if items else 0.0


async def run_evaluation(cases_file: Path) -> Path:
    """运行全部标注用例，写评估结果JSON，返回结果文件路径。

    #059：带 skip_reason 的题（含安全组 K）整条跳过——不跑引擎、不进任何分母，
    结果里单列 skipped / security 计数；可考核题才进准确率。
    """
    all_cases, file_meta = load_cases(cases_file)
    skipped_cases = [c for c in all_cases if (c.get("skip_reason") or "").strip()]
    security_cases = [c for c in skipped_cases
                      if c.get("security_flag") or c.get("skip_reason") == "security"]
    cases = [c for c in all_cases if not (c.get("skip_reason") or "").strip()]
    print(f"加载 {len(all_cases)} 条标注：可考核 {len(cases)} 条；"
          f"跳过 {len(skipped_cases)} 条（其中安全组 {len(security_cases)} 条）")
    print(f"决策引擎环境: {os.environ.get('DECISION_ENGINE', '(读取.env)')}")

    orchestrator = Orchestrator()
    engine_name = type(orchestrator.decision_engine).__name__

    detailed: list[dict] = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['question']}")
        detailed.append(await evaluate_case(orchestrator, case))

    # #059 Chief 裁决：refuse 类降级为**观察项**，不进主分母（当时产品固定话术
    # 一句话同时表达拒答与转人工；#062 拆分后话术已分开，该裁决维持不变）。
    refuse_items = [d for d in detailed if d["expected_action"] == "refuse"]
    scored_items = [d for d in detailed if d["expected_action"] != "refuse"]
    # P 组：「答得对不对」——新数据 expected_action=answer；历史用例（无该字段）按 should_answer=true 归入
    p_items = [d for d in scored_items
               if d["expected_action"] == "answer"
               or (not d["expected_action"] and d["should_answer"])]
    # handoff 组：「该找人时找没找」
    handoff_items = [d for d in scored_items if d["expected_action"] == "handoff"]
    # 主指标分母 = P + handoff（与任务书口径一致）
    action_items = [d for d in scored_items if d in p_items or d in handoff_items]
    source_items = [d for d in p_items if d["expected_sources"]]
    refusal_items = [d for d in scored_items if not d["should_answer"]]
    metrics = {
        # 主指标：两类题的「处置对了」合起来算，但分子分母分开报
        "action_accuracy": _rate(action_items, "answer_pass"),
        "action_cases": len(action_items),
        "action_passed": sum(1 for d in action_items if d["answer_pass"]),
        # P：答得对不对
        "answer_accuracy": _rate(p_items, "answer_pass"),
        "answer_cases": len(p_items),
        "answer_passed": sum(1 for d in p_items if d["answer_pass"]),
        # handoff：该找人时找没找
        "handoff_accuracy": _rate(handoff_items, "handoff_pass"),
        "handoff_cases": len(handoff_items),
        "handoff_passed": sum(1 for d in handoff_items if d["handoff_pass"]),
        "source_accuracy": _rate(source_items, "source_pass"),
        "refusal_accuracy": _rate(refusal_items, "refusal_pass"),
        # 观察项：只如实记录，不进任何主分母
        "refuse_observed": {
            "cases": len(refuse_items),
            "passed": sum(1 for d in refuse_items if d["refuse_pass"]),
            "rate": _rate(refuse_items, "refuse_pass"),
            "in_main_denominator": False,
        },
        "refuse_cases": len(refuse_items),
        "refuse_passed": sum(1 for d in refuse_items if d["refuse_pass"]),
        "clarification_rate": round(sum(1 for d in detailed if d["clarified"]) / len(detailed), 4) if detailed else 0.0,
        "evaluated_cases": len(detailed),
        "total_cases": len(action_items),      # 主指标分母 = P + handoff
        "skipped": len(skipped_cases),
        "security": len(security_cases),
        "skipped_non_security": len(skipped_cases) - len(security_cases),
        "source_passed": sum(1 for d in source_items if d["source_pass"]),
        "source_scored": len(source_items),
        "refusal_cases": len(refusal_items),
        "clarified_cases": sum(1 for d in detailed if d["clarified"]),
        "average_latency_ms": round(sum(d["latency_ms"] for d in detailed) / len(detailed)) if detailed else 0,
    }

    out_path = RESULTS_DIR / f"accuracy_eval_{os.environ.get('DECISION_ENGINE', 'default')}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    payload = {
        "metadata": {
            "source_file": cases_file.name,
            "engine_env": os.environ.get("DECISION_ENGINE", ""),
            "engine_class": engine_name,
            "llm_model": orchestrator.llm_client.model,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "scoring": (
                "action_accuracy=(P答对+handoff转对)/(P+handoff)，主指标；"
                "answer_accuracy=P答对/P，handoff_accuracy=handoff转对/handoff，两者分开报；"
                "answer: should_answer=true时expected_keywords任一命中且非拒答非澄清；"
                "false时按expected_action判：handoff=回答含转人工动作，refuse=只拒答且无转人工动作"
                "（先判handoff再判refuse，互斥；无expected_action的历史用例沿用任一类拒答话术）。"
                "澄清反问不计作答（单列clarification_rate）。source: 预期来源任一命中。"
                "#059：带skip_reason的题（含安全组）整条跳过，不进分母，单列skipped/security。"
            ),
            "refuse_observed_note": REFUSE_OBSERVED_NOTE,
            "handoff_markers": list(HANDOFF_MARKERS),
            "refuse_markers": list(REFUSE_MARKERS),
            "source_meta": file_meta,
        },
        "accuracy_metrics": metrics,
        "skipped_cases": [
            {
                "idx": all_cases.index(c) + 1,
                "question": c.get("question", ""),
                "skip_reason": c.get("skip_reason", ""),
                "security": bool(c.get("security_flag") or c.get("skip_reason") == "security"),
            }
            for c in skipped_cases
        ],
        "detailed_results": detailed,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n处置正确率（主指标）: {metrics['action_accuracy'] * 100:.1f}%"
          f"（{metrics['action_passed']}/{metrics['action_cases']}"
          f" = P {metrics['answer_passed']}/{metrics['answer_cases']}"
          f" + handoff {metrics['handoff_passed']}/{metrics['handoff_cases']}）")
    print(f"P 答案准确率: {metrics['answer_accuracy'] * 100:.1f}%"
          f"（{metrics['answer_passed']}/{metrics['answer_cases']}，P 题，严格口径：澄清不计作答）")
    print(f"handoff 命中率: {metrics['handoff_accuracy'] * 100:.1f}%"
          f"（{metrics['handoff_passed']}/{metrics['handoff_cases']}，handoff 题）")
    print(f"来源准确率: {metrics['source_accuracy'] * 100:.1f}%（{metrics['source_passed']}/{metrics['source_scored']}，P 题内有来源的）")
    print(f"负样本合计（含 handoff，历史口径）: {metrics['refusal_accuracy'] * 100:.1f}%（{metrics['refusal_cases']}例）")
    observed = metrics["refuse_observed"]
    print(f"纯拒答观察项: {observed['rate'] * 100:.1f}%（{observed['passed']}/{observed['cases']}，"
          f"refuse 题，不进主分母）")
    print(f"口径说明: {REFUSE_OBSERVED_NOTE}")
    print(f"跳过: {metrics['skipped']} 条（安全组 {metrics['security']} 条、其他 skip {metrics['skipped_non_security']} 条），不进分母")
    print(f"澄清率: {metrics['clarification_rate'] * 100:.1f}%（{metrics['clarified_cases']}例）")
    print(f"结果文件: {out_path}")
    return out_path


def apply_marker_override(handoff: str | None = None, refuse: str | None = None) -> None:
    """命令行临时覆盖动作标记（#059 C2：默认标记表是草案，审批前先这样试跑）。"""
    global HANDOFF_MARKERS, REFUSE_MARKERS, REFUSAL_MARKERS
    if handoff:
        HANDOFF_MARKERS = tuple(x.strip() for x in handoff.split(",") if x.strip())
    if refuse:
        REFUSE_MARKERS = tuple(x.strip() for x in refuse.split(",") if x.strip())
    REFUSAL_MARKERS = HANDOFF_MARKERS + REFUSE_MARKERS


def main() -> None:
    parser = argparse.ArgumentParser(description="SDWAN客服准确率评估")
    parser.add_argument("cases_file", nargs="?", default="tests/accuracy_test_cases.json",
                        help="标注用例JSON文件")
    parser.add_argument("--engine", choices=["rule", "jev", "kev", "qwen"], default=None,
                        help="指定决策引擎（默认读环境变量/.env）")
    parser.add_argument("--handoff-markers", default=None,
                        help="临时覆盖 handoff 标记（逗号分隔；默认值是待审批草案）")
    parser.add_argument("--refuse-markers", default=None,
                        help="临时覆盖 refuse 标记（逗号分隔；默认值是待审批草案）")
    args = parser.parse_args()

    if args.handoff_markers or args.refuse_markers:
        apply_marker_override(args.handoff_markers, args.refuse_markers)

    if args.engine:
        os.environ["DECISION_ENGINE"] = args.engine

    cf = Path(args.cases_file)
    if not cf.is_absolute():
        cf = PROJECT_ROOT / cf
    if not cf.exists():
        print(f"标注用例文件不存在: {cf}")
        sys.exit(1)

    asyncio.run(run_evaluation(cf))


if __name__ == "__main__":
    main()
