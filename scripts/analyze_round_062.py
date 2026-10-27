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
import sys
from pathlib import Path

sys.path.insert(0, "/tmp/kf062_impl")
from backend.config import settings  # noqa: E402

HANDOFF = settings.HANDOFF_MESSAGE
HANDOFF_KEY = "转接人工客服"
FORCE_THRESHOLD = settings.FORCE_ANSWER_MIN_TOP1


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
    # A-wrong 五分（Chief 口径+收口局细化）：顺序判定 ③→②→①→④→⑤，互斥
    m["b1"], m["b2"], m["b3"], m["b4"], m["b5"] = [], [], [], [], []
    er_map = {r["seq"]: r for r in turns}
    for s in m["wrong_seqs"]:
        r = er_map[s]
        reason = r.get("escalate_reason") or ""
        if r.get("error") or reason.startswith("生成重试后仍失败"):
            m["b3"].append(s)          # 重试/兜底失败（工程缺陷）
        elif reason and "仍转人工且未写理由" not in reason \
                and r.get("path") != "human_escalation":
            m["b2"].append(s)          # 模型自写理由（按设计合理）
        elif (top1.get(s) or 0) >= FORCE_THRESHOLD:
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
    for k, label in (("b1", "①无理由话术(限top1≥20)"), ("b2", "②模型自写理由"),
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
