"""3种决策引擎对比测试脚本：相同问题分别经rule/Jev/Kev决策，输出对比表格与JSON。

运行方式（使用项目虚拟环境，在项目根目录）:
    python tests/compare_engines.py [问题文件路径]
    默认问题文件: tests/test_questions_phase2.txt（每行一个问题）

输出:
    - 控制台对比表格
    - engine_comparison.json: {"results": [{"question", "engines": {...}}], "summary"}

说明:
    - 未配置真实JEV_API_KEY时跳过Jev真实调用（任务书决策3），记录skipped原因
    - Kev模型不可用（依赖未装/模型无法下载）时记录kev_failed回退结果
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.decision_layer import create_decision_engine  # noqa: E402
from backend.decision_layer.jev_client import JevEngine  # noqa: E402

OUTPUT_PATH = ROOT / "engine_comparison.json"
PLACEHOLDER_KEYS = ("", "your-jev-api-key-here")


def load_questions(path: Path) -> list[str]:
    questions = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not questions:
        raise ValueError(f"问题文件为空: {path}")
    return questions


def result_to_dict(result) -> dict:
    return {
        "intent": result.intent,
        "intent_confidence": round(result.intent_confidence, 3),
        "needs_clarification": result.needs_clarification,
        "user_emotion": result.user_emotion,
        "technical_complexity": result.technical_complexity,
        "escalate_to_human": result.escalate_to_human,
        "latency_ms": result.latency_ms,
        "engine": result.engine,
    }


async def decide_with_timing(engine, question: str) -> tuple[dict, float]:
    start = time.monotonic()
    result = await engine.decide(question, {})
    return result_to_dict(result), (time.monotonic() - start) * 1000


async def main() -> int:
    questions_file = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "test_questions_phase2.txt"
    questions = load_questions(questions_file)

    rule_engine = create_decision_engine("rule")
    kev_engine = create_decision_engine("kev")

    jev_skipped_reason = ""
    if settings.JEV_API_KEY in PLACEHOLDER_KEYS:
        jev_skipped_reason = "未配置真实JEV_API_KEY，按任务书决策3跳过真实调用"
    else:
        jev_engine = JevEngine(settings.JEV_API_KEY)

    results = []
    for question in questions:
        engines: dict = {}

        rule_result, _ = await decide_with_timing(rule_engine, question)
        engines["rule"] = rule_result

        if jev_skipped_reason:
            engines["jev"] = {"skipped": True, "reason": jev_skipped_reason}
        else:
            jev_result, _ = await decide_with_timing(jev_engine, question)
            engines["jev"] = jev_result

        kev_result, _ = await decide_with_timing(kev_engine, question)
        engines["kev"] = kev_result

        results.append({"question": question, "engines": engines})
        print(
            f"{question:<12} | rule: {rule_result['intent']:<18}({rule_result['latency_ms']}ms)"
            f" | jev: {engines['jev'].get('intent', 'skipped'):<18}"
            f" | kev: {kev_result['intent']:<18}({kev_result['latency_ms']}ms, {kev_result['engine']})"
        )

    rule_latencies = [r["engines"]["rule"]["latency_ms"] for r in results]
    kev_real = [r for r in results if r["engines"]["kev"].get("engine") == "kev"]
    kev_latencies = [r["engines"]["kev"]["latency_ms"] for r in kev_real]
    summary = {
        "question_count": len(results),
        "rule_avg_latency_ms": round(sum(rule_latencies) / len(rule_latencies), 2),
        "rule_max_latency_ms": max(rule_latencies),
        "jev_skipped": bool(jev_skipped_reason),
        "kev_available": bool(kev_real),
        "kev_avg_latency_ms": round(sum(kev_latencies) / len(kev_latencies), 2) if kev_latencies else None,
        "note": (
            "Kev经本地kev.serve推理"
            if kev_real
            else "Kev模型不可用（服务未启动或依赖缺失），记录为kev_failed回退结果"
        ),
    }
    output = {"summary": summary, "results": results}
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n规则引擎平均延迟: {summary['rule_avg_latency_ms']}ms（要求<100ms）")
    print(f"对比结果已写入: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
