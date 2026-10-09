"""测试报告生成器：读取批量测试结果JSON，生成Markdown格式测试报告。

用法:
    python tests/generate_report.py tests/results/batch_test_20261009-120000.json
    python tests/generate_report.py tests/results/batch_test_*.json   # 取最新一份

输出: tests/results/test_report_<来源时间戳>.md，包含测试概览、引擎/路径统计、
详细结果表格与失败案例列表。
"""
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "tests" / "results"


def resolve_input(argv: list[str]) -> Path:
    """解析输入文件：支持具体路径或glob（取最新），无参数时取tests/results下最新批次结果。"""
    if len(argv) >= 2:
        pattern = argv[1]
        candidates = sorted(RESULTS_DIR.glob(pattern)) if not Path(pattern).is_absolute() else [Path(pattern)]
        if not candidates and "*" not in pattern and "?" not in pattern:
            candidates = [Path(pattern)]
    else:
        candidates = sorted(RESULTS_DIR.glob("batch_test_*.json"))
    if not candidates:
        raise SystemExit(f"未找到批量测试结果文件（查找: {argv[1] if len(argv) >= 2 else 'tests/results/batch_test_*.json'}）")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def fmt_ms(ms: float) -> str:
    """毫秒数格式化：≥1000显示秒，否则显示毫秒。"""
    return f"{ms / 1000:.2f}s" if ms >= 1000 else f"{ms:.0f}ms"


def group_stats(results: list[dict], key: str) -> list[tuple[str, int, float]]:
    """按指定字段分组统计：[(取值, 数量, 平均延迟ms)]，按数量降序。"""
    groups: dict[str, list[int]] = {}
    for r in results:
        groups.setdefault(r.get(key) or "(空)", []).append(r.get("latency_ms", 0))
    return sorted(
        ((name, len(lats), sum(lats) / len(lats)) for name, lats in groups.items()),
        key=lambda x: -x[1],
    )


def generate_report(data: dict, source_path: Path) -> str:
    """由批次结果dict构造Markdown报告全文。"""
    meta = data.get("metadata", {})
    results = data.get("results", [])
    total = meta.get("total_questions", len(results))
    passed = meta.get("passed", sum(1 for r in results if r.get("status") == "ok"))
    pass_rate = meta.get("pass_rate", (passed / total) if total else 0.0)
    avg = meta.get("average_latency_ms", 0)
    lines: list[str] = []

    lines.append("# 测试报告")
    lines.append("")
    lines.append(f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **数据来源**: `{source_path.name}`（{meta.get('timestamp', '')}）")
    lines.append(f"- **决策引擎**: {meta.get('engine_class', '')}（环境变量: {meta.get('engine_env') or '读取.env'}）")
    lines.append(f"- **LLM模型**: {meta.get('llm_model', '')}")
    lines.append("")

    lines.append("## 测试概览")
    lines.append("")
    lines.append(f"- 测试问题总数: **{total}**")
    lines.append(f"- 正常回复: **{passed}**（通过率 **{pass_rate * 100:.1f}%**，目标≥90%）")
    lines.append(f"- 异常/失败: {total - passed}")
    lines.append(f"- 平均响应时间: **{fmt_ms(avg)}**（目标<10秒）")
    lines.append(f"- 最大响应时间: {fmt_ms(meta.get('max_latency_ms', 0))} / 最小: {fmt_ms(meta.get('min_latency_ms', 0))}")
    clarifications = sum(1 for r in results if r.get("need_clarification"))
    refusals = sum(1 for r in results if r.get("is_refusal"))
    lines.append(f"- 触发澄清: {clarifications} 题 / 标准拒答: {refusals} 题")
    lines.append("")

    for title, key in (("## 各决策引擎统计", "engine"), ("## 各处理路径统计", "path")):
        lines.append(title)
        lines.append("")
        lines.append("| 取值 | 题数 | 平均延迟 |")
        lines.append("|---|---|---|")
        for name, count, mean in group_stats(results, key):
            lines.append(f"| {name} | {count} | {fmt_ms(mean)} |")
        lines.append("")

    lines.append("## 意图分布")
    lines.append("")
    lines.append("| 意图 | 题数 |")
    lines.append("|---|---|")
    for name, count in Counter(r.get("intent") or "(空)" for r in results).most_common():
        lines.append(f"| {name} | {count} |")
    lines.append("")

    lines.append("## 详细测试结果")
    lines.append("")
    lines.append("| # | 用户输入 | 路径 | 意图 | 引擎 | 延迟 | 回复摘要 |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        summary = r.get("answer", "").replace("\n", " ").replace("|", "\\|")
        summary = summary[:60] + ("…" if len(summary) > 60 else "")
        lines.append(
            f"| {i} | {r.get('question', '')[:40]} | {r.get('path', '')} | {r.get('intent', '')} "
            f"| {r.get('engine', '')} | {fmt_ms(r.get('latency_ms', 0))} | {summary} |"
        )
    lines.append("")

    failures = [r for r in results if r.get("status") != "ok"]
    lines.append("## 失败/异常案例")
    lines.append("")
    if not failures:
        lines.append("无。全部问题均得到正常回复。")
    else:
        for r in failures:
            lines.append(f"- **{r.get('question')}**: {r.get('error', '空回复')}")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    source = resolve_input(sys.argv)
    data = json.loads(source.read_text(encoding="utf-8"))
    report = generate_report(data, source)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = RESULTS_DIR / f"test_report_{stamp}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"报告已生成: {out_path}（{len(report)}字符）")


if __name__ == "__main__":
    main()
