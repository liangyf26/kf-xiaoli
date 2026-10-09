"""3种决策引擎准确率对比：汇总各引擎的accuracy_eval结果JSON，生成对比报告。

用法:
    python tests/compare_engine_accuracy.py tests/results/accuracy_eval_rule_*.json tests/results/accuracy_eval_jev_*.json tests/results/accuracy_eval_kev_*.json
    python tests/compare_engine_accuracy.py    # 无参数时自动按rule/jev/kev取最新结果

输出: tests/results/engine_accuracy_report_<时间戳>.md（含对比表与逐题差异明细）
"""
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
ENGINE_ORDER = ["rule", "jev", "kev"]
ENGINE_LABELS = {"rule": "规则引擎", "jev": "Jev API (OpenRouter)", "kev": "Kev本地模型(GPU)"}


def resolve_inputs(argv: list[str]) -> dict[str, Path]:
    """按引擎收集各自的最新accuracy_eval结果文件。"""
    if len(argv) >= 2:
        files = [Path(p) for p in argv[1:]]
    else:
        files = []
        for engine in ENGINE_ORDER:
            matches = sorted(RESULTS_DIR.glob(f"accuracy_eval_{engine}_*.json"))
            if matches:
                files.append(max(matches, key=lambda p: p.stat().st_mtime))
    by_engine: dict[str, Path] = {}
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        engine = data.get("metadata", {}).get("engine_env") or f.stem.split("_")[2]
        if engine not in by_engine or f.stat().st_mtime > by_engine[engine].stat().st_mtime:
            by_engine[engine] = f
    return by_engine


def fmt_pct(value: float) -> str:
    """小数比例转百分比字符串。"""
    return f"{value * 100:.1f}%"


def generate_report(runs: dict[str, dict], sources: dict[str, Path]) -> str:
    """汇总各引擎评估结果为Markdown对比报告。"""
    lines: list[str] = []
    lines.append("# 3种决策引擎准确率对比报告")
    lines.append("")
    lines.append(f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **评估集**: tests/accuracy_test_cases.json（10个标注用例，预期客观取自知识库）")
    lines.append(f"- **LLM生成模型**: {', '.join(sorted({r.get('metadata', {}).get('llm_model', '') for r in runs.values()}))}")
    lines.append("")

    lines.append("## 对比总表")
    lines.append("")
    lines.append("| 引擎 | 答案准确率 | 来源准确率 | 拒答准确率 | 澄清率 | 平均延迟 | 评估时间 |")
    lines.append("|---|---|---|---|---|---|---|")
    for engine in ENGINE_ORDER:
        if engine not in runs:
            continue
        m = runs[engine]["accuracy_metrics"]
        meta = runs[engine]["metadata"]
        lines.append(
            f"| {ENGINE_LABELS.get(engine, engine)} | {fmt_pct(m['answer_accuracy'])} "
            f"| {fmt_pct(m['source_accuracy'])}（{m['source_passed']}/{m['source_scored']}） "
            f"| {fmt_pct(m['refusal_accuracy'])}（{m['refusal_cases']}例） "
            f"| {fmt_pct(m.get('clarification_rate', 0.0))}（{m.get('clarified_cases', 0)}例） "
            f"| {m['average_latency_ms']}ms | {meta.get('timestamp', '')} |"
        )
    lines.append("")
    lines.append("> 口径说明：答案准确率为严格口径——澄清反问不计作答（澄清话术常罗列具体选项，会被关键词误判）。"
                 "决策引擎只影响意图识别与路由，答案文本均由同一LLM生成。")
    lines.append("")

    lines.append("## 逐题差异明细")
    lines.append("")
    for engine in ENGINE_ORDER:
        if engine not in runs:
            continue
        lines.append(f"### {ENGINE_LABELS.get(engine, engine)}（数据: `{sources[engine].name}`）")
        lines.append("")
        lines.append("| 问题 | 答案判定 | 来源判定 | 命中关键词 | 失败原因 |")
        lines.append("|---|---|---|---|---|")
        for d in runs[engine]["detailed_results"]:
            answer_mark = "✅" if d["answer_pass"] else "❌"
            source_mark = "✅" if d["source_pass"] else "❌"
            hit = "、".join(d.get("keyword_hit", [])) or "-"
            if d["answer_pass"]:
                reason = "-"
            elif d["should_answer"]:
                reason = "回复未命中任何预期关键词" + (f"（错误: {d['error']}）" if d.get("error") else "（疑似拒答）" if "暂时" in d.get("answer", "") else "")
            else:
                reason = "知识库外问题未正确拒答"
            lines.append(f"| {d['question'][:30]} | {answer_mark} | {source_mark} | {hit} | {reason} |")
        lines.append("")

    lines.append("## 结论说明")
    lines.append("")
    lines.append("- 决策引擎只影响意图识别与路由；答案文本均由同一LLM基于知识库生成，因此三引擎答案准确率趋同是预期现象。")
    lines.append("- 引擎差异体现在：意图判定的稳定性、延迟（规则<1ms / Jev约1s / Kev约1s）、以及模糊问题的澄清倾向（见逐题明细）。")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    sources = resolve_inputs(sys.argv)
    if not sources:
        raise SystemExit("未找到任何accuracy_eval结果文件，请先运行tests/accuracy_evaluation.py")
    runs = {engine: json.loads(path.read_text(encoding="utf-8")) for engine, path in sources.items()}
    report = generate_report(runs, sources)

    out_path = RESULTS_DIR / f"engine_accuracy_report_{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"对比报告已生成: {out_path}")
    print(f"覆盖引擎: {', '.join(sources.keys())}")


if __name__ == "__main__":
    main()
