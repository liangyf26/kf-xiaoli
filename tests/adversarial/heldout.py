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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# D（20261027 复审必修D）：账本锚项目根绝对路径——旧版 `runs/adversarial/heldout`
# 相对路径跟着 cwd 走，换目录运行就找不到账本（绕过口①）。
HELDOUT_ROOT = PROJECT_ROOT / "runs" / "adversarial" / "heldout"
MIN_SCOREABLE = 8            # E：可计分低于此数不下结论（默认 10 题的 80%）
DIFF_PP_THRESHOLD = 15.0     # 疑似死记判据（pp），用户 2026-10-27 定


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _set_key(shas: list[str]) -> str:
    """题面集合内容哈希：一次性判重的唯一键，与目录路径无关（绕过口②：整目录
    复制到新路径键不变，照样判已用）。"""
    return _sha("\n".join(sorted(shas)))


def generate(config: dict, out_root: Path = HELDOUT_ROOT, *,
             ask_blue=None, n: int | None = None,
             ledger: Path | None = None) -> dict:
    """用新 seed 生成一套封存题。sha 先落盘，题面后落档；蓝方失败不落半套题面。"""
    from tests.adversarial.run_round import _blue_questions

    seed = f"{config['match']['seed']}-heldout-{time.strftime('%Y%m%d%H%M%S')}"
    cfg = {**config, "match": {**config["match"], "seed": seed}}
    kb = Path(config["kb"]["seed_path"])
    n = n or int(cfg["match"].get("heldout_questions", 10))   # E：默认 10 题（可计分 <8 判样本不足）
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
                                 "reason": it.get("reason", ""),
                                 "trap": it.get("trap")}, ensure_ascii=False) + "\n")
    # D：生成即登记账本（state=started）——封存题一出生就占坑，作答中途崩溃
    # 也留痕（绕过口③：旧版作答成功后才记账，崩一次就白嫖一次）。
    key = _set_key([_sha(it["text"]) for it in items])
    _ledger_append({"set_key": key, "dir": str(run_dir.resolve()), "state": "started",
                    "sha256": [_sha(it["text"]) for it in items],
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ledger=ledger)
    return {"dir": str(run_dir), "questions_file": str(q_file), "sha_file": str(sha_file),
            "n": len(items), "seed": seed, "set_key": key}


def run(heldout_dir: Path, answer_fn, search_fn, scope_terms: list[str],
        keywords: dict | None = None, scope_judge=None, resolve_fn=None,
        ledger: Path | None = None, scope_ctx=None) -> dict:
    """红方对封存题作答并按训练局同款判定链计分。

    answer_fn(question) -> {"answer": str, "sources": list, "path": str}
    search_fn(question) -> list[条目]（label 属性，同 classify 约定）
    scope_judge(question) -> {"status": …}（训练局同款蓝方范围判定，集成时注入）
    resolve_fn(jp) -> jp：场景断言 expect_hit 内容短语→来源标签的解析
    （run_round._resolve 同款，集成时注入；不注入则按短语原样判定）
    scope_ctx（#062 第二节）：一轮共用的范围判定上下文，集成时注入（同主题沿用）

    D 一次性核账（20261027 复审必修D，默认账本 runs/adversarial/heldout/used.jsonl，
    测试可注入 ledger）：作答前依次核
      1. 题面 sha == questions.sha256（防换题，原有）
      2. 账本存在本套 set_key 的 started/used 记录 → 拒（已用/已启动，复制目录同拒）
      3. 账本查无本套 set_key → 拒（题面+sha 同改的未生成套，绕过口④）
    作答成功后把该条 started 原地升级 used。"""
    heldout_dir = Path(heldout_dir)
    q_file = heldout_dir / "heldout.jsonl"
    items = [json.loads(l) for l in q_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    sha_file = heldout_dir / "questions.sha256"
    saved = [l for l in sha_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    now_shas = [_sha(it["question"]) for it in items]
    if saved != now_shas:
        raise ValueError("封存题题面与已落盘 sha256 不一致，拒绝跑（防换题）")
    # N1（20261027 连审必修）：set_key 一律按题面现算，行内 sha256 字段不信任。
    # 旧 key 取 jsonl 字段——改题面+改 questions.sha256、保留旧字段即可让 set_key
    # 不变、账本照常放行（连审 R3 实测 ACCEPT）。
    for it, now in zip(items, now_shas):
        if it.get("sha256") != now:
            raise ValueError("封存题行内 sha256 字段与题面现算不一致，拒绝跑（防改字段绕过）")
    key = _set_key(now_shas)
    _check_unused(key, heldout_dir, ledger=ledger)
    _claim(key, heldout_dir, ledger=ledger)

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
                            cid=it.get("cid", ""), judge_point=judge_points,
                            scope_ctx=scope_ctx, trap=it.get("trap"))
        turns.append({"sha256": it["sha256"], "question": it["question"],
                      "classification": cls})
    with (heldout_dir / "heldout_turns.jsonl").open("w", encoding="utf-8") as fh:
        for t in turns:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")
    scoreable = [t for t in turns if t["classification"]["in_scope"] and t["classification"]["scored"]]
    red = sum(1 for t in turns if t["classification"]["verdict"] == "red_scored")
    rate = red / len(scoreable) if scoreable else 0.0
    result = {"n": len(turns), "scoreable": len(scoreable), "red_scored": red,
              "red_rate": round(rate, 4), "dir": str(heldout_dir), "set_key": key}
    _promote_answered(key, result, ledger=ledger)
    return result


def compare(train_rate: float, heldout_rate: float, scoreable_n: int = -1) -> dict:
    """训练局得分率 − 封存题得分率 > 15pp → 疑似死记。

    E（20261027 复审必修E）两种不判情形，绝不让 0 分母/小样本污染结论：
    - scoreable_n == 0 → status="无法判定"（旧版按 0 分母得 heldout_rate=0，训练局
      60% 就误报死记）；
    - 0 < scoreable_n < MIN_SCOREABLE(=8) → status="样本不足，不下结论"
      （默认 10 题差 1 题就 20pp 的假阳性来源）。
    两种情形 suspected_memorization 一律 False、diff_pp 保留计算值供参考但不参与判定。"""
    diff_pp = round((train_rate - heldout_rate) * 100, 1)
    if scoreable_n == 0:
        return {"train_rate": train_rate, "heldout_rate": heldout_rate, "diff_pp": diff_pp,
                "status": "无法判定", "scoreable_n": 0, "suspected_memorization": False}
    if scoreable_n < MIN_SCOREABLE:
        return {"train_rate": train_rate, "heldout_rate": heldout_rate, "diff_pp": diff_pp,
                "status": f"样本不足，不下结论（可计分 {scoreable_n} < {MIN_SCOREABLE}）",
                "scoreable_n": scoreable_n, "suspected_memorization": False}
    return {"train_rate": train_rate, "heldout_rate": heldout_rate, "diff_pp": diff_pp,
            "status": "ok", "scoreable_n": scoreable_n,
            "suspected_memorization": diff_pp > DIFF_PP_THRESHOLD}


def report_section(round_dir: Path, compare_result: dict, meta: dict) -> Path:
    """把 held-out 段落追加进 round_report.md（E：分子/分母必须写明）。"""
    tag = ("疑似死记（差 > 15pp）" if compare_result["suspected_memorization"]
           else compare_result.get("status", "未见疑似死记"))
    lines = [
        "\n## held-out 封存题复测",
        f"- 封存题目录：`{meta.get('dir', '')}`（n={meta.get('n', '?')}，seed={meta.get('seed', '?')}）",
        f"- 可计分（分子/分母口径）：red_scored {meta.get('red_scored', '?')} / 可计分 {meta.get('scoreable', '?')}"
        f" / 出题 {meta.get('n', '?')}",
        f"- 训练局得分率 {compare_result['train_rate']:.1%}｜封存题得分率 {compare_result['heldout_rate']:.1%}"
        f"｜差 {compare_result['diff_pp']}pp",
        f"- 判定（阈值 {DIFF_PP_THRESHOLD}pp）：**{tag}**",
    ]
    report = Path(round_dir) / "round_report.md"
    with report.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return report


def _ledger_path(ledger: Path | None = None) -> Path:
    p = Path(ledger) if ledger is not None else HELDOUT_ROOT / "used.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _ledger_rows(ledger: Path | None = None) -> list[dict]:
    p = _ledger_path(ledger)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            # H（20261027 复审小建议）：旧账本行没有 set_key/state（06:06 烟测残留
            # 的 /tmp 记录），标记 legacy 不参与判重，不删原行。
            rec.setdefault("state", "legacy")
            rows.append(rec)
    return rows


def _ledger_append(rec: dict, ledger: Path | None = None) -> None:
    with _ledger_path(ledger).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _check_unused(key: str, heldout_dir: Path, ledger: Path | None = None) -> None:
    """D 核账（三态：started→answering→used）：
    - used → 已用，拒；answering → 作答曾启动（崩溃亦占坑），重跑一律拒（绕过口③）
    - started 且 dir 不同 → 目录复制/换路径重跑，拒（绕过口②）
    - 账本查无此 set_key → 题面+sha 同改的未生成套，拒（绕过口④）
    - started 且 dir 相同 → 首次作答，放行（随后 _claim 原子升级 answering）
    旧账本（state=legacy）不占坑。"""
    here = str(Path(heldout_dir).resolve())
    rows = _ledger_rows(ledger)
    mine = [r for r in rows if r.get("set_key") == key]
    for r in mine:
        if r["state"] == "used":
            raise ValueError(f"封存题已用过（每套只用一次）: key={key[:12]} 首次={r.get('dir')}")
        if r["state"] == "answering":
            raise ValueError(f"封存题疑似复用（作答已在 {r.get('dir')} 启动，崩溃亦占坑）: 本次={here}")
    if not mine:
        raise ValueError(f"账本无此题面集合（未生成即作答，防题面+sha 同改）: key={key[:12]}")
    if all(r["state"] == "started" and r.get("dir") != here for r in mine):
        raise ValueError(f"封存题疑似复用（该题面集合已在别处生成: {mine[0].get('dir')}）: 本次={here}")


def _claim(key: str, heldout_dir: Path, ledger: Path | None = None) -> None:
    """首次作答领取：同 set_key 且同 dir 的 started → answering（唯一放行口，
    之后无论成败都拒重跑）。"""
    here = str(Path(heldout_dir).resolve())
    p = _ledger_path(ledger)
    rows = _ledger_rows(ledger)
    hit = False
    for r in rows:
        if r.get("set_key") == key and r["state"] == "started" and r.get("dir") == here:
            r["state"] = "answering"
            r["claimed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            hit = True
    if not hit:
        raise ValueError(f"无可领取的 started 记录（疑似并发/重跑）: key={key[:12]}")
    _rewrite_ledger(rows, p)


def _promote_answered(key: str, result: dict, ledger: Path | None = None) -> None:
    """作答成功：answering → used（审计留痕不删行）。"""
    p = _ledger_path(ledger)
    rows = _ledger_rows(ledger)
    for r in rows:
        if r.get("set_key") == key and r["state"] == "answering":
            r["state"] = "used"
            r["used_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            r["red_rate"] = result.get("red_rate")
    _rewrite_ledger(rows, p)


def _rewrite_ledger(rows: list[dict], path: Path) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(path)
