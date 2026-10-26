"""#061 阶段 5：开放题一次性校准（任务书 :188）。

抽 20 条开放题 → 人工标一次 → 蓝方按 rubric 判一遍 → 一致率 ≥85% 才开启
开放题计分（round.yaml scoring.open_questions: true）；校准前只记录不计分。

用法：
  .venv/bin/python -m tests.adversarial.calibrate_open sample --out tmp/calib061
  .venv/bin/python -m tests.adversarial.calibrate_open score \
      --cases tmp/calib061/cases.jsonl --rubric tmp/calib061/rubric.md \
      --out tmp/calib061/rubric.jsonl
  .venv/bin/python -m tests.adversarial.calibrate_open agree \
      --manual tmp/calib061/manual.jsonl --rubric tmp/calib061/rubric.jsonl

抽样沿用 labeling-batch-protocol 口径：分层按 batch 占比、同会话 cap、
固定 seed 可复现；盲标稿答案位全空，块头写三件套口径与来源提醒。
"""
import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LABELED = PROJECT_ROOT / "data" / "wx_real" / "labeled_060.json"
SESSIONS = PROJECT_ROOT / "data" / "wx_real" / "sessions.json"
SESSION_CAP = 3
AGREE_THRESHOLD = 0.85
# 开放题问句特征：含问号或常见疑问词（宽松启发式，抽完还有人工复核关）
_HINTS = ("？", "?", "多少", "怎么", "为什么", "为什么", "能不能", "可以吗", "吗", "咋", "啥", "几")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_labeled(path: Path = LABELED) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def load_cases(path: Path = LABELED) -> list[dict]:       # 兼容旧名
    return load_labeled(path)


def load_sessions(path: Path = SESSIONS) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["sessions"]


def open_pool(sessions: list[dict] | None = None, labeled: list[dict] | None = None) -> list[dict]:
    """开放题候选池：原始会话的客户消息，有问句特征、不在已标卷里（285 卷是
    closed-book——expected_action 空的全是 not_customer，不是开放题）。"""
    sessions = load_sessions() if sessions is None else sessions
    labeled = load_labeled() if labeled is None else labeled
    known = {_sha(c.get("question") or "") for c in labeled}
    known |= {str(c.get("question") or "").strip() for c in labeled}
    pool: list[dict] = []
    for s in sessions:
        for i, t in enumerate(s.get("turns") or []):
            if t.get("speaker") != "customer":
                continue
            text = (t.get("text") or "").strip()
            if not text or text in known or _sha(text) in known:
                continue
            if not any(h in text for h in _HINTS):
                continue
            if len(text) > 80:                       # 长段非问句（粘贴/岗位描述类）剔除
                continue
            pool.append({"case_id": f"{s['session_id']}-t{i}",
                         "batch": s.get("session_key", "?")[:1] or "?",
                         "session_id": s["session_id"],
                         "question": text,
                         "bot_reply": t.get("bot_reply") or "",
                         "human_reply": t.get("human_reply") or ""})
    return pool


def sample_open(cases: list[dict], n: int = 20, seed: int = 61,
                session_cap: int = SESSION_CAP) -> list[dict]:
    """分层抽样：按 batch 占比分名额（最大余数法）、同会话不超过 session_cap、固定 seed。"""
    pool = cases
    batches = sorted({c.get("batch") or "?" for c in pool})
    share = Counter((c.get("batch") or "?") for c in pool)
    quota: dict[str, int] = {}
    used = 0
    for i, b in enumerate(batches):
        if i == len(batches) - 1:
            quota[b] = n - used                      # 最后一个批次兜底，总额 = n
        else:
            q = share[b] * n // len(pool)
            quota[b] = q
            used += q
    rng = random.Random(seed)
    sessions: dict[str, int] = defaultdict(int)
    picked: list[dict] = []
    for b in batches:
        items = [c for c in pool if (c.get("batch") or "?") == b]
        rng.shuffle(items)
        got = 0
        for c in items:
            if got >= quota[b]:
                break
            if sessions[c["session_id"]] >= session_cap:
                continue
            picked.append(c)
            sessions[c["session_id"]] += 1
            got += 1
    return picked[:n]


