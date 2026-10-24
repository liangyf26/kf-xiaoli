"""蓝队风格池：采样器 + dry-run 出题入口（#057 任务书 4.2）。

随机源纪律（任务书 4.2/6）：只用 random.Random(seed) 实例（TDD :117 的
mutate(variant_seed) 同款写法）；禁止 random.* 模块级函数、禁止 time/uuid
参与选题。全局 Random 只允许出现在常量种子派生处。

蓝队/红队隔离（任务书 4.2）：blue_prompt() 与 red_payload() 是两个独立函数，
dry-run 分别落盘 blue_prompts.jsonl / red_payloads.jsonl 供 grep 复核——
红队可见上下文只含题面文本，不含风格卡任何字段。

dry-run（任务书 4.2/6）：蓝队用确定性假生成器（examples + 种子题干 + seed 拼题，
不联网），红队用假应答（固定空答案），不调 172.28.50.9 / 172.28.29.24、
不起真机；非 --dry-run 直接拒绝（本任务验收不调真机，防误触）。

CLI（任务书 4.2，--window 默认值按用户 2026-10-25 口径=20，可配置不许写死）:
  python -m tests.adversarial.scenarios.style_sampler \
    --seed 42 --n 60 --window 20 \
    --styles tests/adversarial/scenarios/styles \
    --matrix /tmp/sp057/matrix.json \
    --out /tmp/sp057/run1 --dry-run --kb data/sdwan.md
"""
import argparse
import json
import random
import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
PROJECT_ROOT = _THIS.parents[2]          # 仓库根 kf-xiaoli/
ADV_DIR = _THIS.parents[1]               # tests/adversarial/
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(ADV_DIR))         # 裸导入 test_knowledge_regression 需要

from tests.adversarial.judge import judge_turn                 # noqa: E402
from tests.adversarial.scenarios import verifier               # noqa: E402
from tests.adversarial.scenarios.verifier import (             # noqa: E402
    round_reject_rate, verify_one)

# 滑动窗口默认局数：用户 2026-10-25 口径（20 局，覆盖任务书旧值 10），
# 必须 --window 可配置，不许写死（Chief 转达，任务书 N 值确认流程同款）。
WINDOW_DEFAULT = 20
N_MAX = 60                 # 每局题数上限（任务书 4.2）
MAIN_FRAC = 0.6            # 主池占比 60/40（PRD :217-225）
RETRY_ROUNDS = 3           # 整局重出最多 3 次（任务书 4.3）
REJECT_THRESHOLD = 0.2     # 一局不过率 >20% 触发重出（任务书 4.3）

# 判据挂载映射：回归题组 → 场景 judge_point（判据原文取自 D1–D4 scenarios 的
# turn.judge_point，不新造判据）。语义依据：P/T/TAX/L=价格/试用/发票/合规，
# 是「诱导绝对承诺」高发面 → D1 forbid；TSH/C=售后排障/客户端，截图高频 →
# D2 图片语法 forbid；F=流量 → D3；D4 内部结构 forbid 对全部组叠加（任何
# 题面都可能被内部结构泄漏污染）。意图口径=ab_run.load_questions 的 cid。
SCENARIO_GROUP_MAP = {"D1": ("P", "T", "TAX", "L"), "D2": ("TSH", "C"),
                      "D3": ("F",), "D4": None}


def load_styles(styles_dir: Path) -> list:
    """读 5 张风格卡，按 style_id 排序（确定性；字段校验交单测）。"""
    cards = []
    for p in sorted(Path(styles_dir).glob("*.json")):
        cards.append(json.loads(p.read_text(encoding="utf-8")))
    return sorted(cards, key=lambda c: c["style_id"])


def load_question_pool() -> list:
    """种子题池：复用 ab_run.load_questions（题集单源，任务书 2）→ [(cid, 题干)]。"""
    from tests.adversarial.ab_run import load_questions
    return [(cid, q) for cid, q in load_questions()]


