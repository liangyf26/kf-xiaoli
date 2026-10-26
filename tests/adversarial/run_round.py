"""#061 总入口：读配置 → 起隔离实例 → 跑 N 局 → 落盘 → 对账。

阶段 2 只做程序模式。--dry-run 不连真服务：红方交空答案，用来验编排和日志。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tests.adversarial import match_log
from tests.adversarial.broadcast import Outbox
from tests.adversarial.match import Answer, Pose, run_game

DEFAULTS = {
    "red": {"base": "http://172.28.29.24:30032/v1", "model": "qwen3.8-27b", "timeout_s": 120},
    "blue": {"base": "http://172.28.50.9:30030/v1", "model": "Qwen3.8-27B"},
    "match": {"games": 1, "mode": "fixed", "questions": 10, "target_score": 20,
              "max_questions": 60, "max_followups": 3, "seed": 42},
    "retry": {"per_question": 2, "backoff_s": [5, 20]},
    "kb": {"seed_path": "data/sdwan-new.md", "work_dir": "runs/adversarial/kb"},
    "broadcast": {"enabled": True, "group": "kf红蓝对抗直播", "level": "summary",
                  "max_per_sec": 2, "relay_every_s": 60},
    "mode": "program",
    "scope": ["SDWAN", "直播线路", "带宽"],
    "stall_s": 600,
}


def load_config(path: Path | None) -> dict:
    """yaml 缺的字段用 DEFAULTS 补。不用 PyYAML，配置只有一层映射。"""
    import yaml
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path else {}
    loaded = loaded or {}
    merged = dict(DEFAULTS)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _dry_pose(topic: int, followups: int) -> Pose:
    return Pose(question=f"dry-topic-{topic}", reason="dry-run", followup=bool(followups))


def _dry_answer(question: str) -> Answer:
    return Answer(answer="", sources=[], path="dry_run")


def run(config: dict, out_root: Path, questions: int | None = None,
        dry_run: bool = False, pose_fn=None, answer_fn=None) -> tuple[Path, int]:
    if config.get("mode") != "program":
        raise SystemExit("[run_round] 阶段 2 只做程序模式（mode: program）")
    if questions is not None:
        config["match"]["questions"] = questions
    kb = Path(config["kb"]["seed_path"])
    round_dir = match_log.new_round(out_root, config, kb if kb.exists() else None)
    box = Outbox(round_dir, level=config["broadcast"]["level"],
                 max_per_sec=config["broadcast"]["max_per_sec"],
                 enabled=config["broadcast"]["enabled"])
    box.emit(0, "open", f"开局 seed={config['match']['seed']} mode={config['match']['mode']}")

    games = []
    for game_no in range(1, config["match"]["games"] + 1):
        gdir = match_log.game_dir(round_dir, game_no)

        def on_turn(record, _dir=gdir):
            match_log.append_jsonl(_dir / "turns.jsonl", record)

        outcome = run_game(game_no, config, pose_fn or _dry_pose,
                           answer_fn or _dry_answer, lambda q: [],
                           on_turn, sleep=lambda s: None)
        games.append({"game": game_no, "expected": config["match"]["questions"],
                      "turns": outcome["turns"]})
        box.emit(game_no, "summary",
                 f"game {game_no}: 题 {len(outcome['turns'])} 无效 {outcome['invalid']}")
    box.flush()
    summary = match_log.summarize(games)
    match_log.write_summary(round_dir, summary)
    return round_dir, (0 if summary["balanced"] else 3)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="#061 一轮对局")
    ap.add_argument("--config", default="tests/adversarial/round.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--questions", type=int, default=None)
    ap.add_argument("--out", default="runs/adversarial")
    args = ap.parse_args(argv)
    if not args.dry_run:
        raise SystemExit("[run_round] 阶段 2 验收只跑 --dry-run；真模型局在阶段 3")
    config = load_config(Path(args.config))
    round_dir, code = run(config, Path(args.out), args.questions, dry_run=True)
    print(f"[run_round] {round_dir} exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
