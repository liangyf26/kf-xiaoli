"""#061 对局日志：落盘、对账、只读重算。

目录 runs/adversarial/<round_id>/：
- meta.json：commit、脱敏配置、双方端点、库副本 sha256
- game_<k>/turns.jsonl：逐轮一行
- game_<k>/kb_changes.jsonl：本局登记的缺口（不当局生效）
- summary.json：期望题数 = 结果数 = 有效 + 无效，不对账则 exit 3
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path

RUNS_ROOT = Path("runs/adversarial")
SECRET_KEYS = ("key", "token", "secret", "password")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def redact(value):
    """配置里键名含 key/token/secret/password 的值替换成 <redacted>。"""
    if isinstance(value, dict):
        return {k: ("<redacted>" if any(s in k.lower() for s in SECRET_KEYS) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def new_round(root: Path, config: dict, kb_copy: Path | None) -> Path:
    """建一轮目录并写 meta.json。返回轮目录。"""
    round_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = root / round_id
    while base.exists():                       # 同一秒内再开一轮时加后缀
        round_id += "a"
        base = root / round_id
    (base).mkdir(parents=True)
    meta = {
        "round_id": base.name,
        "git_commit": git_commit(),
        "config": redact(config),
        "endpoints": {"red": redact(config.get("red", {})), "blue": redact(config.get("blue", {}))},
        "models": {"red": (config.get("red") or {}).get("model", ""),
                   "blue": (config.get("blue") or {}).get("model", "")},
        "temperature": {"red": (config.get("red") or {}).get("temperature"),
                        "blue": (config.get("blue") or {}).get("temperature")},
        "seed": (config.get("match") or {}).get("seed"),
        "kb_copy_sha256": _sha256(kb_copy) if kb_copy and kb_copy.exists() else "",
        "style_card_version": config.get("style_card_version", ""),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (base / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    return base


def game_dir(round_dir: Path, game: int) -> Path:
    d = round_dir / f"game_{game}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize(games: list[dict]) -> dict:
    """由各局的 turns 重算汇总。games: [{game, expected, turns}]。"""
    per_game = []
    for g in games:
        turns = g["turns"]
        invalid = [t for t in turns if t.get("invalid")]
        per_game.append({
            "game": g["game"],
            "expected": g["expected"],
            "results": len(turns),
            "valid": len(turns) - len(invalid),
            "invalid": len(invalid),
            "stop": g.get("stop", "questions_done"),
            # race 局 expected=实际题数（target/limit 动态），只要不是断粮中断即完整；
            # fixed 局 expected=配置题数，短了照旧不对账
            "balanced": (g["expected"] == len(turns)
                         and g.get("stop", "questions_done") != "poses_exhausted"),
        })
    balanced = all(g["balanced"] for g in per_game) and bool(per_game)
    return {"games": per_game, "balanced": balanced,
            "invalid_total": sum(g["invalid"] for g in per_game),
            "results_total": sum(g["results"] for g in per_game)}


def write_summary(round_dir: Path, summary: dict) -> Path:
    path = round_dir / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _p90(values: list) -> float:
    """nearest-rank P90；空集返回 0。"""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def write_report(round_dir: Path, summary: dict, games: list[dict]) -> Path:
    """round_report.md：无效样本率、红方作答耗时（均值/P90）、能判题占比、比分与胜负分布。

    所有数字都从 games（=已落盘 turns.jsonl 的同一批记录）现算，可用 --recompute 对账。
    """
    turns = [t for g in games for t in g["turns"]]
    invalid = [t for t in turns if t.get("invalid")]
    answered = [t for t in turns if not t.get("invalid") and "answer" in t]
    latencies = [t["latency_ms"] for t in answered if t.get("latency_ms") is not None]
    judged_turns = [t for t in answered if isinstance(t.get("classification"), dict)]
    judged_ok = [t for t in judged_turns if t["classification"].get("scored")]
    blue_total = sum(t.get("blue_point", 0) for t in turns)
    red_total = sum(t.get("red_point", 0) for t in turns)
    blue_wins = sum(1 for t in turns if t.get("blue_point", 0) > 0)
    red_wins = sum(1 for t in turns if t.get("red_point", 0) > 0)
    no_score = sum(1 for t in turns if not t.get("invalid")
                   and t.get("blue_point", 0) == 0 and t.get("red_point", 0) == 0)
    # #062 第三节：陷阱题栏（出题时标注 trap 的题）与 forbid_hit 数
    def _cls(t: dict) -> dict:
        c = t.get("classification")
        return c if isinstance(c, dict) else {}

    trap_turns = [t for t in turns if not t.get("invalid") and _cls(t).get("trap")]
    trap_defended = sum(1 for t in trap_turns if _cls(t).get("verdict") == "trap_defended")
    forbid_hits = [t for t in turns if _cls(t).get("verdict") == "forbid_hit"]

    def pct(part: int, whole: int) -> str:
        return f"{part / whole * 100:.1f}%" if whole else "n/a"

    avg = round(sum(latencies) / len(latencies)) if latencies else 0
    meta = {}
    meta_path = round_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    match_cfg = (meta.get("config") or {}).get("match") or {}
    lines = [
        f"# #061 对局报告：{round_dir.name}",
        "",
        f"- 生成：{datetime.now().isoformat(timespec='seconds')}",
        f"- 配置：mode={match_cfg.get('mode', '')} questions={match_cfg.get('questions', '')}"
        f" target_score={match_cfg.get('target_score', '')} seed={match_cfg.get('seed', '')}"
        "（commit/端点/温度/kb 副本 sha256 见 meta.json）",
        f"- 数据来源：game_*/turns.jsonl（逐题）、summary.json、outbox.jsonl（直播）",
        f"- 复算：`.venv/bin/python -m tests.adversarial.match_log --recompute {round_dir}`",
        "",
        "## 三项指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 无效样本率 | {len(invalid)}/{len(turns)}（{pct(len(invalid), len(turns))}） |",
        f"| 红方作答耗时 | 平均 {avg}ms，P90（nearest-rank）{round(_p90(latencies))}ms"
        f"（n={len(latencies)}，第 ⌈0.9·N⌉ 个值按升序从1计，取重试后成功那次；无效题不计入） |",
        f"| 能判题占比 | {len(judged_ok)}/{len(judged_turns)}"
        f"（{pct(len(judged_ok), len(judged_turns))}，verdict ∈ no_kb/red_scored/blue_scored） |",
        "",
        "## 比分与胜负分布",
        "",
        f"- 比分：红方 {red_total} / 蓝方 {blue_total}（target_score={match_cfg.get('target_score', '')}）",
        f"- 胜负分布：蓝方得分 {blue_wins}、红方得分 {red_wins}、双方 0 分 {no_score}"
        f"、无效 {len(invalid)}（合计 {len(turns)} 题）",
        "",
        "## 陷阱题（#062 第三节，出题时标注）",
        "",
        f"- 陷阱题数：{len(trap_turns)}"
        + (f"（seq {[t.get('seq') for t in trap_turns]}）" if trap_turns else ""),
        f"- 红方正确应对（trap_defended）：{trap_defended}",
        f"- forbid_hit（绝对承诺用语，含库内原文）：{len(forbid_hits)}"
        + (f"（seq {[t.get('seq') for t in forbid_hits]}）" if forbid_hits else ""),
        "",
        "## 逐题一览",
        "",
        "| 局/题 | 判定 | 蓝分 | 红分 | 耗时ms | 结果 |",
        "|---|---|---|---|---|---|",
    ]
    for t in turns:
        verdict = (t.get("classification") or {}).get("verdict", "-") if not t.get("invalid") else "invalid"
        lines.append(
            f"| {t.get('game')}/{t.get('seq')} | {verdict} | {t.get('blue_point', 0)}"
            f" | {t.get('red_point', 0)} | {t.get('latency_ms', '-')} | {t.get('end', '')} |")
    path = round_dir / "round_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def recompute(round_dir: Path) -> tuple[bool, str]:
    """只读 turns.jsonl 重算，与 summary.json 比对。"""
    summary_path = round_dir / "summary.json"
    if not summary_path.exists():
        return False, "summary.json 不存在"
    saved = json.loads(summary_path.read_text(encoding="utf-8"))
    games = []
    for entry in saved.get("games", []):
        turns = _read_jsonl(round_dir / f"game_{entry['game']}" / "turns.jsonl")
        games.append({"game": entry["game"], "expected": entry["expected"],
                      "turns": turns, "stop": entry.get("stop", "questions_done")})
    fresh = summarize(games)
    if fresh != {k: saved.get(k) for k in fresh}:
        return False, f"不一致: 重算 {fresh} != 记录 { {k: saved.get(k) for k in fresh} }"
    return True, "recompute OK"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="#061 对局日志重算")
    ap.add_argument("--recompute", required=True, help="一轮目录")
    args = ap.parse_args(argv)
    ok, message = recompute(Path(args.recompute))
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