def load_scenario_judge_points() -> dict:
    """D1–D4 场景 turn1 的 judge_point（只读场景文件，短名 D1..D4 → judge_point）。"""
    out = {}
    for short in SCENARIO_GROUP_MAP:
        matches = sorted((_THIS.parent).glob(f"{short}-*.json"))
        if not matches:
            raise SystemExit(f"[sampler] 场景文件缺失: {short}-*.json")
        scene = json.loads(matches[0].read_text(encoding="utf-8"))
        out[short] = scene["turns"][0]["judge_point"]
    return out


def judge_points_for(cid: str, scene_jps: dict) -> list:
    """回归题 cid → 挂载的 [(场景短名, judge_point)]（映射见 SCENARIO_GROUP_MAP）。"""
    group = "".join(ch for ch in cid if ch.isalpha())
    pairs = []
    for short, groups in SCENARIO_GROUP_MAP.items():
        if groups is None or group in groups:      # None = D4 全组叠加
            pairs.append((short, scene_jps[short]))
    return pairs


def resolve_expect_hit(jp: dict, qa_pairs: list) -> dict | None:
    """把 expect_hit 词独立解析成目标条目的 source_label（#057 二审 P1）。

    上轮 align_expect_hit 拿检索 top-1 当期望，又拿同一条当 sources，判据变成
    自己跟自己比、永远绿。这条路径**不经过 search_top**：直接遍历
    KnowledgeBase.qa_pairs（公开属性），按标题或正文包含 expect_hit 词匹配，
    取命中条目的 source_label 当期望。匹配不到返回 None——调用方标
    UNJUDGEABLE，不许静默回退成观测值。forbid_regex / route 原样保留。
    """
    from backend.knowledge import source_label
    words = jp.get("expect_hit") or []
    if not words:
        return jp
    hits = [p for p in qa_pairs
            if any(w in (p.title or "") or w in (p.content or "") for w in words)]
    if not hits:
        return None
    return {**jp, "expect_hit": [source_label(p.section, p.number) for p in hits]}


def _run_order_key(run_id: str) -> tuple:
    """局序键：s<数字> 按数值排（#057 返工 P1）。

    字符串排序会把 s10 排在 s2 前面，取最近 1 局会取错。s 前缀取数字；
    非 s 前缀退回字典序（放数值局之后）。
    """
    if isinstance(run_id, str) and run_id.startswith("s") and run_id[1:].isdigit():
        return (0, int(run_id[1:]))
    return (1, str(run_id))


def _agg_by_run(matrix: dict, window: int | None = None) -> dict:
    """按采样窗口聚合 by_run 明细（最近 window 局，按 seed 数值取尾窗）。

    窗口口径（#057 返工 P1）：调用方显式传入的 window **优先**于矩阵文件里
    记录的 window（CLI --window 是当次采样的窗口，矩阵里的 window 只是上次
    重算时用的值）；两者都缺才退 WINDOW_DEFAULT。
    返回与 style_matrix.per_cell 同构的 {key: {lost, total, rate}}，rate 4 位。
    """
    if window is None:
        window = int(matrix.get("window") or WINDOW_DEFAULT)
    # window<=0 时 [-0:] 会取全部局（#057 二审 P3），下限锁 1
    window = max(1, int(window))
    runs = sorted(matrix.get("by_run") or {}, key=_run_order_key)[-window:]
    per: dict = {}
    for run in runs:
        for key, cell in (matrix["by_run"][run] or {}).items():
            # dry-run 的模拟格不进薄弱点权重（#057 二审 P1）：全是假应答算出来的
            # 失分率，混进来会带着采样器往假薄弱点上打。来源标记 simulated=true
            # 的格子整格跳过；没标记的按真实格计。
            if cell.get("simulated"):
                continue
            acc = per.setdefault(key, {"lost": 0, "total": 0})
            acc["lost"] += cell.get("lost", 0)
            acc["total"] += cell.get("total", 0)
    for acc in per.values():
        acc["rate"] = round(acc["lost"] / acc["total"], 4) if acc["total"] else 0.0
    return per


