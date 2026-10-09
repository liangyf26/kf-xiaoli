"""性能分析脚本：对编排流程各组件埋点计时，定位性能瓶颈。

用法:
    python tests/performance_profile.py [--repeats 2]

分析组件:
    decision      决策层（规则/Jev/Kev意图识别）
    kb_retrieval  知识库检索（按意图+相关度排序）
    prompt_build  Prompt组装（含裁剪）
    llm_generate  LLM推理调用（Qwen chat/completions）
    json_parse    LLM回复JSON解析

方法: 运行时包装Orchestrator内部方法累计耗时（不改动后端代码），
对固定代表问题集逐题走完整编排流程。输出tests/results/performance_profile_*.json，
含average_times、瓶颈组件与优化建议。
"""
import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.orchestrator import handlers as handlers_module  # noqa: E402
from backend.orchestrator.orchestrator import Orchestrator  # noqa: E402

RESULTS_DIR = PROJECT_ROOT / "tests" / "results"

# 代表问题：覆盖价格LLM路径、FAQ直连路径、技术支持、故障排查
PROFILE_QUESTIONS = [
    "直播线路多少钱",
    "IDC线路多少钱",
    "tiktok登不上怎么办",
    "看视频有点卡怎么办",
]


class Profiler:
    """组件耗时采集器：包装同步/异步方法，按组件名累计耗时样本。"""

    def __init__(self) -> None:
        self.times: dict[str, list[float]] = defaultdict(list)

    def wrap_async(self, obj, attr: str, component: str) -> None:
        """包装异步方法，记录每次调用的耗时（毫秒）。"""
        original = getattr(obj, attr)

        async def timed(*args, **kwargs):
            start = time.monotonic()
            try:
                return await original(*args, **kwargs)
            finally:
                self.times[component].append((time.monotonic() - start) * 1000)

        setattr(obj, attr, timed)

    def wrap_sync(self, obj, attr: str, component: str) -> None:
        """包装同步方法，记录每次调用的耗时（毫秒）。"""
        original = getattr(obj, attr)

        def timed(*args, **kwargs):
            start = time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                self.times[component].append((time.monotonic() - start) * 1000)

        setattr(obj, attr, timed)

    def install(self, orchestrator: Orchestrator) -> None:
        """对编排流程的5个组件安装计时包装。"""
        self.wrap_async(orchestrator.decision_engine, "decide", "decision")
        self.wrap_sync(orchestrator.knowledge_base, "get_by_intent", "kb_retrieval")
        self.wrap_sync(orchestrator.knowledge_base, "search_all", "kb_retrieval")
        self.wrap_sync(orchestrator.prompt_builder, "build_prompt", "prompt_build")
        self.wrap_async(orchestrator.llm_client, "generate", "llm_generate")
        self.wrap_sync(handlers_module, "parse_json_response", "json_parse")

    def average_times(self) -> dict[str, float]:
        """各组件平均耗时（毫秒，保留1位小数）。"""
        return {name: round(sum(v) / len(v), 1) for name, v in sorted(self.times.items())}


async def run_profile(repeats: int) -> dict:
    """对代表问题重复运行编排流程，输出性能画像dict。"""
    orchestrator = Orchestrator()
    profiler = Profiler()
    profiler.install(orchestrator)

    per_question: list[dict] = []
    for i in range(repeats):
        for question in PROFILE_QUESTIONS:
            start = time.monotonic()
            result = await orchestrator.process(question, {})
            wall_ms = (time.monotonic() - start) * 1000
            per_question.append({
                "question": question,
                "repeat": i + 1,
                "path": result.get("path", ""),
                "wall_ms": round(wall_ms, 1),
            })
            print(f"[{i + 1}/{repeats}] {question} -> {result.get('path')} {wall_ms:.0f}ms")

    averages = profiler.average_times()
    bottleneck = max(averages.items(), key=lambda x: x[1]) if averages else ("(无样本)", 0.0)
    suggestion = ""
    if bottleneck[1] > 5000:
        suggestion = "瓶颈为LLM推理：建议控制Prompt长度（知识库只取相关度Top-N）、减少重试；模型侧优化（量化/更快服务）超出Demo低成本范围"
    elif bottleneck[1] > 1000:
        suggestion = "考虑优化该组件（缓存/并发/减少调用次数）"
    else:
        suggestion = "各组件耗时均在合理范围"

    return {
        "metadata": {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "engine_env": os.environ.get("DECISION_ENGINE", ""),
            "engine_class": type(orchestrator.decision_engine).__name__,
            "llm_model": orchestrator.llm_client.model,
            "questions": PROFILE_QUESTIONS,
            "repeats": repeats,
            "bottleneck": bottleneck[0],
            "bottleneck_avg_ms": bottleneck[1],
            "suggestion": suggestion,
        },
        "average_times": averages,
        "per_question": per_question,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="SDWAN客服性能分析")
    parser.add_argument("--repeats", type=int, default=2, help="每个问题的重复次数（默认2）")
    args = parser.parse_args()

    profile = asyncio.run(run_profile(args.repeats))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"performance_profile_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n性能分析结果:")
    for component, time_ms in profile["average_times"].items():
        print(f"  {component}: {time_ms:.0f}ms")
    meta = profile["metadata"]
    print(f"\n性能瓶颈: {meta['bottleneck']} ({meta['bottleneck_avg_ms']:.0f}ms)")
    print(f"建议: {meta['suggestion']}")
    print(f"结果文件: {out_path}")


if __name__ == "__main__":
    main()
