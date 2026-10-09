"""批量测试脚本：读取测试问题文件，逐题经完整编排流程（决策→路由→生成）并记录结果。

用法:
    python tests/batch_test.py tests/test_questions_final.txt [--engine rule|jev|kev]

说明:
- 问题文件格式：每行一个问题；空行分隔不同会话，同一会话内的连续问题共享多轮上下文。
- 延迟口径：编排器处理耗时（决策→路由→生成），不含15秒等待汇总窗口（该窗口是产品设计，固定叠加）。
- 决策引擎可通过 --engine 参数或环境变量 DECISION_ENGINE 指定（默认读 .env 配置）。
- 输出JSON到 tests/results/batch_test_YYYYMMDD-HHMMSS.json，供 tests/generate_report.py 生成报告。
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


def parse_question_file(path: Path) -> list[list[str]]:
    """解析问题文件：每行一个问题，空行分隔会话；返回会话列表（每个会话是问题列表）。"""
    sessions: list[list[str]] = []
    current: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line:
            current.append(line)
        elif current:
            sessions.append(current)
            current = []
    if current:
        sessions.append(current)
    return sessions


async def run_session(orchestrator: Orchestrator, questions: list[str], results: list[dict]) -> None:
    """运行一个会话：会话内问题共享多轮上下文，逐题记录完整结果。"""
    context: dict = {"history": [], "previous_intent": None, "clarification_count": 0, "covered_topics": []}
    for question in questions:
        start = time.monotonic()
        status = "ok"
        error = ""
        try:
            result = await orchestrator.process(question, context)
        except Exception as exc:  # noqa: BLE001 单题失败不中断批次
            result = {"answer": "", "sources": [], "path": "error", "intent": "", "engine": "", "need_clarification": False}
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.monotonic() - start) * 1000)

        answer = result.get("answer", "")
        results.append({
            "question": question,
            "answer": answer,
            "sources": result.get("sources", []),
            "latency_ms": latency_ms,
            "engine": result.get("engine", ""),
            "path": result.get("path", ""),
            "intent": result.get("intent", ""),
            "need_clarification": bool(result.get("need_clarification")),
            "status": status,
            "is_refusal": "暂时无法回答" in answer or "人工" in answer[:20],
            "error": error,
        })

        # 与 main.py 相同的上下文更新逻辑，保证多轮行为与线上一致
        context["history"].append({"role": "user", "content": question})
        context["history"].append({"role": "assistant", "content": answer})
        context["history"] = context["history"][-10:]
        context["previous_intent"] = result.get("intent", "") or None
        if result.get("need_clarification"):
            context["clarification_count"] += 1
        for source in result.get("sources", []):
            if source not in context["covered_topics"]:
                context["covered_topics"].append(source)


async def run_batch(question_file: Path) -> Path:
    """执行批量测试并写出结果JSON，返回结果文件路径。"""
    sessions = parse_question_file(question_file)
    total_questions = sum(len(s) for s in sessions)
    print(f"加载 {len(sessions)} 个会话 / {total_questions} 个问题，决策引擎环境: {os.environ.get('DECISION_ENGINE', '(读取.env)')}")

    orchestrator = Orchestrator()
    engine_name = type(orchestrator.decision_engine).__name__

    results: list[dict] = []
    batch_start = time.monotonic()
    for i, session in enumerate(sessions, 1):
        print(f"[会话 {i}/{len(sessions)}] {' / '.join(session)}")
        await run_session(orchestrator, session, results)
    wall_seconds = round(time.monotonic() - batch_start, 2)

    ok_results = [r for r in results if r["status"] == "ok"]
    latencies = [r["latency_ms"] for r in results]
    metadata = {
        "source_file": str(question_file.name),
        "engine_env": os.environ.get("DECISION_ENGINE", ""),
        "engine_class": engine_name,
        "llm_model": orchestrator.llm_client.model,
        "total_questions": len(results),
        "passed": len(ok_results),
        "failed": len(results) - len(ok_results),
        "pass_rate": round(len(ok_results) / len(results), 4) if results else 0.0,
        "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "max_latency_ms": max(latencies) if latencies else 0,
        "min_latency_ms": min(latencies) if latencies else 0,
        "wall_seconds": wall_seconds,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"batch_test_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(
        json.dumps({"metadata": metadata, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n完成: {metadata['passed']}/{metadata['total_questions']} 正常回复, "
          f"平均延迟 {metadata['average_latency_ms']}ms, 墙钟 {wall_seconds}s")
    print(f"结果文件: {out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="SDWAN客服批量测试")
    parser.add_argument("question_file", nargs="?", default="tests/test_questions_final.txt",
                        help="测试问题文件（每行一个问题，空行分隔会话）")
    parser.add_argument("--engine", choices=["rule", "jev", "kev", "qwen"], default=None,
                        help="指定决策引擎（默认读环境变量/.env）")
    args = parser.parse_args()

    if args.engine:
        os.environ["DECISION_ENGINE"] = args.engine

    qf = Path(args.question_file)
    if not qf.is_absolute():
        qf = PROJECT_ROOT / qf
    if not qf.exists():
        print(f"问题文件不存在: {qf}")
        sys.exit(1)

    asyncio.run(run_batch(qf))


if __name__ == "__main__":
    main()