def sample(seed: int, n: int, styles: list, matrix: dict | None = None,
           window: int = WINDOW_DEFAULT) -> list:
    """出一局题目规格（纯函数，任务书 4.2；同 seed 两次结果相等）。

    返回逐题 spec：{idx, source, style_id, intent, template, stage, judge_point}。
    主池 round(0.6*n) 道按「合法格 (风格×意图)」均匀（洗牌轮转），其余打薄弱点；
    薄弱点：矩阵按失分率作权抽格（权重全 0 或无矩阵或局数<1 → 5 档风格各 20%
    轮转洗牌，任务书 4.2）；window 为采样窗口（局数），矩阵 by_run 明细里只取
    最近 window 局聚合权重。
    """
    n = min(int(n), N_MAX)
    m = round(MAIN_FRAC * n)
    weak = n - m
    pool = load_question_pool()
    seeds = {cid: text for cid, text in pool}
    scene_jps = load_scenario_judge_points()
    rng = random.Random(seed)

    legal = [(card, cid) for card in styles for cid in
             (verifier._intent_key(s) for s in card["material_scope"])
             if cid in seeds]
    if not legal:
        raise SystemExit("[sampler] 合法格为空（风格卡 scope 与题集意图不相交）")

    def spec(idx, source, card, cid):
        stage_jps = judge_points_for(cid, scene_jps)
        return {"idx": idx, "source": source, "style_id": card["style_id"],
                "intent": cid, "template": seeds[cid],
                "stage": stage_jps[0][0] if stage_jps else "",
                "judge_points": stage_jps}

    out = []
    cells = list(legal)
    rng.shuffle(cells)                       # 主池：风格×意图均匀（洗牌轮转）
    for i in range(m):
        card, cid = cells[i % len(cells)]
        out.append(spec(len(out), "main", card, cid))

    per = _agg_by_run(matrix, window) if (matrix and matrix.get("by_run")) else (
        (matrix or {}).get("per_cell") or {})
    weights = [float((per.get(f"{cid}|{card['style_id']}") or {}).get("rate", 0.0))
               for card, cid in legal]
    if sum(weights) > 0:                     # 薄弱点：失分率作权抽格（任务书 4.2）
        for _ in range(weak):
            card, cid = rng.choices(list(legal), weights=weights, k=1)[0]
            out.append(spec(len(out), "weak", card, cid))
    else:                                    # 无矩阵/局数<1/权重全 0 → 5 档各 20%
        order = list(styles)
        rng.shuffle(order)
        scopes = {c["style_id"]: [verifier._intent_key(s) for s in c["material_scope"]
                                  if verifier._intent_key(s) in seeds] for c in styles}
        # 某卡 material_scope 全无合法 cid 时跳过（#057 返工 P2）：否则轮转到
        # 该卡会 % 0 抛 ZeroDivisionError。全部卡都空已在上方 legal 检查拦截。
        order = [s for s in order if scopes[s["style_id"]]]
        pos = {c["style_id"]: 0 for c in order}
        pool_by_style: dict[str, list] = {}
        for s in order:
            ids = list(scopes[s["style_id"]])
            rng.shuffle(ids)
            pool_by_style[s["style_id"]] = ids
        for i in range(weak):
            card = order[i % len(order)]
            sid = card["style_id"]
            cid = pool_by_style[sid][pos[sid] % len(pool_by_style[sid])]
            pos[sid] += 1
            out.append(spec(len(out), "weak", card, cid))
    return out


def fake_generate(spec: dict, card: dict, seed: int) -> str:
    """dry-run 蓝队假生成器（确定性，不联网）：examples + 种子题干 + seed 拼题。

    「单号」段让同格多次抽题的题面彼此不同（防 DUP 误剔），也使换 seed 的
    题面整体可区分（A3）；不含风格卡字段、不含库内原文。
    """
    grng = random.Random(f"{seed}:{spec['idx']}:{spec['style_id']}")
    example = grng.choice(card["examples"])
    return f"{example}（单号{seed}-{spec['idx']:02d}）{spec['template']}"


