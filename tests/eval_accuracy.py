"""阶段3答案准确率评估：20个知识库可答问题经编排器真实作答，按预期关键词评分。

评分规则：answer中命中任一expected_keyword即该题通过（关键词客观取自知识库原文，
允许LLM同义改写）；accuracy = 通过题数/总题数，任务书硬指标>85%。

用法（使用项目 .venv 环境）:
    python tests/eval_accuracy.py
输出: 逐题结果表 + docs/acceptance-evidence/20261009-phase3-accuracy-report.json
退出码: accuracy>=0.85 为0，否则1。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.orchestrator.orchestrator import Orchestrator  # noqa: E402

EVAL_SET = ROOT / "tests" / "accuracy_eval_set.json"
REPORT = ROOT / "docs" / "acceptance-evidence" / "20261009-phase3-accuracy-report.json"
ACCURACY_TARGET = 0.85


async def main() -> int:
    eval_set = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    cases = eval_set["cases"]
    orchestrator = Orchestrator()

    results = []
    passed = 0
    start_all = time.monotonic()
    for case in cases:
        start = time.monotonic()
        try:
            result = await orchestrator.process(case["question"], {})
            answer = result.get("answer", "")
            path = result.get("path", "")
        except Exception as exc:  # noqa: BLE001 记录失败继续
            answer, path = f"<编排器异常: {str(exc)[:120]}>", "error"
        latency = int((time.monotonic() - start) * 1000)

        hit = [kw for kw in case["expected_keywords"] if kw in answer]
        ok = bool(hit)
        passed += ok
        results.append({
            "id": case["id"],
            "question": case["question"],
            "kb_refs": case["kb_refs"],
            "expected_keywords": case["expected_keywords"],
            "hit_keywords": hit,
            "passed": ok,
            "path": path,
            "latency_ms": latency,
            "answer_preview": answer[:100],
        })
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {case['id']:>2}. {case['question']:<18} path={path:<14} "
              f"命中{hit if hit else '无'} ({latency}ms)")

    total_time = time.monotonic() - start_all
    accuracy = passed / len(cases)
    print(f"\n=== 答案准确率: {passed}/{len(cases)} = {accuracy:.0%}（任务书要求>{ACCURACY_TARGET:.0%}） ===")
    print(f"总耗时: {total_time:.1f}秒")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "date": time.strftime("%Y-%m-%d"),
        "target": ">85%",
        "accuracy": round(accuracy, 4),
        "passed": passed,
        "total": len(cases),
        "scoring": "expected_keywords任一命中即通过（关键词取自知识库原文）",
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入: {REPORT}")

    return 0 if accuracy >= ACCURACY_TARGET else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
