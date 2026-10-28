#!/usr/bin/env python
"""#062 两局同口径对比分析（离线，只读 turns.jsonl，不碰端点）。

口径（Chief 指定，两局必须一致）：
- 口径A：answer 含「转接人工客服」
- 口径B：answer 整句 == HANDOFF_MESSAGE（settings）
- 转人工题耗时：口径B 题的 latency_ms 均值
- 该答却转人工：口径B 且非陷阱 且 top1>8.0；分母=非陷阱题数；门槛 ≤10%
- 强相关子集：top1≥FORCE_ANSWER_MIN_TOP1(20.0) 单列
- P90：nearest-rank，全题 latency_ms
用法：python analyze_round_062.py <round_dir> [round_dir2 ...]
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # 仓库根（不再写死 worktree）
# settings 的必填字段从仓库根 .env 读（backend/config.py 支持 ENV_FILE 指路），
# 与 CWD 无关——worktree 删除后从主检出也能跑
if (ROOT / ".env").exists():
    os.environ.setdefault("ENV_FILE", str(ROOT / ".env"))
sys.path.insert(0, str(ROOT))
from backend.config import settings  # noqa: E402

HANDOFF = settings.HANDOFF_MESSAGE
HANDOFF_KEY = "转接人工客服"
FORCE_THRESHOLD = settings.FORCE_ANSWER_MIN_TOP1

# 理由不实判定（与 scripts/reason_audit_062.py 的规则保持同步；终审 900f51e 实锤
# seq34/seq21 后收紧：全面否认=top1≥20 即存疑；部分否认需「纯话术+自认仅覆盖」
# ——理由自己说条目覆盖了 X，纯话术却连 X 都没答，行为与理由矛盾）
FULL_DENY_RE = re.compile(
    r"知识库(完全不?|没|无|未)覆盖此类|完全不覆盖|(不|未)覆盖此类|"
    r"无法提供公司(资质|全称)|库里(没有|无)|库中无")
PARTIAL_COVER_RE = re.compile(r"仅覆盖|只覆盖")
DENY_RE = re.compile(FULL_DENY_RE.pattern + "|" +
                     r"仅覆盖|只覆盖|未涉及|未提及|未覆盖|仅确认|仅提到|仅提供")


def norm(s: str) -> str:
    return "".join(s.split()).rstrip("。.!！？?~～")


def analyze(round_dir: Path) -> dict:
    turns = [json.loads(l) for l in
             (round_dir / "game_1" / "turns.jsonl").read_text().splitlines() if l.strip()]
    # kb_top1 离线补算：human_escalation 等路径记录里没有数值字段 → 对局内 kb 副本
    # 跑 kb._relevance 补齐（与局内同一份库，口径一致）
    kb_file = round_dir / "kb" / "sdwan-new-062.md"
    kb = None
    if kb_file.exists():
        from backend.knowledge import KnowledgeBase
        kb = KnowledgeBase(kb_file)
    for r in turns:
        if r.get("kb_top1") is None and kb is not None:
            best = 0.0
            q = r.get("question") or ""
            for qa in kb.qa_pairs:
                best = max(best, kb._relevance(qa, q))
            r["kb_top1"] = best
    top1 = {r["seq"]: r.get("kb_top1") for r in turns}
    traps = set()
    for r in turns:
        c = r.get("classification") or {}
        if c.get("is_trap") or c.get("trap"):
            traps.add(r["seq"])
    n = len(turns)
    a_hits = [r for r in turns if HANDOFF_KEY in r.get("answer", "")]
    b_hits = [r for r in turns if norm(r.get("answer", "")) == norm(HANDOFF)]
    lat = {r["seq"]: r.get("latency_ms") or 0 for r in turns}
    b_lat = [lat[r["seq"]] for r in b_hits]
    fast = [r for r in b_hits if r.get("path") == "human_escalation"]
    gen = [r for r in b_hits if r.get("path") != "human_escalation"]
    # 该答却转人工：转人工 且 非陷阱 且 top1>8.0（Reviewer P1-3 定义）。
    # 两个口径都算：A=含「转接人工客服」字样（宽，含整句外的混合回答）；
    # B=整句==话术（严）。seed42 实测 A=15/28、B=12/28。
    nontrap = [r for r in turns if r["seq"] not in traps]
    a_wrong = [r["seq"] for r in a_hits if r["seq"] not in traps and (top1.get(r["seq"]) or 0) > 8.0]
    b_wrong = [r["seq"] for r in b_hits if r["seq"] not in traps and (top1.get(r["seq"]) or 0) > 8.0]
    wrongly = a_wrong   # 主口径=A（宽），B 单列
    nontrap_n = len(nontrap)
    strong = [(r["seq"], round(top1.get(r["seq"]) or 0, 1)) for r in a_hits
              if (top1.get(r["seq"]) or 0) >= FORCE_THRESHOLD]
    strong_all = sorted((r["seq"], top1[r["seq"]]) for r in turns
                        if (top1.get(r["seq"]) or 0) >= FORCE_THRESHOLD)
    all_lat = sorted(lat.values())
    import math
    p90 = all_lat[max(0, math.ceil(0.9 * len(all_lat)) - 1)]
    # escalate_reason 分类：决策层 / 缺陷合成（超阈值未写理由） / 模型自写
    er = [(r["seq"], r.get("escalate_reason") or "") for r in turns
          if r.get("escalate_reason")]
    er_fast = [(s, t) for s, t in er if t.startswith("决策层")]
    er_synth = [(s, t) for s, t in er if "仍转人工且未写理由" in t]
    er_model = [(s, t) for s, t in er if s not in {x for x, _ in er_fast + er_synth}]
    m = {
        "dir": str(round_dir.name), "n": n, "traps": sorted(traps),
        "A": (len(a_hits), sum(lat[r["seq"]] for r in a_hits) / len(a_hits) / 1000 if a_hits else 0),
        "B": (len(b_hits), sum(b_lat) / len(b_lat) / 1000 if b_lat else 0),
        "fast_n": len(fast), "fast_lat": (sum(lat[r["seq"]] for r in fast) / len(fast) / 1000 if fast else 0),
        "gen_n": len(gen), "gen_lat": (sum(lat[r["seq"]] for r in gen) / len(gen) / 1000 if gen else 0),
        "wrong": (len(wrongly), nontrap_n,
                  len(wrongly) / nontrap_n * 100 if nontrap_n else 0),
        "wrong_seqs": wrongly,
        "b_wrong_n": len(b_wrong), "b_wrong_seqs": b_wrong,
        "strong_escal": strong, "strong_all": strong_all,
        "p90": p90 / 1000,
        "seq_lat": {r["seq"]: lat[r["seq"]] / 1000 for r in turns},
        "paths": {r["seq"]: r.get("path") for r in turns},
        "retries": {r["seq"]: r.get("retries") for r in turns if r.get("retries")},
        "er_fast": er_fast, "er_synth": er_synth, "er_model": er_model,
    }
    # A-wrong 六分（终审后口径，Chief 2026-10-28）：③→②'→②→①→④→⑤，互斥。
    # ②'（理由不实）先于 ② 判——理由声称库没覆盖/只覆盖了部分，但注入段 top1≥20
    # 的条目与理由矛盾（全面否认；或自认「仅覆盖X」却纯话术连 X 都没答），不算②。
    m["b1"], m["b2p"], m["b2"], m["b3"], m["b4"], m["b5"] = [], [], [], [], [], []
    er_map = {r["seq"]: r for r in turns}
    for s in m["wrong_seqs"]:
        r = er_map[s]
        reason = r.get("escalate_reason") or ""
        strong = (top1.get(s) or 0) >= FORCE_THRESHOLD
        is_b = norm(r.get("answer", "")) == norm(HANDOFF)
        # 订单锚定豁免（终审裁定 seq11 误报）：按单号/具体订单查数据，库结构性
        # 没有这单的信息，理由「无对应条目」成立，不算不实
        order_anchored = bool(re.search(r"单号|具体订单|订单核对|单据", 
                                        (r.get("question") or "") + reason))
        if r.get("error") or reason.startswith("生成重试后仍失败"):
            m["b3"].append(s)          # 重试/兜底失败（工程缺陷）
        elif strong and not order_anchored and DENY_RE.search(reason) and (
                FULL_DENY_RE.search(reason)
                or (is_b and PARTIAL_COVER_RE.search(reason))):
            m["b2p"].append(s)         # 理由不实（真缺陷候选：与注入条目矛盾）
        elif reason and "仍转人工且未写理由" not in reason \
                and r.get("path") != "human_escalation":
            m["b2"].append(s)          # 模型自写理由（理由与库一致，按设计合理）
        elif strong:
            m["b1"].append(s)          # 无理由话术（限强制作答带=真缺陷）
        elif r.get("path") == "human_escalation" or reason.startswith("决策层"):
            m["b4"].append(s)          # 决策层 fast path（阈下按设计）
        else:
            m["b5"].append(s)          # 阈下生成层自主（top1<20，政策允许，含混合答）
    return m


for d in sys.argv[1:]:
    rd = Path(d)
    if not (rd / "game_1" / "turns.jsonl").exists():
        print(f"[skip] {rd}: no game_1/turns.jsonl")
        continue
    m = analyze(rd)
    print(f"\n=== {m['dir']} === {m['n']}题 陷阱题{m['traps']}")
    print(f"口径A(含转人工字样): {m['A'][0]}/{m['n']}={m['A'][0]/m['n']*100:.0f}% 均{m['A'][1]:.1f}s")
    print(f"口径B(整句话术):    {m['B'][0]}/{m['n']}={m['B'][0]/m['n']*100:.0f}% 均{m['B'][1]:.1f}s"
          f"  [fast path {m['fast_n']}题 均{m['fast_lat']:.1f}s | 生成层自转 {m['gen_n']}题 均{m['gen_lat']:.1f}s]")
    w = m["wrong"]
    print(f"该答却转人工A(含字样): {w[0]}/{w[1]}={w[2]:.0f}%{' ✅≤10%' if w[2] <= 10 else ' ❌>10%'}  seq={m['wrong_seqs']}")
    print(f"该答却转人工B(整句):  {m['b_wrong_n']}/{w[1]}={m['b_wrong_n']/w[1]*100:.0f}%  seq={m['b_wrong_seqs']}")
    # Chief 口径（2026-10-28）：A 口径拆三类——①无理由话术（模型决策缺陷）
    # ②模型自写理由（按设计合理）③重试/兜底失败（工程缺陷）；决策层 fast path 单列
    for k, label in (("b1", "①无理由话术(限top1≥20)"), ("b2p", "②'理由不实(真缺陷候选)"),
                     ("b2", "②模型自写理由(一致)"),
                     ("b3", "③重试/兜底失败"), ("b4", "④决策层fast path"),
                     ("b5", "⑤阈下生成层自主")):
        seqs = m[k]
        print(f"  {label}: {len(seqs)} 题  seq={seqs}")
    print(f"强相关(top1≥20)全列: {m['strong_all']}")
    print(f"强相关仍转人工: {m['strong_escal']}")
    print(f"P90(nearest-rank): {m['p90']:.1f}s")
    if m["retries"]:
        print(f"重试题: {m['retries']}")
    print(f"escalate_reason {len(m['er_fast'])+len(m['er_synth'])+len(m['er_model'])} 条:"
          f" 决策层{len(m['er_fast'])} 缺陷合成{len(m['er_synth'])} 模型自写{len(m['er_model'])}")
    for s, t in m["er_synth"]:
        print(f"  [缺陷合成] seq{s}: {t[:70]}")
    for s, t in m["er_model"]:
        print(f"  [模型自写] seq{s}: {t[:70]}")
