"""准确率评估脚本：读取标注用例JSON，逐题运行完整编排流程并计算准确率指标。

用法:
    python tests/accuracy_evaluation.py tests/accuracy_test_cases.json [--engine rule|jev|kev]

标注格式（每条）:
    question             用户问题
    expected_keywords    预期答案关键词（任一命中即该题答案正确，允许LLM同义改写）
    expected_sources     预期知识库来源（任一命中即来源正确；空列表=不考核来源）
    should_answer        true=知识库应能作答；false=正确行为是拒答/转人工

指标:
    answer_accuracy    答案准确率：should_answer=true时关键词任一命中；false时正确拒答
    source_accuracy    来源准确率：expected_sources非空的作答题中，返回来源任一命中
    refusal_accuracy   拒答判断准确率：should_answer=false的用例中正确拒答的比例

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

REFUSAL_MARKERS = ("无法回答", "人工", "抱歉", "暂时")


def is_refusal(answer: str) -> bool:
    """判断回复是否为拒答/转人工类话术。"""
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

    keyword_hit = [kw for kw in keywords if kw in answer]
    source_hit = [s for s in expected_sources if s in sources]
    refused = is_refusal(answer)
    clarified = need_clarification or path == "clarification"

    if should_answer:
        answer_pass = bool(keyword_hit) and not refused and not clarified
    else:
        answer_pass = refused and not clarified
    source_pass = bool(source_hit) if (should_answer and expected_sources) else True
    refusal_pass = refused if not should_answer else True

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
        "answer_pass": answer_pass,
        "source_pass": source_pass,
        "refusal_pass": refusal_pass,
        "clarified": clarified,
    }


async def run_evaluation(cases_file: Path) -> Path:
    """运行全部标注用例，写评估结果JSON，返回结果文件路径。"""
    cases = json.loads(cases_file.read_text(encoding="utf-8"))
    print(f"加载 {len(cases)} 个标注用例，决策引擎环境: {os.environ.get('DECISION_ENGINE', '(读取.env)')}")

    orchestrator = Orchestrator()
    engine_name = type(orchestrator.decision_engine).__name__

    detailed: list[dict] = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['question']}")
        detailed.append(await evaluate_case(orchestrator, case))

    answer_items = [d for d in detailed]
    source_items = [d for d in detailed if d["should_answer"] and d["expected_sources"]]
    refusal_items = [d for d in detailed if not d["should_answer"]]
    metrics = {
        "answer_accuracy": round(sum(1 for d in answer_items if d["answer_pass"]) / len(answer_items), 4) if answer_items else 0.0,
        "source_accuracy": round(sum(1 for d in source_items if d["source_pass"]) / len(source_items), 4) if source_items else 0.0,
        "refusal_accuracy": round(sum(1 for d in refusal_items if d["refusal_pass"]) / len(refusal_items), 4) if refusal_items else 0.0,
        "clarification_rate": round(sum(1 for d in detailed if d["clarified"]) / len(detailed), 4) if detailed else 0.0,
        "total_cases": len(detailed),
        "answer_passed": sum(1 for d in answer_items if d["answer_pass"]),
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
            "scoring": "answer: should_answer=true时expected_keywords任一命中且非拒答非澄清；false时正确拒答。澄清反问不计作答（单列clarification_rate）。source: 预期来源任一命中。",
        },
        "accuracy_metrics": metrics,
        "detailed_results": detailed,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n答案准确率: {metrics['answer_accuracy'] * 100:.1f}%（{metrics['answer_passed']}/{metrics['total_cases']}，目标≥85%，严格口径：澄清不计作答）")
    print(f"来源准确率: {metrics['source_accuracy'] * 100:.1f}%（{metrics['source_passed']}/{metrics['source_scored']}）")
    print(f"拒答准确率: {metrics['refusal_accuracy'] * 100:.1f}%（{metrics['refusal_cases']}例）")
    print(f"澄清率: {metrics['clarification_rate'] * 100:.1f}%（{metrics['clarified_cases']}例）")
    print(f"结果文件: {out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="SDWAN客服准确率评估")
    parser.add_argument("cases_file", nargs="?", default="tests/accuracy_test_cases.json",
                        help="标注用例JSON文件")
    parser.add_argument("--engine", choices=["rule", "jev", "kev"], default=None,
                        help="指定决策引擎（默认读环境变量/.env）")
    args = parser.parse_args()

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
