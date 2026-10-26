"""回归例行脚本（#061）：换库 / 改提示词 / 改话术之后，一键复跑考卷并和上次比。

判定逻辑**复用** tests/accuracy_evaluation.py（同一套三分口径与动作标记），本脚本
不做任何新的判定，只负责：跑一轮 → 落盘 → 找上一次 regression 结果 → 算 delta。

用法（kf-xiaoli 仓根目录）：
    .venv/bin/python scripts/run_regression.py --engine rule          # 规则引擎
    .venv/bin/python scripts/run_regression.py --engine llm --tag 话术拆分
    --engine rule : DECISION_ENGINE=rule（决策走规则，生成仍按 .env 的 LLM 渠道）
    --engine llm  : 不动环境变量，按 .env 里配置的决策引擎（qwen/jev/kev 任一）
    --tag         : 可选，写进文件名与 metadata，方便标记「这次改了什么」

结果落 tests/results/regression_<YYYYmmdd-HHMM>[_<tag>].json：
    - 三分指标（action_accuracy / answer_accuracy / handoff_accuracy）+ 各自分子分母
    - refuse_observed（观察项，不进主分母）
    - 逐条明细（detailed_results）+ 跳过清单（skipped_cases）
    - labeled.json 的 sha256 + 当前代码 git rev（溯源：这次考卷考的是哪份数据哪版代码）

对照：自动找上一次 regression_*.json 算 delta（三个指标 + refuse_observed 命中率），
打印成对照表，回退的行标 ⚠。分母 <100 时明示「小样本，一题约 100/分母 分，不设门禁」
——26 条可考核时一题就是 3.85 分，单题波动足以淹没趋势，所以只看方向不设红线。

边界：不改判定口径、不改 labeled.json、不改知识库；只写 tests/results/ 下的新文件。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests import accuracy_evaluation as ae  # noqa: E402  判定逻辑唯一来源（属性调用，测试可替换 Orchestrator）

DEFAULT_LABELED = PROJECT_ROOT / "data" / "wx_real" / "labeled.json"
RESULTS_DIR = PROJECT_ROOT / "tests" / "results"

# 参与对照的指标：三个主指标 + 观察项命中率（观察项只看方向，不设门禁）
DELTA_KEYS = ("action_accuracy", "answer_accuracy", "handoff_accuracy")
OBSERVED_KEY = "refuse_observed"
SAMPLE_DENOMINATOR_LIMIT = 100


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_rev() -> str:
    """当前代码版本（short rev）；不在 git 仓库/无 git 时给 unknown，不让脚本崩。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def find_latest_baseline(results_dir: Path, exclude: Path | None = None) -> Path | None:
    """上一次 regression 结果（按文件名时间戳排序取最新）；没有/全部无效 → None。"""
    candidates = sorted(results_dir.glob("regression_*.json"))
    if exclude is not None:
        candidates = [c for c in candidates if c.resolve() != Path(exclude).resolve()]
    for path in reversed(candidates):   # 新的在前
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload.get("metrics"), dict) and "action_accuracy" in payload["metrics"]:
            return path
    return None


def _direction(delta: float) -> str:
    if delta > 0:
        return "improved"
    if delta < 0:
        return "regressed"
    return "same"


def build_delta(prev_payload: dict | None, curr_metrics: dict,
                curr_refuse: dict, labeled_sha: str) -> dict | None:
    """对照上一次结果算 delta；无基线返回 None（首跑不是错误）。"""
    if not prev_payload:
        return None
    prev_metrics = prev_payload.get("metrics", {})
    prev_refuse = prev_payload.get("refuse_observed", {})
    pairs = {
        key: (prev_metrics.get(key), curr_metrics.get(key))
        for key in DELTA_KEYS
    }
    pairs[f"{OBSERVED_KEY}.rate"] = (
        prev_refuse.get("rate"), curr_refuse.get("rate")
    )
    metrics: dict = {}
    for name, (prev, curr) in pairs.items():
        prev = prev if isinstance(prev, (int, float)) else None
        curr = curr if isinstance(curr, (int, float)) else None
        if prev is None or curr is None:
            metrics[name] = {"prev": prev, "curr": curr, "delta": None,
                             "direction": "unknown"}
            continue
        diff = round(curr - prev, 4)
        metrics[name] = {"prev": prev, "curr": curr, "delta": diff,
                         "direction": _direction(diff)}
    return {
        "baseline": prev_payload.get("metadata", {}).get("result_file"),
        "baseline_git_rev": prev_payload.get("metadata", {}).get("git_rev"),
        "labeled_sha_changed": prev_payload.get("metadata", {}).get("labeled_sha256") != labeled_sha,
        "metrics": metrics,
    }


