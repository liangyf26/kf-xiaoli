#!/usr/bin/env python3
"""#060b V2/V2b 抽查工具（任务书唯一来源：turtleschool/docs/任务书-060b-两阶段筛选.md A3 §6）。

V2 条目级抽查（分母写死）：
- 抽样框 = screen_060.json 全部 sessions[].candidates[]，按会话 verdict 分两层：
  customer 层 / not_customer 层（unsure 不参与，stage-2 由 Writer 直接判）。
- 每层取 15 条：层内按 (session_key, session_start, turn_idx) 排序后等距抽（种子 60），
  同一会话最多 2 条（等距位超 2 时用种子随机替换成层内未选中且会话计数<2 的条目）。
- 某层 <15 → 全取，实际 n 报告，不许用另一层补齐；两层合计 <10 → 「样本不足·待补」
  不判 PASS。准确率 = 判对/实际复核，两层各报一个再报合计，判线 ≥85%。

V2b 会话级误剔盲复核：
- 抽样框 = verdict=="not_customer" 的全部会话，按 (session_start, session_key) 排序后
  等距抽 10 个（--sample-sessions，种子 60），不足全取并写明「总体不足 10」。
- 误剔 = 盲复核认为该会话含至少一条真实客户诉求（问/要/查/办/投诉）。
- 误剔率 = 误剔会话数 / 实际复核会话数，判线 ≤10%（10 个最多 1 个）。
- 逐会话轮次文本从 --sessions 库按 §3.2 同款窗口（最后 20 轮）渲染进盲稿供 Writer 看。

命令（任务书 A3 §6 原样）：
  screen_check.py --screen screen_060.json --output screen_check_030.json --sample 15,15 --seed 60
  screen_check.py --audit-sessions --screen screen_060.json --output screen_check_030.json --sample-sessions 10 --seed 60
  screen_check.py --check screen_check_030.json
  screen_check.py --check-audit screen_check_030.json

盲约定：盲稿不含系统判定；Writer 盲判用 --print-blind（只打印题目/会话轮次，无 verdict）。
不改主产物 screen_060.json；本工具只写 screen_check_030.json。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import expand_sample as es  # noqa: E402
import screen_candidates as sc  # noqa: E402  复用窗口/渲染（与 stage-1 同款上下文）

PASS_LINE_V2 = 0.85      # V2 判线 ≥85%
PASS_LINE_V2B = 0.10     # V2b 误剔率判线 ≤10%
MIN_TOTAL_REVIEWED = 10  # 两层合计复核 <10 → 样本不足·待补


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _evenly(items: list, n: int, seed: int) -> list:
    """排序后等距抽 n 条（n>=len 全取）。返回选中的下标（升序）。"""
    if n >= len(items):
        return list(range(len(items)))
    idx = sorted({round(i * (len(items) - 1) / (n - 1)) for i in range(n)})
    return idx


def _cap_two_per_session(items: list, idxs: list, n: int, seed: int) -> list:
    """等距位同会话 >2 条时：保留该会话前 2 个等距位，超额位用种子随机替换成
    层内未选中且会话计数<2 的条目（找不到替换位就宁可少取，不许破 2 条上限）。"""
    key = lambda i: (items[i]["session_key"], items[i]["session_start"])
    cnt: dict = {}
    keep, drop = [], []
    for i in idxs:
        k = key(i)
        cnt[k] = cnt.get(k, 0) + 1
        (keep if cnt[k] <= 2 else drop).append(i)
    if not drop:
        return keep
    rng = random.Random(seed)
    chosen = set(keep)
    cnt2: dict = {}
    for i in keep:
        cnt2[key(i)] = cnt2.get(key(i), 0) + 1
    pool = [i for i in range(len(items)) if i not in chosen and cnt2.get(key(i), 0) < 2]
    for _ in drop:
        if not pool:
            break
        pick = rng.choice(pool)
        pool.remove(pick)
        keep.append(pick)
        cnt2[key(pick)] = cnt2.get(key(pick), 0) + 1
        pool = [i for i in pool if cnt2.get(key(i), 0) < 2]
    return sorted(keep)


def _layer_sample(cands: list, n: int, seed: int) -> list:
    """一层抽样：排序 → 等距 → 同会话≤2。cands 为该层全部候选 dict。"""
    items = sorted(cands, key=lambda c: (c["session_key"], c["session_start"],
                                         c["turn_idx"]))
    idxs = _evenly(items, n, seed)
    idxs = _cap_two_per_session(items, idxs, n, seed)
    return [items[i] for i in idxs]


def _session_window(sessions_by_key: dict, sk: str, ss: str, cand_idxs: list) -> dict:
    """渲染筛选时送入模型的同一窗口（候选轮 + 前 3 客户轮，最后 20 轮）。"""
    s = sessions_by_key.get((sk, ss))
    if s is None:
        return {"sent_turns": 0, "sent_chars": 0, "window_text": "（sessions.json 找不到该会话）"}
    kept, chars, _ov = sc.build_screen_window(s["turns"], cand_idxs)
    return {"sent_turns": len(kept), "sent_chars": chars,
            "window_text": "\n".join(sc.render_turn(s["turns"][i]) for i in kept)}


def cmd_sample(args) -> int:
    """V2 抽查（任务书 §6 定稿）：customer 层按会话、not_customer 层按条目。"""
    screen = _load(args.screen)
    sessions_by_key = {(s["session_key"], s["start_time"]): s
                       for s in es.canonical_sessions(_load(args.sessions)["sessions"])}
    n_sessions, n_items = args.sample_sessions, args.sample_items
    cust_frame = sorted((x for x in screen.get("sessions", [])
                         if x.get("verdict") == "customer" and x.get("confirm") == "keep"),
                        key=lambda x: (x["session_start"], x["session_key"]))
    notc_items = []
    for x in screen.get("sessions", []):
        if x.get("verdict") == "not_customer":
            for c in x.get("candidates", []):
                notc_items.append({**c, "layer": "not_customer"})
    samples = []
    for x in (cust_frame[i] for i in _evenly(cust_frame, n_sessions, args.seed)):
        win = _session_window(sessions_by_key, x["session_key"], x["session_start"],
                              [c["turn_idx"] for c in x.get("candidates", [])])
        samples.append({
            "sample_id": f"V2-{len(samples) + 1:03d}",
            "unit": "session",
            "session_key": x["session_key"],
            "session_start": x["session_start"],
            "layer": "customer",
            "sent_turns": win["sent_turns"],
            "sent_chars": win["sent_chars"],
            "window_text": win["window_text"],
            "writer_answer": None,     # 是/否：该会话有没有我方业务范围内的客户诉求
        })
    layer_n = {"customer": len(samples)}
    for c in _layer_sample(notc_items, n_items, args.seed):
        samples.append({
            "sample_id": f"V2-{len(samples) + 1:03d}",
            "unit": "item",
            "session_key": c["session_key"],
            "session_start": c["session_start"],
            "turn_idx": c["turn_idx"],
            "question": c["question"],
            "layer": "not_customer",
            "writer_answer": None,     # 是/否：这条原话的对端是不是客户
        })
    layer_n["not_customer"] = len(samples) - layer_n["customer"]
    layer_frame = {"customer": len(cust_frame), "not_customer": len(notc_items)}
    total_reviewed_possible = sum(layer_n.values())
    data = {
        "description": ("#060b V2 抽查盲稿（customer 层会话级、not_customer 层条目级；"
                        "Writer 填 writer_answer；--check 算准确率）"),
        "screen_source": args.screen,
        "screen_meta_cap": screen.get("meta", {}).get("cap"),
        "sample_spec": f"sessions={n_sessions},items={n_items}",
        "seed": args.seed,
        "layer_frame": layer_frame,
        "layer_n": layer_n,
        "layer_note": {k: "总体不足 15" for k in layer_n
                       if layer_frame[k] < (n_sessions if k == "customer" else n_items)},
        "samples": samples,
    }
    if total_reviewed_possible < MIN_TOTAL_REVIEWED:
        data["insufficient"] = ("样本不足·待补：两层合计 "
                                f"{total_reviewed_possible} < {MIN_TOTAL_REVIEWED}，不判 PASS")
    out = Path(args.output)
    merged = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    merged.update(data)
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"V2 盲稿: customer 层（会话）框 {layer_frame['customer']} 抽 {layer_n['customer']}；"
          f"not_customer 层（条目）框 {layer_frame['not_customer']} 抽 {layer_n['not_customer']}"
          f"（seed={args.seed}，条目同会话≤2）→ {args.output}")
    if data.get("insufficient"):
        print(f"⚠️ {data['insufficient']}")
    return 0


def cmd_audit(args) -> int:
    screen = _load(args.screen)
    sessions_data = _load(args.sessions)
    canon = es.canonical_sessions(sessions_data["sessions"])
    by_key = {(s["session_key"], s["start_time"]): s for s in canon}
    frame = sorted((x for x in screen.get("sessions", [])
                    if x.get("verdict") == "not_customer"),
                   key=lambda x: (x["session_start"], x["session_key"]))
    idxs = _evenly(frame, args.sample_sessions, args.seed)
    picked = [frame[i] for i in idxs]
    entries = []
    for x in picked:
        s = by_key.get((x["session_key"], x["session_start"]))
        if s is None:
            print(f"⚠️ 会话 {x['session_key']}|{x['session_start']} 在 sessions.json 找不到"
                  f"（screen 与库不一致？）", file=sys.stderr)
            continue
        kept, chars, _ov = sc.build_screen_window(s["turns"], [])
        entries.append({
            "session_key": x["session_key"],
            "session_start": x["session_start"],
            "sent_turns": len(kept),
            "sent_chars": chars,
            "window_text": "\n".join(sc.render_turn(s["turns"][i]) for i in kept),
            "writer_answer": None,   # 是/否：该会话是否含至少一条真实客户诉求
            "writer_reason": "",
        })
    data = {
        "session_audit_spec": {
            "frame_total": len(frame),
            "sampled": len(entries),
            "sample_sessions": args.sample_sessions,
            "seed": args.seed,
            "insufficient_frame": len(frame) < args.sample_sessions,
            "false_drop_definition": "盲复核认为该会话含至少一条真实客户诉求（问/要/查/办/投诉）",
        },
        "session_audit": {
            "entries": entries,
            "audited": 0, "false_drops": 0, "rate": None, "verdict": "未复核",
        },
    }
    out = Path(args.output)
    merged = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    merged.update(data)
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    note = "（总体不足 10，分母改为实际数）" if len(frame) < args.sample_sessions else ""
    print(f"V2b 盲稿: not_customer 会话框 {len(frame)} 抽 {len(entries)} {note}"
          f"→ {args.output}")
    return 0


def cmd_check(args) -> int:
    data = _load(args.check_file)
    samples = data.get("samples", [])
    done = [s for s in samples if s.get("writer_answer") in ("是", "否")]
    if data.get("insufficient"):
        print(f"⛔ 样本不足·待补：{data['insufficient']}")
        return 1
    if not done:
        print("尚未复核（writer_answer 全空）。Writer 填完后重跑 --check。")
        return 0
    per_layer = {}
    for layer in ("customer", "not_customer"):
        d = [s for s in done if s["layer"] == layer]
        ok = sum(1 for s in d
                 if (layer == "customer") == (s["writer_answer"] == "是"))
        per_layer[layer] = (ok, len(d))
    total_ok = sum(v[0] for v in per_layer.values())
    total_n = sum(v[1] for v in per_layer.values())
    rate = total_ok / total_n if total_n else 0.0
    verdict = ("PASS" if rate >= PASS_LINE_V2 else "FAIL") if total_n >= 1 else "未复核"
    if total_n < MIN_TOTAL_REVIEWED:
        verdict = f"样本不足·待补（实际复核 {total_n} < {MIN_TOTAL_REVIEWED}）"
        print(f"⛔ {verdict}")
        return 1
    for layer, (ok, n) in per_layer.items():
        print(f"  {layer} 层: {ok}/{n} = {ok / n * 100:.1f}%" if n else
              f"  {layer} 层: 实际复核 0 条")
    print(f"V2 合计准确率: {total_ok}/{total_n} = {rate * 100:.1f}%（判线 ≥85%）→ {verdict}")
    # 结论回写 check 文件（不改主产物 screen_060.json）
    data["check_result"] = {"audited": total_n, "correct": total_ok,
                            "rate": round(rate, 4), "verdict": verdict,
                            "per_layer": {k: {"correct": v[0], "n": v[1]}
                                          for k, v in per_layer.items()}}
    Path(args.check_file).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if verdict == "PASS" else 1


def cmd_check_audit(args) -> int:
    data = _load(args.check_audit_file)
    spec = data.get("session_audit_spec", {})
    audit = data.get("session_audit", {})
    entries = audit.get("entries", [])
    done = [e for e in entries if e.get("writer_answer") in ("是", "否")]
    if not done:
        print("尚未复核（session_audit.entries 的 writer_answer 全空）。")
        return 0
    false_drops = sum(1 for e in done if e["writer_answer"] == "是")
    n = len(done)
    rate = false_drops / n if n else 0.0
    verdict = "PASS" if rate <= PASS_LINE_V2B else "FAIL"
    note = "（总体不足 10，分母=实际复核数）" if spec.get("insufficient_frame") else ""
    print(f"V2b 误剔率: {false_drops}/{n} = {rate * 100:.1f}%{note}（判线 ≤10%）→ {verdict}")
    for e in done:
        if e["writer_answer"] == "是":
            print(f"  误剔: {e['session_key']}|{e['session_start']} —— {e.get('writer_reason', '')}")
    audit.update({"audited": n, "false_drops": false_drops,
                  "rate": round(rate, 4), "verdict": verdict})
    data["session_audit"] = audit
    Path(args.check_audit_file).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if verdict == "PASS" else 1


def cmd_print_blind(args) -> int:
    """盲判清单：只打印题目/会话轮次，不含系统判定（Writer 照此填，别看文件里的 layer）。"""
    data = _load(args.print_blind_file)
    for s in data.get("samples", []):
        if s.get("unit") == "session":
            print(f"[{s['sample_id']}] {s['session_key']}|{s['session_start']}"
                  f"（{s['sent_turns']} 轮）")
            print(s["window_text"])
        else:
            print(f"[{s['sample_id']}] {s['session_key']}|{s['session_start']}#T{s['turn_idx']}")
            print(f"  客户原话: {s['question']}")
    audit = data.get("session_audit", {}).get("entries", [])
    for i, e in enumerate(audit, 1):
        print(f"[V2b-{i:02d}] {e['session_key']}|{e['session_start']}（{e['sent_turns']} 轮）")
        print(e["window_text"])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="#060b V2/V2b 抽查与盲复核（A3 §6）")
    ap.add_argument("--screen", default="data/wx_real/screen_060.json")
    ap.add_argument("--sessions", default=es.DEFAULT_SESSIONS,
                    help="V2b 渲染会话窗口用（--audit-sessions）")
    ap.add_argument("--output", default="data/wx_real/screen_check_030.json")
    ap.add_argument("--check", dest="check_file", default=None, metavar="FILE",
                    help="核 V2 条目级准确率")
    ap.add_argument("--check-audit", dest="check_audit_file", default=None, metavar="FILE",
                    help="核 V2b 会话级误剔率")
    ap.add_argument("--audit-sessions", action="store_true", help="生成 V2b 会话盲复核稿")
    ap.add_argument("--sample-sessions", type=int, default=15,
                    help="V2 customer 层抽几个会话（默认 15；配合 --audit-sessions 时为 V2b 的 10）")
    ap.add_argument("--sample-items", type=int, default=15,
                    help="V2 not_customer 层抽几条（默认 15，同会话最多 2 条）")
    ap.add_argument("--seed", type=int, default=60)
    ap.add_argument("--print-blind", dest="print_blind_file", default=None, metavar="FILE",
                    help="打印盲判清单（不含系统判定）")
    args = ap.parse_args(argv)
    if args.print_blind_file:
        return cmd_print_blind(args)
    if args.check_file:
        return cmd_check(args)
    if args.check_audit_file:
        return cmd_check_audit(args)
    if args.audit_sessions:
        if "--sample-sessions" not in sys.argv:
            args.sample_sessions = 10          # V2b 默认 10，与 V2 的 15 区分
        return cmd_audit(args)
    return cmd_sample(args)


if __name__ == "__main__":
    sys.exit(main())
