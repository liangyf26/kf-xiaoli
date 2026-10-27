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
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LABELED = PROJECT_ROOT / "data" / "wx_real" / "labeled_060.json"
SESSIONS = PROJECT_ROOT / "data" / "wx_real" / "sessions.json"
SESSION_CAP = 3
AGREE_THRESHOLD = 0.85
# 开放题问句特征：严格按 K 口径清单（20261027 Chief 逐条看样后定）。
# 旧表里的「几」误命中非问句闲聊（「已几晚加班」「估计局长几天睡不着」实证），已去。
_HINTS = ("？", "?", "吗", "呢", "多少", "怎么", "能不能", "是不是", "行不行")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_labeled(path: Path = LABELED) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def load_cases(path: Path = LABELED) -> list[dict]:       # 兼容旧名
    return load_labeled(path)


def load_sessions(path: Path = SESSIONS) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["sessions"]


def open_pool(sessions: list[dict] | None = None, labeled: list[dict] | None = None,
              domain_sessions: set[str] | None = None, min_len: int = 8,
              max_len: int = 60, require_prior_turn: bool = True) -> list[dict]:
    """开放题候选池：原始会话的客户消息，有问句特征、不在已标卷里（285 卷是
    closed-book——expected_action 空的全是 not_customer，不是开放题）。

    K 修正（20261027，Chief 逐条看过 /tmp/calib061b 样本后定口径）：
    - 域过滤：默认只抽 labeled_060 出现过的 session（107 个 SDWAN 业务域会话），
      房产出租/公司转让/开发票类混入会让人工标注白费；
    - 题面 8-60 字（原来 ≤80，碎片「怎么测」与粘贴段落都进不来）；
    - 同会话该 turn 之前必须已有 turn（首 turn 多为无上下文碎片，判不了该答该转）。
    """
    sessions = load_sessions() if sessions is None else sessions
    labeled = load_labeled() if labeled is None else labeled
    known = {_sha(c.get("question") or "") for c in labeled}
    known |= {str(c.get("question") or "").strip() for c in labeled}
    if domain_sessions is None:
        domain_sessions = {c.get("session_id") for c in labeled if c.get("session_id")}
    pool: list[dict] = []
    for s in sessions:
        if s["session_id"] not in domain_sessions:
            continue
        turns = s.get("turns") or []
        for i, t in enumerate(turns):
            if t.get("speaker") != "customer":
                continue
            text = (t.get("text") or "").strip()
            if not text or text in known or _sha(text) in known:
                continue
            if not any(h in text for h in _HINTS):
                continue
            if not (min_len <= len(text) <= max_len):
                continue
            if require_prior_turn and i == 0:    # 首 turn 无上下文，脱离会话判不了
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
    """人工 vs rubric 逐条一致率。

    F（20261027 复审必修F）分母口径修正：
    - 分母 = 人工侧给了 verdict 的全部条目（rubric 缺条/漏判=不一致，不再剔除分母——
      旧口径漏判 3 条被剔后 7/7=100% 虚高开闸）；
    - 人工未标（verdict 空）且 rubric 也空 → 单列 undecidable「无法判定」，
      不进分子也不进分母，但报条数（人工允许标不动 5 条的出口）；
    - 人工已标但 rubric 漏 → blue_missing 单列且计入分母的不一致。"""
    manual = {json.loads(l)["case_id"]: json.loads(l)
              for l in Path(manual_path).read_text(encoding="utf-8").splitlines() if l.strip()}
    rubric = {json.loads(l)["case_id"]: json.loads(l)
              for l in Path(rubric_path).read_text(encoding="utf-8").splitlines() if l.strip()}
    manual_verdict = {cid: (m.get("verdict") or "").strip() for cid, m in manual.items()}
    rubric_verdict = {cid: (r.get("verdict") or "").strip() for cid, r in rubric.items()}
    undecidable = sorted(cid for cid, v in manual_verdict.items()
                         if not v and not rubric_verdict.get(cid, ""))
    scored = {cid: v for cid, v in manual_verdict.items() if v}
    blue_missing = sorted(cid for cid in scored if not rubric_verdict.get(cid, ""))
    agree = sum(1 for cid, v in scored.items()
                if rubric_verdict.get(cid) and rubric_verdict[cid] == v)
    n = len(scored)
    rate = agree / n if n else 0.0
    return {"n": n, "agree": agree, "rate": round(rate, 4),
            "pass": n >= len(manual) - 5 and rate >= AGREE_THRESHOLD and not blue_missing,
            "blue_missing": blue_missing, "undecidable": undecidable}


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
        # L（20261027 复审必修L）：判分必须直调 _blue_chat（temperature=0）。
        # 旧版两个错：①blue 键名 base_url/api_key 与 _blue_chat 读的 base 不符
        # →25/25 KeyError: 'base'；②误用 _ask_blue（出题 helper），prompt 被包上
        # 「按风格卡出一道客户问题」→ 模型被判分任务时被要求出题。
        from tests.adversarial.run_round import _blue_chat
        rubric_text = Path(args.rubric).read_text(encoding="utf-8")
        blue = {"base": os.environ.get("BLUE_BASE", ""),
                "model": os.environ.get("BLUE_MODEL", ""),
                "temperature": 0}                     # key 由 _blue_chat 从 QWEN_BLUE_API_KEY 取
        rows = []
        for line in Path(args.cases).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            try:
                text = _blue_chat(blue, rubric_prompt(c["question"], rubric_text),
                                  temperature=0)
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