def render_table(payload: dict) -> str:
    """对照表（纯文本，方便贴进报告/飞书）。"""
    lines: list[str] = []
    delta = payload.get("delta")
    if not delta:
        lines.append("无历史基线（首跑），本次结果即为 baseline。")
    else:
        header = f"{'指标':<22}{'上次':>9}{'本次':>9}{'Δ':>10}  判定"
        lines.append(header)
        for name, item in delta["metrics"].items():
            prev = "-" if item["prev"] is None else f"{item['prev']:.4f}"
            curr = "-" if item["curr"] is None else f"{item['curr']:.4f}"
            diff = "-" if item["delta"] is None else f"{item['delta']:+.4f}"
            mark = {"improved": "↑ 改善", "regressed": "⚠ 回退",
                    "same": "→ 持平", "unknown": "? 无对比"}[item["direction"]]
            lines.append(f"{name:<22}{prev:>9}{curr:>9}{diff:>10}  {mark}")
        if delta.get("labeled_sha_changed"):
            lines.append("⚠ labeled.json 与基线不是同一份数据（sha256 变了），"
                         "指标对比掺入了考卷本身的差异。")
    warn = payload.get("sample_warning")
    if warn:
        lines.append(
            f"小样本：主指标分母 {warn['denominator']} < {SAMPLE_DENOMINATOR_LIMIT}，"
            f"一题 ≈{warn['per_question_points']} 分，波动大，不设门禁。"
        )
    return "\n".join(lines)


def run_regression(labeled_path: str | Path = DEFAULT_LABELED, engine: str = "rule",
                   tag: str | None = None, results_dir: Path = RESULTS_DIR,
                   now: datetime | None = None) -> dict:
    """跑一轮回归并落盘，返回 regression payload（含 delta）。"""
    labeled_path = Path(labeled_path)
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if engine == "rule":
        os.environ["DECISION_ENGINE"] = "rule"
    elif engine == "llm":
        os.environ.pop("DECISION_ENGINE", None)   # 交给 .env 决定（qwen/jev/kev）
    else:
        raise ValueError(f"engine 只支持 rule|llm，实际「{engine}」")

    eval_path = asyncio.run(ae.run_evaluation(labeled_path))   # 判定口径完全来自 accuracy_evaluation
    eval_payload = json.loads(Path(eval_path).read_text(encoding="utf-8"))
    metrics = eval_payload["accuracy_metrics"]

    labeled_sha = sha256_of(labeled_path)
    prev = find_latest_baseline(results_dir)
    prev_payload = json.loads(prev.read_text(encoding="utf-8")) if prev else None
    delta = build_delta(prev_payload, metrics, metrics.get(OBSERVED_KEY, {}),
                        labeled_sha)

    denominator = metrics.get("total_cases", 0)
    payload: dict = {
        "metadata": {
            "task": "#061 回归例行",
            "engine": engine,
            "engine_class": eval_payload["metadata"].get("engine_class"),
            "tag": tag or "",
            "labeled_path": str(labeled_path),
            "labeled_sha256": labeled_sha,
            "git_rev": git_rev(),
            "created": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
            "source_eval": Path(eval_path).name,
            "result_file": "",   # 落盘后回填，delta 里引用的是它
        },
        "metrics": metrics,
        "refuse_observed": metrics.get(OBSERVED_KEY, {}),
        "skipped_cases": eval_payload.get("skipped_cases", []),
        "detailed_results": eval_payload.get("detailed_results", []),
        "delta": delta,
        "sample_warning": None,
    }
    if 0 < denominator < SAMPLE_DENOMINATOR_LIMIT:
        payload["sample_warning"] = {
            "denominator": denominator,
            "per_question_points": round(100 / denominator, 2),
            "note": "小样本，一题分数波动大，不设门禁，只看方向",
        }

    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    name = f"regression_{stamp}_{tag}.json" if tag else f"regression_{stamp}.json"
    out_path = results_dir / name
    bump = 1
    while out_path.exists():                     # 同一分钟重跑不覆盖旧结果
        bump += 1
        name = f"regression_{stamp}_{tag}_{bump}.json" if tag \
            else f"regression_{stamp}_{bump}.json"
        out_path = results_dir / name
    payload["metadata"]["result_file"] = out_path.name
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print(f"\n回归结果: {out_path}")
    print(f"数据指纹: labeled.json sha256={labeled_sha[:12]}… · git {payload['metadata']['git_rev']} · engine={engine}"
          + (f" · tag={tag}" if tag else ""))
    print(render_table(payload))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#061 回归例行：复跑考卷并与上次对照")
    parser.add_argument("--engine", choices=["rule", "llm"], default="rule",
                        help="rule=规则决策；llm=按 .env 配置的决策引擎（默认 rule）")
    parser.add_argument("--tag", default=None, help="可选标记，写进文件名与 metadata")
    parser.add_argument("--labeled", default=str(DEFAULT_LABELED), help="考卷路径")
    parser.add_argument("--results-dir", default=str(RESULTS_DIR), help="回归结果目录")
    args = parser.parse_args(argv)

    labeled_path = Path(args.labeled)
    if not labeled_path.is_absolute():
        labeled_path = PROJECT_ROOT / labeled_path
    if not labeled_path.exists():
        print(f"考卷不存在: {labeled_path}", file=sys.stderr)
        return 2

    run_regression(labeled_path, engine=args.engine, tag=args.tag,
                   results_dir=Path(args.results_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
