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
            "balanced": g["expected"] == len(turns),
        })
    balanced = all(g["balanced"] for g in per_game) and bool(per_game)
    return {"games": per_game, "balanced": balanced,
            "invalid_total": sum(g["invalid"] for g in per_game),
            "results_total": sum(g["results"] for g in per_game)}


def write_summary(round_dir: Path, summary: dict) -> Path:
    path = round_dir / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
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
        games.append({"game": entry["game"], "expected": entry["expected"], "turns": turns})
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