def blue_prompt(spec: dict, card: dict, seed: int) -> str:
    """蓝队 prompt（任务书 4.2）：只拼风格卡字段 + 意图标签 + 种子题干。

    不拼知识库正文、不拼判据/答案。仅落盘 blue_prompts.jsonl（蓝队内部）。
    """
    parts = [f"style_id={card['style_id']}", f"persona={card['persona']}",
             f"tone={card['tone']}", f"sentence_len={card['sentence_len']}",
             f"follow_up={card['follow_up']}",
             f"material_scope={','.join(card['material_scope'])}",
             f"temperature={card['temperature']}",
             f"intent={spec['intent']}",
             f"seed_template={spec['template']}",
             f"单号={seed}-{spec['idx']:02d}"]
    return " | ".join(parts)


def red_payload(idx: int, text: str) -> dict:
    """红队可见上下文（任务书 4.2）：只含题面文本（dry-run 无对话历史）。

    不得含风格卡任何字段名与 style_id 值（5.1.7 单测锁定）。
    """
    return {"idx": idx, "text": text}


def build_round(seed: int, n: int, styles: list, kb_lines: list,
                matrix: dict | None, window: int = WINDOW_DEFAULT) -> tuple:
    """生成一局题面并质检，返回 (questions, reject_rate)。

    questions 每项含 spec + card + text + verifier{pass,code}；被剔题保留原
    idx（供矩阵分母口径：被剔题不进分母）。text 生成后与库行做 LEAK 检查
    （kb_lines 来自 start_isolated.gate_copy 拷出的副本，不读 data/ 原件写任何地方）。
    """
    specs = sample(seed, n, styles, matrix=matrix, window=window)
    by_style = {c["style_id"]: c for c in styles}
    questions, seen, flags = [], [], []
    for spec in specs:
        card = by_style[spec["style_id"]]
        text = fake_generate(spec, card, seed)
        q = {"intent": spec["intent"], "material_scope": card["material_scope"],
             "text": text, "judge_point": (spec["judge_points"][0][1]
                                           if spec["judge_points"] else {})}
        ok, code = verify_one(q, kb_lines, seen)
        flags.append(ok)
        if ok:
            seen.append(text)
        questions.append({**spec, "card": card, "text": text,
                          "verifier": {"pass": ok, "code": code}})
    return questions, round_reject_rate(flags)


def run_with_retry(seed: int, n: int, styles: list, kb_lines: list,
                   matrix: dict | None, window: int = WINDOW_DEFAULT,
                   build_fn=build_round) -> tuple:
    """整局重出（任务书 4.3）：不过率 >20% 用 seed+1 重出，最多 3 次。

    第 3 次仍超标：stdout 打印 VERIFIER_REJECT_RATE=<x> 并退出码 2。
    返回 (questions, used_seed, attempts)。
    """
    rate = 0.0
    for attempt in range(RETRY_ROUNDS):
        used = seed + attempt
        questions, rate = build_fn(used, n, styles, kb_lines, matrix, window)
        if rate <= REJECT_THRESHOLD:
            return questions, used, attempt + 1
    print(f"VERIFIER_REJECT_RATE={rate:.4f}")
    raise SystemExit(2)


