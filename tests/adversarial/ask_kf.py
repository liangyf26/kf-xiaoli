"""#061 群聊模式入口（阶段 2 先落函数，阶段 3 再接真实例）。

红队只传题号。脚本按题号从日志取题面原文，拒绝不存在或已答过的题号。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_questions(path: Path) -> dict[str, dict]:
    """jsonl 每行一个 {id, question}。"""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        out[str(item["id"])] = item
    return out


def answer_status(turns_path: Path) -> set[str]:
    """已有终态的题号。"""
    if not turns_path.exists():
        return set()
    done = set()
    for line in turns_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            done.add(str(json.loads(line).get("id")))
    return done


def take(qid: str, questions: dict, answered: set[str]) -> dict:
    """按题号取题。不存在或已答过都抛 ValueError，调用方不发请求。"""
    if qid not in questions:
        raise ValueError(f"题号不存在: {qid}")
    if qid in answered:
        raise ValueError(f"题号已答过: {qid}")
    return questions[qid]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="#061 按题号取题（群聊模式）")
    ap.add_argument("qid")
    ap.add_argument("--questions", required=True)
    ap.add_argument("--turns", required=True)
    args = ap.parse_args(argv)
    try:
        item = take(args.qid, load_questions(Path(args.questions)),
                    answer_status(Path(args.turns)))
    except ValueError as exc:
        print(f"[ask_kf] 拒绝: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"id": item["id"], "question": item["question"]}, ensure_ascii=False))
    print("NEXT: blue_new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
