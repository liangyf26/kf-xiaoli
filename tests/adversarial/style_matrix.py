"""从落盘 jsonl 重算「意图 × 风格」失分率矩阵（#057 任务书 4.4）。

只读 questions.jsonl（不读知识库、不联网）；失分率 = 失分题数 / 过质检题数，
被 verifier 剔除的题（verifier.pass=false）不进分母。输出 JSON 键排序、
浮点保留 4 位，保证同一组输入重算逐字节一致（任务书 A4）。

窗口口径（任务书 4.4 + 用户 2026-10-25 口径）：一局 = 一个 run_id；按 run_id
排序取最近 --window 局进聚合，并在 by_run 明细里逐局落账（供采样器再做
「最近 N 局」的二次窗口）。--window 默认 20（用户口径，覆盖任务书旧值 10）。

运行（仓库根）:
  python tests/adversarial/style_matrix.py --runs /tmp/sp057/run1 --window 20 --out matrix.json
"""
import argparse
import json
import sys
from pathlib import Path


def load_rows(run_dir: Path) -> list:
    """读一个 run 目录的 questions.jsonl（每行一题；空行跳过）。"""
    p = Path(run_dir) / "questions.jsonl"
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def compute(runs: list, window: int) -> dict:
    """聚合窗口内各局的 (intent, style_id) 失分率，返回矩阵 dict。"""
    by_run: dict = {}
    for run_dir in runs:
        for row in load_rows(run_dir):
            if not (row.get("verifier") or {}).get("pass"):
                continue                       # 被剔题不进分母（任务书 4.4）
            if (row.get("verdict") or {}).get("ok") is None:
                continue                       # UNJUDGEABLE（期望匹配不到）不进分母
            key = f"{row['intent']}|{row['style_id']}"
            acc = by_run.setdefault(row.get("run_id", Path(run_dir).name), {})
            cell = acc.setdefault(key, {"lost": 0, "total": 0, "simulated": False})
            cell["total"] += 1
            if (row.get("verdict") or {}).get("ok") is False:
                cell["lost"] += 1
            # dry-run 落盘的行带 simulated 标记（#057 二审 P1）：该格失分率是
            # 假应答算出来的，聚合时保留标记，采样器聚合窗口时整格跳过。
            if (row.get("verdict") or {}).get("simulated"):
                cell["simulated"] = True
    # 局序按 seed 数值（s10 在 s2 之后），字符串排序会取错最近局（#057 返工 P1）；
    # window<=0 时 [-0:] 取全部局，下限锁 1（#057 二审 P3）
    window_runs = sorted(by_run, key=lambda r: (0, int(r[1:])) if (
        isinstance(r, str) and r.startswith("s") and r[1:].isdigit()) else (1, str(r))
                         )[-max(1, int(window)):]
    per = {}
    for run in window_runs:
        for key, cell in by_run[run].items():
            acc = per.setdefault(key, {"lost": 0, "total": 0, "simulated": False})
            acc["lost"] += cell["lost"]
            acc["total"] += cell["total"]
            acc["simulated"] = acc["simulated"] or cell.get("simulated", False)
    out_per = {k: {"lost": v["lost"], "total": v["total"], "simulated": v["simulated"],
                   "rate": round(v["lost"] / v["total"], 4) if v["total"] else 0.0}
               for k, v in sorted(per.items())}
    return {"window": int(window), "runs": window_runs,
            "per_cell": out_per,
            "by_run": {r: by_run[r] for r in window_runs}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="意图×风格 失分率矩阵重算（#057）")
    ap.add_argument("--runs", nargs="+", required=True, help="run 目录（questions.jsonl 所在）")
    ap.add_argument("--window", type=int, default=20,
                    help="滑动窗口局数（默认 20，用户 2026-10-25 口径）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    matrix = compute(args.runs, args.window)
    Path(args.out).write_text(
        json.dumps(matrix, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    print(f"[style_matrix] runs={len(args.runs)} 窗口内局={len(matrix['runs'])} "
          f"格数={len(matrix['per_cell'])} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