def run_dry_run(args) -> int:
    """dry-run 全流程：采样→假生成→质检（重出）→假应答 verdict→落盘三文件。"""
    from tests.adversarial.start_isolated import gate_copy   # 只 import，不改动它

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    styles = load_styles(Path(args.styles))
    matrix = None
    if args.matrix and Path(args.matrix).exists():
        matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))

    # 知识库只经 gate_copy 的 /tmp 副本读取（任务书 4.3）；重跑先清自己上次的副本
    kb_src = Path(args.kb) if args.kb else PROJECT_ROOT / "data" / "sdwan.md"
    kb_dst = Path("/tmp/kf-adv/kb-sp057") / out.name / kb_src.name
    kb_dst.parent.mkdir(parents=True, exist_ok=True)
    kb_dst.unlink(missing_ok=True)
    kb_real = gate_copy(kb_src, kb_dst)
    kb_lines = kb_real.read_text(encoding="utf-8").splitlines()

    questions, used_seed, attempts = run_with_retry(
        args.seed, args.n, styles, kb_lines, matrix, args.window)

    # 期望与观测分开（#057 二审 P1）：expect_hit 遍历 qa_pairs 按词匹配得出
    # 目标 source_label，不经过 search_top；观测 sources 才是检索 top-1。
    # 两者用同一条值就是自己跟自己比，永远绿。匹配不到标 UNJUDGEABLE。
    from backend.knowledge import KnowledgeBase, source_label
    kb_copy = KnowledgeBase(kb_real)
    observed = {}
    for cid, text in load_question_pool():
        hits = kb_copy.search_top(text, limit=1)
        if hits:
            observed[cid] = source_label(hits[0].section, hits[0].number)

    q_lines, blue_lines, red_lines = [], [], []
    # run_id 由局种子标识（s<seed>），与 --out 目录名解耦：同 seed 重放是同一局，
    # 任务书 A2/A8 的整文件 cmp 才能成立；矩阵 by_run 也按局聚合（style_matrix 4.4）。
    for q in questions:
        ok, stage = True, q["stage"]
        unjudgeable = False
        for short, jp in q["judge_points"]:   # dry-run 红队假应答：固定空答案
            aligned = resolve_expect_hit(jp, kb_copy.qa_pairs)
            if aligned is None:               # expect_hit 词在库里匹配不到
                unjudgeable = True
                break
            j = judge_turn("", [observed.get(q["intent"], "")], "", aligned)
            if not j["pass"]:
                ok, stage = False, short
                break
        verdict = {"ok": None if unjudgeable else ok, "stage": stage,
                   "simulated": True}         # dry-run 全是模拟来源，不进薄弱权重
        row = {"run_id": f"s{used_seed}", "seed": used_seed, "idx": q["idx"],
               "style_id": q["style_id"], "intent": q["intent"], "text": q["text"],
               "verifier": q["verifier"], "verdict": verdict,
               "latency_ms": 0}
        q_lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        blue_lines.append(json.dumps(
            {"idx": q["idx"], "style_id": q["style_id"], "intent": q["intent"],
             "prompt": blue_prompt(q, q["card"], used_seed)},
            ensure_ascii=False, sort_keys=True))
        if q["verifier"]["pass"]:
            red_lines.append(json.dumps(
                red_payload(q["idx"], q["text"]), ensure_ascii=False, sort_keys=True))

    (out / "questions.jsonl").write_text("\n".join(q_lines) + "\n", encoding="utf-8")
    (out / "blue_prompts.jsonl").write_text("\n".join(blue_lines) + "\n", encoding="utf-8")
    (out / "red_payloads.jsonl").write_text("\n".join(red_lines) + "\n", encoding="utf-8")
    print(f"[sampler] run={out.name} seed={used_seed}（重出 {attempts-1} 次）"
          f" n={len(questions)} 过检={sum(1 for q in questions if q['verifier']['pass'])}"
          f" kb副本={kb_real}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="蓝队风格池采样器（#057，dry-run）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--window", type=int, default=WINDOW_DEFAULT,
                    help=f"滑动窗口局数（默认 {WINDOW_DEFAULT}，用户 2026-10-25 口径）")
    ap.add_argument("--styles", default=str(_THIS.parent / "styles"))
    ap.add_argument("--matrix", default=None, help="失分率矩阵 json（style_matrix.py 产物）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--kb", default=None, help="知识库文件（经 gate_copy 拷 /tmp 副本后只读副本）")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = ap.parse_args(argv)
    if not args.dry_run:
        raise SystemExit("[sampler] 本任务只支持 --dry-run（验收不调真机/不联网）")
    return run_dry_run(args)


if __name__ == "__main__":
    sys.exit(main())