def write_blind(cases: list[dict], out_md: Path) -> Path:
    """盲标稿：块头口径 + 每题一块（题面 + 空答位）。沿用 labeling-batch-protocol。"""
    lines = [
        "# 校准-061 开放题盲标稿",
        "",
        f"> 样本 {len(cases)} 条（开放题：无期望动作、有实际问句）。口径：人工只判",
        "> 「该答 / 该转人工」与一句理由；答案位留空待填，标注后回填 manual.jsonl。",
        "",
    ]
    for c in cases:
        lines += [f"## {c['case_id']}", f"- 题面：{c['question']}",
                  f"- 人工结论（answer/handoff）：", f"- 理由：", ""]
    out_md = Path(out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")
    return out_md


def dump_cases(cases: list[dict], out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps({"case_id": c["case_id"], "batch": c.get("batch"),
                                 "session_id": c["session_id"],
                                 "question": c["question"], "sha256": _sha(c["question"]),
                                 "expected_action": c.get("expected_action", "")},
                                ensure_ascii=False) + "\n")
    return out_path


def agreement(manual_path: Path, rubric_path: Path) -> dict:
    """人工 vs rubric 逐条一致率。manual/rubric 行均含 case_id 与 verdict(answer/handoff)。"""
    manual = {json.loads(l)["case_id"]: json.loads(l)
              for l in Path(manual_path).read_text(encoding="utf-8").splitlines() if l.strip()}
    rubric = {json.loads(l)["case_id"]: json.loads(l)
              for l in Path(rubric_path).read_text(encoding="utf-8").splitlines() if l.strip()}
    both = sorted(set(manual) & set(rubric))
    if not both:
        return {"n": 0, "agree": 0, "rate": 0.0, "pass": False}
    agree = sum(1 for cid in both if manual[cid].get("verdict") == rubric[cid].get("verdict"))
    rate = agree / len(both)
    return {"n": len(both), "agree": agree, "rate": round(rate, 4),
            "pass": rate >= AGREE_THRESHOLD}


def rubric_prompt(question: str, rubric_text: str) -> str:
    return (f"你是客服质检员。按以下评分规则判断客户这句话客服应「answer」（直接回答）"
            f"还是「handoff」（转人工）。\n评分规则：\n{rubric_text}\n\n"
            f"客户话术：{question}\n\n"
            f"只回一行 JSON：{{\"verdict\": \"answer|handoff\", \"basis\": \"一句话依据\"}}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="#061 阶段5 开放题校准工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_s = sub.add_parser("sample", help="抽开放题 + 写盲标稿")
    p_s.add_argument("--out", default="tmp/calib061")
    p_s.add_argument("--n", type=int, default=20)
    p_s.add_argument("--seed", type=int, default=61)
    p_r = sub.add_parser("score", help="蓝方按 rubric 判一遍")
    p_r.add_argument("--cases", required=True)
    p_r.add_argument("--rubric", required=True)
    p_r.add_argument("--out", required=True)
    p_a = sub.add_parser("agree", help="人工 vs rubric 一致率")
    p_a.add_argument("--manual", required=True)
    p_a.add_argument("--rubric", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "sample":
        cases = sample_open(open_pool(), n=args.n, seed=args.seed)
        cases_jsonl = dump_cases(cases, Path(args.out) / "cases.jsonl")
        blind = write_blind(cases, Path(args.out) / "blind_manual.md")
        print(json.dumps({"n": len(cases), "cases": str(cases_jsonl), "blind": str(blind)},
                         ensure_ascii=False))
        return 0
    if args.cmd == "score":
        from tests.adversarial.run_round import _ask_blue
        rubric_text = Path(args.rubric).read_text(encoding="utf-8")
        blue = {"base_url": "", "model": "", "api_key": ""}   # 由环境注入
        import os
        blue = {"base_url": os.environ.get("BLUE_BASE", ""),
                "model": os.environ.get("BLUE_MODEL", ""),
                "api_key": os.environ.get("BLUE_API_KEY", "")}
        rows = []
        for line in Path(args.cases).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            try:
                text, _ = _ask_blue(blue, rubric_prompt(c["question"], rubric_text), "{}")
                verdict = json.loads(text) if text.strip().startswith("{") else {}
            except Exception as exc:                       # noqa: BLE001
                verdict = {"verdict": "", "basis": f"{type(exc).__name__}: {exc}"}
            rows.append({"case_id": c["case_id"], "verdict": verdict.get("verdict", ""),
                         "basis": verdict.get("basis", "")})
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(json.dumps({"n": len(rows), "out": str(out)}, ensure_ascii=False))
        return 0
    result = agreement(args.manual, args.rubric)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
