"""#061 阶段 5：held-out 封存题复测（docs/任务书-061-红蓝对抗对局.md :187）。

每轮末自动（run_round 集成，--heldout 关闭时跳过）：
  1. 用新 seed 让蓝方生成一套封存题（题面、题集 cid、蓝方出题理由）
  2. 题面 sha256 **先落盘** questions.sha256，再落题面 heldout.jsonl
     （先固定承诺这套题，防事后换题；PRD :204：每套只用一次）
  3. 红方对封存题作答（隔离实例，最新库版本），走与训练局同款判定链
  4. compare：训练局得分率 − 封存题得分率 > 15pp → 「疑似死记」（用户 10-27 定）

封存题一次性：runs/adversarial/heldout/used.jsonl 记档，同一套再跑直接拒绝。
"""
import hashlib
import json
import time
from pathlib import Path

HELDOUT_ROOT = Path("runs/adversarial/heldout")
DIFF_PP_THRESHOLD = 15.0     # 疑似死记判据（pp），用户 2026-10-27 定


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate(config: dict, out_root: Path = HELDOUT_ROOT, *,
             ask_blue=None, n: int | None = None) -> dict:
    """用新 seed 生成一套封存题。sha 先落盘，题面后落档；蓝方失败不落半套题面。"""
    from tests.adversarial.run_round import _blue_questions

    seed = f"{config['match']['seed']}-heldout-{time.strftime('%Y%m%d%H%M%S')}"
    cfg = {**config, "match": {**config["match"], "seed": seed}}
    kb = Path(config["kb"]["seed_path"])
    n = n or int(cfg["match"].get("heldout_questions", 5))
    items = _blue_questions(cfg, n, kb, ask_blue=ask_blue)
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = out_root / stamp
    run_dir.mkdir()
    sha_file = run_dir / "questions.sha256"
    # 题面哈希先落盘（任务书 :187）：先固定承诺这套题，再写题面全文
    sha_file.write_text("\n".join(_sha(it["text"]) for it in items) + "\n", encoding="utf-8")
    q_file = run_dir / "heldout.jsonl"
    with q_file.open("w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps({"sha256": _sha(it["text"]),
                                 "question": it["text"], "cid": it.get("intent", ""),
                                 "judge_points": it.get("judge_points", []),
                                 "reason": it.get("reason", "")}, ensure_ascii=False) + "\n")
    return {"dir": str(run_dir), "questions_file": str(q_file), "sha_file": str(sha_file),
            "n": len(items), "seed": seed}


def run(heldout_dir: Path, answer_fn, search_fn, scope_terms: list[str],
        keywords: dict | None = None, scope_judge=None, resolve_fn=None) -> dict:
    """红方对封存题作答并按训练局同款判定链计分。

    answer_fn(question) -> {"answer": str, "sources": list, "path": str}
    search_fn(question) -> list[条目]（label 属性，同 classify 约定）
    scope_judge(question) -> {"status": …}（训练局同款蓝方范围判定，集成时注入）
    resolve_fn(jp) -> jp：场景断言 expect_hit 内容短语→来源标签的解析
    （run_round._resolve 同款，集成时注入；不注入则按短语原样判定）
    同一套封存题已 used → 拒绝（ValueError）。
    """
    heldout_dir = Path(heldout_dir)
    _ensure_unused(heldout_dir)
    q_file = heldout_dir / "heldout.jsonl"
    items = [json.loads(l) for l in q_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    sha_file = heldout_dir / "questions.sha256"
    saved = [l for l in sha_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    if saved != [_sha(it["question"]) for it in items]:
        raise ValueError("封存题题面与已落盘 sha256 不一致，拒绝跑（防换题）")

    from tests.adversarial.classify import classify_turn

    turns = []
    for it in items:
        got = answer_fn(it["question"]) or {}
        judge_points = it.get("judge_points") or None
        if resolve_fn is not None and judge_points:
            judge_points = [(s, resolve_fn(jp)) for s, jp in judge_points]
        cls = classify_turn(it["question"], got.get("answer", ""), got.get("sources") or [],
                            got.get("path", "llm_generation"), scope_terms, search_fn,
                            keywords=keywords, open_question=False,
                            scope_judge=scope_judge,
                            cid=it.get("cid", ""), judge_point=judge_points)
        turns.append({"sha256": it["sha256"], "question": it["question"],
                      "classification": cls})
    with (heldout_dir / "heldout_turns.jsonl").open("w", encoding="utf-8") as fh:
        for t in turns:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")
    scoreable = [t for t in turns if t["classification"]["in_scope"] and t["classification"]["scored"]]
    red = sum(1 for t in turns if t["classification"]["verdict"] == "red_scored")
    rate = red / len(scoreable) if scoreable else 0.0
    result = {"n": len(turns), "scoreable": len(scoreable), "red_scored": red,
              "red_rate": round(rate, 4), "dir": str(heldout_dir)}
    _mark_used(heldout_dir, result)
    return result


def compare(train_rate: float, heldout_rate: float) -> dict:
    """训练局得分率 − 封存题得分率 > 15pp → 疑似死记。"""
    diff_pp = round((train_rate - heldout_rate) * 100, 1)
    return {"train_rate": train_rate, "heldout_rate": heldout_rate,
            "diff_pp": diff_pp,
            "suspected_memorization": diff_pp > DIFF_PP_THRESHOLD}


def report_section(round_dir: Path, compare_result: dict, meta: dict) -> Path:
    """把 held-out 段落追加进 round_report.md。"""
    tag = "疑似死记（差 > 15pp）" if compare_result["suspected_memorization"] else "未见疑似死记"
    lines = [
        "\n## held-out 封存题复测",
        f"- 封存题目录：`{meta.get('dir', '')}`（n={meta.get('n', '?')}，seed={meta.get('seed', '?')}）",
        f"- 训练局得分率 {compare_result['train_rate']:.1%}｜封存题得分率 {compare_result['heldout_rate']:.1%}"
        f"｜差 {compare_result['diff_pp']}pp",
        f"- 判定（阈值 {DIFF_PP_THRESHOLD}pp）：**{tag}**",
    ]
    report = Path(round_dir) / "round_report.md"
    with report.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return report


def _ensure_unused(heldout_dir: Path) -> None:
    used = Path(HELDOUT_ROOT) / "used.jsonl"
    if used.exists():
        want = str(Path(heldout_dir).resolve())
        for line in used.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("dir") == want:
                raise ValueError(f"封存题已用过（每套只用一次）: {want}")


def _mark_used(heldout_dir: Path, result: dict) -> None:
    used = Path(HELDOUT_ROOT) / "used.jsonl"
    used.parent.mkdir(parents=True, exist_ok=True)
    with used.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"dir": str(Path(heldout_dir).resolve()),
                             "used_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "red_rate": result.get("red_rate")}, ensure_ascii=False) + "\n")
